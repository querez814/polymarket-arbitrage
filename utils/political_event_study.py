"""Honest, event-clustered analysis for non-executable political price history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Iterable, Sequence


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    price: float
    source: str


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    family: str
    occurrence_at: datetime
    points: Sequence[PricePoint]


def _window_point(
    points: Sequence[PricePoint],
    *,
    target: datetime,
    direction: str,
    tolerance: timedelta,
    excluded_timestamps: set[datetime],
) -> PricePoint | None:
    """Choose one non-reused point in a directionally valid bounded window."""
    if direction == "before":
        candidates = [
            point for point in points
            if point.timestamp <= target
            and target - point.timestamp <= tolerance
            and point.timestamp not in excluded_timestamps
        ]
        return max(candidates, key=lambda point: point.timestamp, default=None)
    candidates = [
        point for point in points
        if point.timestamp >= target
        and point.timestamp - target <= tolerance
        and point.timestamp not in excluded_timestamps
    ]
    return min(candidates, key=lambda point: point.timestamp, default=None)


def _event_observation(
    event: EventSpec, *, baseline_minutes: int, entry_minutes: int, horizon_minutes: int,
    point_tolerance_minutes: int,
) -> dict | None:
    points = sorted(event.points, key=lambda point: point.timestamp)
    tolerance = timedelta(minutes=point_tolerance_minutes)
    used_timestamps: set[datetime] = set()
    baseline = _window_point(
        points,
        target=event.occurrence_at - timedelta(minutes=baseline_minutes),
        direction="before",
        tolerance=tolerance,
        excluded_timestamps=used_timestamps,
    )
    if baseline:
        used_timestamps.add(baseline.timestamp)
    entry = _window_point(
        points,
        target=event.occurrence_at - timedelta(minutes=entry_minutes),
        direction="before",
        tolerance=tolerance,
        excluded_timestamps=used_timestamps,
    )
    if entry:
        used_timestamps.add(entry.timestamp)
    exit_point = _window_point(
        points,
        target=event.occurrence_at + timedelta(minutes=horizon_minutes),
        direction="after",
        tolerance=tolerance,
        excluded_timestamps=used_timestamps,
    )
    if not baseline or not entry or not exit_point:
        return None
    return {
        "event_id": event.event_id,
        "family": event.family,
        "baseline_price": baseline.price,
        "entry_price": entry.price,
        "exit_price": exit_point.price,
        "signal": entry.price - baseline.price,
    }


def _trades(observations: Iterable[dict], threshold: float, cost: float) -> list[dict]:
    trades = []
    for observation in observations:
        signal = observation["signal"]
        if abs(signal) < threshold:
            continue
        direction = "YES" if signal > 0 else "NO"
        gross_probability_points = (
            observation["exit_price"] - observation["entry_price"]
            if direction == "YES"
            else observation["entry_price"] - observation["exit_price"]
        )
        trades.append({
            **observation,
            "direction": direction,
            "gross_probability_points": gross_probability_points,
            "assumed_round_trip_cost": cost,
            "net_probability_points": gross_probability_points - cost,
        })
    return trades


def _summary(trades: Sequence[dict]) -> dict:
    probability_points = [trade["net_probability_points"] for trade in trades]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in probability_points:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "trigger_count": len(trades),
        "mean_net_probability_points": mean(probability_points) if probability_points else None,
        "cumulative_net_probability_points": cumulative,
        "max_drawdown_probability_points": max_drawdown,
        "trades": list(trades),
    }


def run_price_only_study(
    events: Sequence[EventSpec],
    *,
    baseline_minutes: int,
    entry_minutes: int,
    horizon_minutes: int,
    threshold_candidates: Sequence[float],
    assumed_round_trip_cost: float,
    train_fraction: float = 0.6,
    point_tolerance_minutes: int = 2,
) -> dict:
    """Run a walk-forward signal study; it never claims executable performance."""
    if not events or not threshold_candidates:
        raise ValueError("events and threshold_candidates are required")
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    if point_tolerance_minutes < 0:
        raise ValueError("point_tolerance_minutes must be non-negative")
    ordered = sorted(events, key=lambda event: (event.occurrence_at, event.event_id))
    split = max(1, min(len(ordered) - 1, int(len(ordered) * train_fraction))) if len(ordered) > 1 else 1
    train_events, holdout_events = ordered[:split], ordered[split:]
    observations = {
        event.event_id: _event_observation(
            event,
            baseline_minutes=baseline_minutes,
            entry_minutes=entry_minutes,
            horizon_minutes=horizon_minutes,
            point_tolerance_minutes=point_tolerance_minutes,
        )
        for event in ordered
    }
    train_observations = [observations[event.event_id] for event in train_events if observations[event.event_id]]
    holdout_observations = [observations[event.event_id] for event in holdout_events if observations[event.event_id]]
    selected_threshold = max(
        threshold_candidates,
        key=lambda threshold: _summary(_trades(train_observations, threshold, assumed_round_trip_cost))["mean_net_probability_points"]
        if _trades(train_observations, threshold, assumed_round_trip_cost) else float("-inf"),
    )
    train_trades = _trades(train_observations, selected_threshold, assumed_round_trip_cost)
    holdout_trades = _trades(holdout_observations, selected_threshold, assumed_round_trip_cost)
    return {
        "data_quality": {
            "classification": "price_only_signal_research",
            "executable_pnl": False,
            "limitation": "Minute price history has no contemporaneous bid/ask depth, queue position, or fill evidence.",
        },
        "parameters": {
            "baseline_minutes": baseline_minutes,
            "entry_minutes": entry_minutes,
            "horizon_minutes": horizon_minutes,
            "assumed_round_trip_cost": assumed_round_trip_cost,
            "point_tolerance_minutes": point_tolerance_minutes,
        },
        "coverage": {
            "events_requested": len(ordered),
            "complete_events": sum(observation is not None for observation in observations.values()),
            "missing_window_events": sum(observation is None for observation in observations.values()),
        },
        "walk_forward": {
            "train_event_ids": [event.event_id for event in train_events],
            "holdout_event_ids": [event.event_id for event in holdout_events],
            "selected_threshold": selected_threshold,
        },
        "train": _summary(train_trades),
        "holdout": _summary(holdout_trades),
    }
