from datetime import datetime, timedelta, timezone

import pytest

from utils.political_event_study import EventSpec, PricePoint, run_price_only_study


UTC = timezone.utc
OCCURRENCE = datetime(2026, 7, 1, 18, tzinfo=UTC)


def _points(*prices: float) -> list[PricePoint]:
    return [
        PricePoint(
            timestamp=OCCURRENCE + timedelta(minutes=offset),
            price=price,
            source="prices-history",
        )
        for offset, price in zip((-30, -15, 0, 15), prices)
    ]


def test_price_only_study_uses_event_clusters_for_holdout_and_labels_non_executable():
    events = [
        EventSpec("speech-a", "speech", OCCURRENCE, _points(0.50, 0.56, 0.62, 0.66)),
        EventSpec("speech-b", "speech", OCCURRENCE, _points(0.50, 0.55, 0.60, 0.64)),
        EventSpec("vote-a", "vote", OCCURRENCE, _points(0.50, 0.45, 0.40, 0.54)),
        EventSpec("vote-b", "vote", OCCURRENCE, _points(0.50, 0.44, 0.38, 0.56)),
    ]

    result = run_price_only_study(
        events,
        baseline_minutes=30,
        entry_minutes=15,
        horizon_minutes=15,
        threshold_candidates=(0.03, 0.10),
        assumed_round_trip_cost=0.02,
        train_fraction=0.5,
    )

    assert result["data_quality"]["classification"] == "price_only_signal_research"
    assert result["data_quality"]["executable_pnl"] is False
    assert result["walk_forward"]["train_event_ids"] == ["speech-a", "speech-b"]
    assert result["walk_forward"]["holdout_event_ids"] == ["vote-a", "vote-b"]
    assert result["walk_forward"]["selected_threshold"] == 0.03
    assert result["holdout"]["trigger_count"] == 2
    assert all(row["net_probability_points"] < 0 for row in result["holdout"]["trades"])


def test_price_only_study_reports_missing_windows_as_coverage_not_zero_returns():
    event = EventSpec(
        "approval-a",
        "approval",
        OCCURRENCE,
        [PricePoint(OCCURRENCE, 0.5, "prices-history")],
    )

    result = run_price_only_study(
        [event],
        baseline_minutes=30,
        entry_minutes=15,
        horizon_minutes=15,
        threshold_candidates=(0.01,),
        assumed_round_trip_cost=0.01,
    )

    assert result["coverage"]["complete_events"] == 0
    assert result["coverage"]["missing_window_events"] == 1
    assert result["holdout"]["trigger_count"] == 0


def test_price_only_study_requires_distinct_directional_points_within_tolerance():
    event = EventSpec(
        "timing-a",
        "speech",
        OCCURRENCE,
        [
            # This point is too late for both pre-event windows, and cannot be
            # reused as the exit even though it is close to all three targets.
            PricePoint(OCCURRENCE, 0.50, "prices-history"),
            PricePoint(OCCURRENCE + timedelta(minutes=15), 0.55, "prices-history"),
        ],
    )

    result = run_price_only_study(
        [event],
        baseline_minutes=30,
        entry_minutes=15,
        horizon_minutes=15,
        threshold_candidates=(0.01,),
        assumed_round_trip_cost=0.01,
        point_tolerance_minutes=2,
    )

    assert result["coverage"]["complete_events"] == 0
    assert result["coverage"]["missing_window_events"] == 1


def test_price_only_study_reports_probability_points_not_returns():
    event = EventSpec("speech-a", "speech", OCCURRENCE, _points(0.50, 0.56, 0.62, 0.66))

    result = run_price_only_study(
        [event],
        baseline_minutes=30,
        entry_minutes=15,
        horizon_minutes=15,
        threshold_candidates=(0.03,),
        assumed_round_trip_cost=0.02,
    )

    trade = result["train"]["trades"][0]
    assert trade["gross_probability_points"] == pytest.approx(0.10)
    assert trade["net_probability_points"] == pytest.approx(0.08)
    assert "gross_return" not in trade
    assert "mean_net_probability_points" in result["train"]
