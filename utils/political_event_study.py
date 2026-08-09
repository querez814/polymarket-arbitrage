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


def _nearest(points: Sequence[PricePoint], target: datetime) -> PricePoint | None:
    if not points:
        return None
    return min(points, key=lambda point: abs(point.timestamp - target))


def _event_observation(
    event: EventSpec, *, baseline_minutes: int, entry_minutes: int, horizon_minutes: int
) -> dict | None:
    points = sorted(event.points, key=lambda point: point.timestamp)
    baseline = _nearest(points, event.occurrence_at - timedelta(minutes=baseline_minutes))
    entry = _nearest(points, event.occurrence_at - timedelta(minutes=entry_minutes))
    exit_point = _nearest(points, event.occurrence_at + timedelta(minutes=horizon_minutes))
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
        gross_return = (
            observation["exit_price"] - observation["entry_price"]
            if direction == "YES"
            else observation["entry_price"] - observation["exit_price"]
        )
        trades.append({
            **observation,
            "direction": direction,
            "gross_return": gross_return,
            "assumed_round_trip_cost": cost,
            "net_return": gross_return - cost,
        })
    return trades


def _summary(trades: Sequence[dict]) -> dict:
    returns = [trade["net_return"] for trade in trades]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "trigger_count": len(trades),
        "mean_net_return": mean(returns) if returns else None,
        "cumulative_net_return": cumulative,
        "max_drawdown": max_drawdown,
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
) -> dict:
    """Run a walk-forward signal study; it never claims executable performance."""
    if not events or not threshold_candidates:
        raise ValueError("events and threshold_candidates are required")
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    ordered = sorted(events, key=lambda event: (event.occurrence_at, event.event_id))
    split = max(1, min(len(ordered) - 1, int(len(ordered) * train_fraction))) if len(ordered) > 1 else 1
    train_events, holdout_events = ordered[:split], ordered[split:]
    observations = {
        event.event_id: _event_observation(
            event,
            baseline_minutes=baseline_minutes,
            entry_minutes=entry_minutes,
            horizon_minutes=horizon_minutes,
        )
        for event in ordered
    }
    train_observations = [observations[event.event_id] for event in train_events if observations[event.event_id]]
    holdout_observations = [observations[event.event_id] for event in holdout_events if observations[event.event_id]]
    selected_threshold = max(
        threshold_candidates,
        key=lambda threshold: _summary(_trades(train_observations, threshold, assumed_round_trip_cost))["mean_net_return"]
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
