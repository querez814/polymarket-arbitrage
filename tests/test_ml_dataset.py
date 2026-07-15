from copy import deepcopy
from datetime import timedelta

import pytest

from utils.ml_dataset import (
    FEATURE_NAMES,
    LABEL_NAMES,
    CostAssumptions,
    build_point_in_time_dataset,
    build_point_in_time_examples,
    chronological_split,
)


def assumptions(**overrides):
    values = {
        "polymarket_taker_fee": 0.01,
        "kalshi_taker_fee": 0.02,
        "gas_cost_per_leg": 0.001,
        "slippage_per_leg": 0.002,
        "decision_latency": timedelta(seconds=5),
        "label_horizon": timedelta(minutes=1),
        "max_label_delay": timedelta(minutes=1),
    }
    values.update(overrides)
    return CostAssumptions(**values)


def snapshot(minute, *, buy_price=0.40, sell_price=0.50):
    timestamp = f"2026-06-24T12:{minute:02d}:00Z"
    poly_timestamp = f"2026-06-24T12:{minute:02d}:01Z"
    kalshi_timestamp = f"2026-06-24T12:{minute:02d}:03Z"
    return {
        "timestamp": timestamp,
        "pair": {"polymarket_id": "poly-1", "kalshi_ticker": "kalshi-1"},
        "polymarket": {
            "timestamp": poly_timestamp,
            "yes": {"ask_size": 20, "bid_size": 19, "spread": 0.02},
        },
        "kalshi": {
            "timestamp": kalshi_timestamp,
            "yes": {"ask_size": 15, "bid_size": 14, "spread": 0.03},
        },
        "directions": [
            {
                "token": "YES",
                "buy_platform": "polymarket",
                "sell_platform": "kalshi",
                "buy_price": buy_price,
                "sell_price": sell_price,
            }
        ],
    }


def test_builds_point_in_time_features_and_cost_adjusted_future_label():
    rows = [
        snapshot(0, buy_price=0.40, sell_price=0.44),
        snapshot(1, buy_price=0.41, sell_price=0.45),
        snapshot(2, buy_price=0.42, sell_price=0.50),
    ]

    examples = build_point_in_time_examples(rows, assumptions())

    assert len(examples) == 1
    example = examples[0]
    assert example.feature_mapping() == dict(zip(FEATURE_NAMES, example.features))
    assert example.label_mapping() == dict(zip(LABEL_NAMES, example.labels))
    assert example.feature_timestamp.isoformat() == "2026-06-24T12:00:00+00:00"
    assert example.label_timestamp.isoformat() == "2026-06-24T12:02:00+00:00"
    assert example.feature_mapping()["venue_observation_skew_seconds"] == 2
    # Future gross edge 0.08 less fees 0.0042 + 0.0100, gas 0.002, slippage 0.004.
    assert example.label_mapping()["future_net_edge_after_costs"] == pytest.approx(
        0.0598
    )
    assert example.label_mapping()["positive_after_costs"] is True


def test_label_selection_includes_latency_and_rejects_stale_future_observations():
    rows = [snapshot(0), snapshot(1), snapshot(2)]

    stale_examples = build_point_in_time_examples(
        rows,
        assumptions(
            decision_latency=timedelta(seconds=1),
            label_horizon=timedelta(minutes=1),
            max_label_delay=timedelta(seconds=30),
        ),
    )
    examples = build_point_in_time_examples(
        rows,
        assumptions(
            decision_latency=timedelta(seconds=1),
            label_horizon=timedelta(minutes=1),
            max_label_delay=timedelta(minutes=1),
        ),
    )

    assert stale_examples == []
    assert len(examples) == 1
    assert examples[0].label_timestamp.isoformat() == "2026-06-24T12:02:00+00:00"


def test_duplicate_pair_timestamp_fails_closed():
    row = snapshot(0)

    with pytest.raises(ValueError, match="duplicate snapshot"):
        build_point_in_time_examples([row, deepcopy(row)], assumptions())


def test_incomplete_feature_direction_is_excluded_and_counted():
    rows = [snapshot(0), snapshot(2)]
    rows[0]["kalshi"]["yes"]["spread"] = None

    result = build_point_in_time_dataset(rows, assumptions())

    assert result.examples == ()
    assert result.snapshots_seen == 2
    assert result.candidate_directions == 2
    assert result.skipped_incomplete_features == 1
    assert result.skipped_no_future_snapshot == 1


def test_chronological_split_purges_cross_boundary_labels():
    rows = [snapshot(minute) for minute in range(10)]
    examples = build_point_in_time_examples(
        rows,
        assumptions(
            decision_latency=timedelta(0),
            label_horizon=timedelta(minutes=1),
            max_label_delay=timedelta(0),
        ),
    )

    split = chronological_split(examples, train_fraction=0.5, validation_fraction=0.25)

    validation_start = min(item.feature_timestamp for item in split.validation)
    test_start = min(item.feature_timestamp for item in split.test)
    assert max(item.label_timestamp for item in split.train) < validation_start
    assert max(item.label_timestamp for item in split.validation) < test_start
    assert {item.feature_timestamp for item in split.train}.isdisjoint(
        item.feature_timestamp for item in split.validation
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("polymarket_taker_fee", -0.01),
        ("slippage_per_leg", 1.0),
        ("label_horizon", timedelta(0)),
        ("decision_latency", timedelta(seconds=-1)),
    ],
)
def test_invalid_assumptions_fail_closed(field, value):
    with pytest.raises(ValueError):
        assumptions(**{field: value})
