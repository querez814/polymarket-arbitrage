"""Pure clock policy for scheduled-event pair monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Sequence

from core.event_contracts import EventPairLink

EventLaneState = Literal["scheduled", "warm", "hot", "burst", "cooldown"]


@dataclass(frozen=True)
class EventLanePolicy:
    lookahead: timedelta = timedelta(days=7)
    warm_before: timedelta = timedelta(hours=24)
    hot_before: timedelta = timedelta(hours=1)
    burst_before: timedelta = timedelta(minutes=5)
    burst_after: timedelta = timedelta(minutes=15)
    cooldown_after: timedelta = timedelta(days=1)
    scheduled_interval_seconds: float = 300.0
    warm_interval_seconds: float = 60.0
    hot_interval_seconds: float = 2.0
    burst_interval_seconds: float = 1.0
    cooldown_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not (
            self.lookahead
            > self.warm_before
            > self.hot_before
            > self.burst_before
            > timedelta(0)
        ):
            raise ValueError(
                "event windows must satisfy lookahead > warm > hot > burst > 0"
            )
        if self.burst_after <= timedelta(0) or self.cooldown_after <= self.burst_after:
            raise ValueError(
                "event post-release windows must satisfy cooldown > burst > 0"
            )
        intervals = (
            self.scheduled_interval_seconds,
            self.warm_interval_seconds,
            self.hot_interval_seconds,
            self.burst_interval_seconds,
            self.cooldown_interval_seconds,
        )
        if any(value <= 0 for value in intervals):
            raise ValueError("event lane intervals must be positive")


@dataclass(frozen=True)
class EventPairSchedule:
    pair_id: str
    event_id: str
    event_type: str
    state: EventLaneState
    scheduled_at: datetime
    seconds_to_event: float
    interval_seconds: float


@dataclass(frozen=True)
class EventLaneSnapshot:
    generated_at: datetime
    pairs: tuple[EventPairSchedule, ...]


class EventLaneScheduler:
    """Translate event-pair links into current monitoring schedules."""

    def __init__(
        self,
        *,
        policy: EventLanePolicy,
        clock: Callable[[], datetime] | None = None,
    ):
        self.policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def schedule(self, links: Sequence[EventPairLink]) -> EventLaneSnapshot:
        now = self._now()
        schedules: dict[str, EventPairSchedule] = {}
        for link in links:
            scheduled_at = link.scheduled_at
            if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
                raise ValueError("event-pair link time must be timezone-aware")
            scheduled_at = scheduled_at.astimezone(timezone.utc)
            delta = scheduled_at - now
            state_interval = self._state_interval(delta)
            if state_interval is None:
                continue
            state, interval = state_interval
            candidate = EventPairSchedule(
                pair_id=link.pair_id,
                event_id=link.event_id,
                event_type=link.event_type,
                state=state,
                scheduled_at=scheduled_at,
                seconds_to_event=delta.total_seconds(),
                interval_seconds=interval,
            )
            current = schedules.get(link.pair_id)
            if current is None or self._priority(candidate) < self._priority(current):
                schedules[link.pair_id] = candidate
        return EventLaneSnapshot(
            generated_at=now,
            pairs=tuple(schedules[key] for key in sorted(schedules)),
        )

    def _state_interval(self, delta: timedelta) -> tuple[EventLaneState, float] | None:
        if delta > self.policy.lookahead or delta < -self.policy.cooldown_after:
            return None
        if delta >= self.policy.warm_before:
            return "scheduled", self.policy.scheduled_interval_seconds
        if delta >= self.policy.hot_before:
            return "warm", self.policy.warm_interval_seconds
        if delta >= self.policy.burst_before:
            return "hot", self.policy.hot_interval_seconds
        if delta >= -self.policy.burst_after:
            return "burst", self.policy.burst_interval_seconds
        return "cooldown", self.policy.cooldown_interval_seconds

    @staticmethod
    def _priority(schedule: EventPairSchedule) -> tuple[float, float, str]:
        return (
            schedule.interval_seconds,
            abs(schedule.seconds_to_event),
            schedule.event_id,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event lane clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)
