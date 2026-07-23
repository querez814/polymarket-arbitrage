"""Bounded retry, jitter, circuit breaking, and request metrics for REST reads."""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Callable


class CircuitOpenError(RuntimeError):
    """Endpoint calls are paused after a sustained rolling failure rate."""


class EndpointResilience:
    RETRYABLE_STATUSES = frozenset({425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        window_size: int = 20,
        minimum_calls: int = 5,
        failure_threshold: float = 0.60,
        cooldown_seconds: float = 15.0,
        random_source: random.Random | None = None,
        clock: Callable[[], float] | None = None,
    ):
        if base_delay <= 0 or max_delay < base_delay:
            raise ValueError("retry delays must be positive and ordered")
        if not 0 < minimum_calls <= window_size:
            raise ValueError("minimum_calls must fit within window_size")
        if not 0 < failure_threshold <= 1 or cooldown_seconds <= 0:
            raise ValueError("circuit thresholds must be positive")
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.minimum_calls = minimum_calls
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._outcomes: deque[bool] = deque(maxlen=window_size)
        self._random = random_source or random.SystemRandom()
        self._clock = clock or time.monotonic
        self._opened_until = 0.0
        self._requests = 0
        self._failures = 0
        self._retries = 0
        self._circuit_rejections = 0

    def before_request(self) -> None:
        now = self._clock()
        if now < self._opened_until:
            self._circuit_rejections += 1
            raise CircuitOpenError(
                f"endpoint circuit open for {self._opened_until - now:.1f}s"
            )
        if self._opened_until:
            self._opened_until = 0.0
            self._outcomes.clear()

    def record(self, *, succeeded: bool) -> None:
        self._requests += 1
        if not succeeded:
            self._failures += 1
        self._outcomes.append(succeeded)
        if len(self._outcomes) < self.minimum_calls:
            return
        failure_rate = 1 - (sum(self._outcomes) / len(self._outcomes))
        if failure_rate >= self.failure_threshold:
            self._opened_until = self._clock() + self.cooldown_seconds

    def retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        self._retries += 1
        if retry_after:
            try:
                value = float(retry_after)
                if 0 <= value <= self.max_delay:
                    return value
            except ValueError:
                pass
        exponential = min(self.max_delay, self.base_delay * (2**attempt))
        return min(self.max_delay, exponential + self._random.uniform(0, self.base_delay))

    @property
    def metrics(self) -> dict[str, float | int | bool]:
        failure_rate = (
            1 - sum(self._outcomes) / len(self._outcomes)
            if self._outcomes
            else 0.0
        )
        return {
            "requests": self._requests,
            "failures": self._failures,
            "retries": self._retries,
            "circuit_rejections": self._circuit_rejections,
            "rolling_failure_rate": round(failure_rate, 4),
            "circuit_open": self._clock() < self._opened_until,
        }
