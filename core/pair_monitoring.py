"""Adaptive hot/cold scheduling for already verified cross-venue pairs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import calendar
import math
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class DuePair:
    pair: Any
    tier: str
    scheduled_event_id: str = ""
    event_lane_state: str = ""


@dataclass(frozen=True)
class PairScheduleOverride:
    event_id: str
    lane_state: str
    interval_seconds: float

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.lane_state.strip():
            raise ValueError("pair schedule override identity must be non-empty")
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("pair schedule override interval must be positive")


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
        self._schedule_overrides: dict[str, PairScheduleOverride] = {}

    def set_schedule_overrides(
        self, overrides: dict[str, PairScheduleOverride]
    ) -> None:
        if any(not pair_id.strip() for pair_id in overrides):
            raise ValueError("pair schedule override IDs must be non-empty")
        self._schedule_overrides = dict(overrides)

    def due_pairs(self, pairs: Sequence[Any]) -> list[DuePair]:
        now = self._now()
        ranked = sorted(pairs, key=self._rank, reverse=True)
        ordinary_ranked = [
            pair
            for pair in sorted(pairs, key=self._rank, reverse=True)
            if pair.pair_id not in self._schedule_overrides
        ]
        hot_ids = {pair.pair_id for pair in ordinary_ranked[: self.hot_limit]}
        due: list[DuePair] = []
        for pair in ranked:
            telemetry = self._telemetry.setdefault(pair.pair_id, _Telemetry())
            override = self._schedule_overrides.get(pair.pair_id)
            if override is not None:
                tier = f"event_{override.lane_state}"
                interval = override.interval_seconds
            else:
                tier = "hot" if pair.pair_id in hot_ids else "cold"
                interval = self.hot_interval if tier == "hot" else self.cold_interval
            if (
                telemetry.last_evaluated is None
                or (now - telemetry.last_evaluated).total_seconds() >= interval
            ):
                due.append(
                    DuePair(
                        pair=pair,
                        tier=tier,
                        scheduled_event_id=(override.event_id if override else ""),
                        event_lane_state=(override.lane_state if override else ""),
                    )
                )
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

    def _rank(self, pair: Any) -> tuple[int, float, int, float, float, str]:
        telemetry = self._telemetry.get(pair.pair_id, _Telemetry())
        return (
            int(pair.pair_id in self._schedule_overrides),
            telemetry.observed_net_edge,
            telemetry.opportunity_count,
            float(getattr(pair, "discovery_priority", 0.0)),
            float(getattr(pair, "verification_confidence", 0.0)),
            pair.pair_id,
        )

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def discovery_priority_score(
    *,
    family_size: int,
    event_date_key: str,
    executable_capacity: float,
    max_capacity: float,
    now: datetime | None = None,
) -> float:
    """Score cold exploration from durable evidence, never reported volume."""
    if family_size <= 0 or max_capacity <= 0:
        raise ValueError("family size and maximum capacity must be positive")
    observed_at = now or datetime.now(timezone.utc)
    observed_at = (
        observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    )
    event_at: datetime | None = None
    try:
        if len(event_date_key) == 7:
            year, month = (int(part) for part in event_date_key.split("-"))
            day = calendar.monthrange(year, month)[1]
            event_at = datetime(year, month, day, tzinfo=timezone.utc)
        elif len(event_date_key) == 4:
            event_at = datetime(int(event_date_key), 12, 31, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        event_at = None
    days = (
        max(0.0, (event_at - observed_at).total_seconds() / 86400)
        if event_at
        else 730.0
    )
    rarity_score = 1.0 / family_size
    proximity_score = 1.0 / (1.0 + days / 365.0)
    capacity_score = min(
        1.0, max(0.0, float(executable_capacity)) / float(max_capacity)
    )
    return rarity_score + proximity_score + capacity_score
