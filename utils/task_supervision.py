"""Supervision policy for long-running upstream-dependent tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from utils.http_resilience import CircuitOpenError


class UnexpectedTaskExit(RuntimeError):
    """A critical long-running task returned while its owner was still running."""


@dataclass(frozen=True)
class RestartEvent:
    task_name: str
    attempt: int
    delay_seconds: float
    error_type: str
    error_message: str


def is_transient_upstream_error(error: BaseException) -> bool:
    """Return whether retrying the complete task is safe and useful."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or 500 <= status <= 599
    return isinstance(
        error,
        (
            httpx.RequestError,
            CircuitOpenError,
            asyncio.TimeoutError,
            ConnectionError,
        ),
    )


class RestartingTaskSupervisor:
    """Keep one critical task alive across explicitly transient failures."""

    def __init__(
        self,
        task_name: str,
        *,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_retry: Callable[[RestartEvent], None] | None = None,
    ):
        if base_delay <= 0 or max_delay < base_delay:
            raise ValueError("retry delays must be positive and ordered")
        self.task_name = task_name
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._on_retry = on_retry
        self._consecutive_failures = 0

    def mark_healthy(self) -> None:
        """Reset backoff after the worker completes a meaningful healthy cycle."""
        self._consecutive_failures = 0

    async def run(
        self,
        worker: Callable[[], Awaitable[None]],
        *,
        should_run: Callable[[], bool],
    ) -> None:
        while should_run():
            try:
                await worker()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not is_transient_upstream_error(error):
                    raise
                self._consecutive_failures += 1
                delay = min(
                    self.max_delay,
                    self.base_delay * (2 ** (self._consecutive_failures - 1)),
                )
                if self._on_retry:
                    self._on_retry(
                        RestartEvent(
                            task_name=self.task_name,
                            attempt=self._consecutive_failures,
                            delay_seconds=delay,
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )
                    )
                if should_run():
                    await self._sleep(delay)
                continue

            if should_run():
                raise UnexpectedTaskExit(
                    f"{self.task_name} stopped while the bot was still running"
                )
            return
