"""Adaptive hot/cold scheduling for already verified cross-venue pairs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class DuePair:
    pair: Any
    tier: str


@dataclass
class _Telemetry:
    last_evaluated: datetime | None = None
    observed_net_edge: float = 0.0
    opportunity_count: int = 0


class PairTierMonitor:
    def __init__(
        self,
        *,
        hot_limit: int,
        hot_interval: float,
        cold_interval: float,
        clock: Callable[[], datetime] | None = None,
    ):
        if hot_limit <= 0 or hot_interval <= 0 or cold_interval <= hot_interval:
            raise ValueError("pair tier limits must satisfy 0 < hot < cold")
        self.hot_limit = hot_limit
        self.hot_interval = hot_interval
        self.cold_interval = cold_interval
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._telemetry: dict[str, _Telemetry] = {}

    def due_pairs(self, pairs: Sequence[Any]) -> list[DuePair]:
        now = self._now()
        ranked = sorted(pairs, key=self._rank, reverse=True)
        hot_ids = {pair.pair_id for pair in ranked[: self.hot_limit]}
        due: list[DuePair] = []
        for pair in ranked:
            telemetry = self._telemetry.setdefault(pair.pair_id, _Telemetry())
            tier = "hot" if pair.pair_id in hot_ids else "cold"
            interval = self.hot_interval if tier == "hot" else self.cold_interval
            if telemetry.last_evaluated is None or (
                now - telemetry.last_evaluated
            ).total_seconds() >= interval:
                due.append(DuePair(pair=pair, tier=tier))
        return due

    def mark_evaluated(
        self,
        pair_id: str,
        *,
        observed_net_edge: float,
        opportunity: bool = False,
    ) -> None:
        telemetry = self._telemetry.setdefault(pair_id, _Telemetry())
        telemetry.last_evaluated = self._now()
        telemetry.observed_net_edge = max(0.0, float(observed_net_edge))
        if opportunity:
            telemetry.opportunity_count += 1

    def _rank(self, pair: Any) -> tuple[float, int, float, str]:
        telemetry = self._telemetry.get(pair.pair_id, _Telemetry())
        return (
            telemetry.observed_net_edge,
            telemetry.opportunity_count,
            float(getattr(pair, "verification_confidence", 0.0)),
            pair.pair_id,
        )

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
