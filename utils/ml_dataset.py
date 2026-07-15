"""Deterministic, point-in-time dataset construction for opportunity ranking.

The labels in this module are future observed top-of-book edges, not fills or
realized profit.  That distinction is intentional: the local snapshot pipeline
does not contain order acknowledgements or fills, so it cannot support an
honest realized-PnL label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from utils.historical_data import parse_timestamp

SCHEMA_VERSION = "cross-platform-edge-v1"
FEATURE_NAMES = (
    "buy_price",
    "sell_price",
    "buy_size",
    "sell_size",
    "buy_spread",
    "sell_spread",
    "gross_edge",
    "estimated_cost",
    "net_edge",
    "max_size",
    "venue_observation_skew_seconds",
)
LABEL_NAMES = (
    "future_gross_edge",
    "future_net_edge_after_costs",
    "positive_after_costs",
)


@dataclass(frozen=True)
class CostAssumptions:
    """Explicit economic and timing assumptions used for every label."""

    polymarket_taker_fee: float
    kalshi_taker_fee: float
    gas_cost_per_leg: float
    slippage_per_leg: float
    decision_latency: timedelta
    label_horizon: timedelta
    max_label_delay: timedelta

    def __post_init__(self) -> None:
        rates = {
            "polymarket_taker_fee": self.polymarket_taker_fee,
            "kalshi_taker_fee": self.kalshi_taker_fee,
            "gas_cost_per_leg": self.gas_cost_per_leg,
            "slippage_per_leg": self.slippage_per_leg,
        }
        for name, value in rates.items():
            if not 0 <= value < 1:
                raise ValueError(f"{name} must be in [0, 1)")
        for name in ("decision_latency", "label_horizon", "max_label_delay"):
            if getattr(self, name) < timedelta(0):
                raise ValueError(f"{name} must be non-negative")
        if self.label_horizon <= timedelta(0):
            raise ValueError("label_horizon must be positive")


@dataclass(frozen=True)
class PointInTimeExample:
    """One direction at observation time, labelled from a later observation."""

    schema_version: str
    pair_id: str
    direction_id: str
    feature_timestamp: datetime
    label_timestamp: datetime
    features: tuple[float, ...]
    labels: tuple[float, float, bool]

    def feature_mapping(self) -> dict[str, float]:
        return dict(zip(FEATURE_NAMES, self.features))

    def label_mapping(self) -> dict[str, float | bool]:
        return dict(zip(LABEL_NAMES, self.labels))


@dataclass(frozen=True)
class ChronologicalSplit:
    train: tuple[PointInTimeExample, ...]
    validation: tuple[PointInTimeExample, ...]
    test: tuple[PointInTimeExample, ...]


@dataclass(frozen=True)
class DatasetBuildResult:
    """Examples plus auditable reasons why candidate directions were excluded."""

    examples: tuple[PointInTimeExample, ...]
    snapshots_seen: int
    candidate_directions: int
    skipped_no_future_snapshot: int
    skipped_missing_future_direction: int
    skipped_incomplete_features: int


def build_point_in_time_examples(
    snapshots: Iterable[Mapping[str, Any]],
    assumptions: CostAssumptions,
) -> list[PointInTimeExample]:
    """Build future-edge labels without allowing future fields into features."""
    return list(build_point_in_time_dataset(snapshots, assumptions).examples)


def build_point_in_time_dataset(
    snapshots: Iterable[Mapping[str, Any]],
    assumptions: CostAssumptions,
) -> DatasetBuildResult:
    """Build examples and report every expected data-quality exclusion."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    seen: set[tuple[str, datetime]] = set()
    snapshots_seen = 0
    for snapshot in snapshots:
        snapshots_seen += 1
        pair_id = _pair_id(snapshot)
        timestamp = parse_timestamp(snapshot["timestamp"])
        identity = (pair_id, timestamp)
        if identity in seen:
            raise ValueError(
                f"duplicate snapshot for {pair_id} at {timestamp.isoformat()}"
            )
        seen.add(identity)
        grouped.setdefault(pair_id, []).append(snapshot)

    examples: list[PointInTimeExample] = []
    candidate_directions = 0
    skipped_no_future_snapshot = 0
    skipped_missing_future_direction = 0
    skipped_incomplete_features = 0
    minimum_delta = assumptions.label_horizon + assumptions.decision_latency
    for pair_id, pair_snapshots in sorted(grouped.items()):
        ordered = sorted(
            pair_snapshots, key=lambda row: parse_timestamp(row["timestamp"])
        )
        for index, current in enumerate(ordered):
            current_directions = _validated_directions(current)
            candidate_directions += len(current_directions)
            feature_timestamp = parse_timestamp(current["timestamp"])
            target_timestamp = feature_timestamp + minimum_delta
            future = _first_snapshot_at_or_after(ordered, index + 1, target_timestamp)
            if future is None:
                skipped_no_future_snapshot += len(current_directions)
                continue
            label_timestamp = parse_timestamp(future["timestamp"])
            if label_timestamp - target_timestamp > assumptions.max_label_delay:
                skipped_no_future_snapshot += len(current_directions)
                continue

            future_directions = {
                _direction_id(direction): direction
                for direction in _validated_directions(future)
            }
            for direction in current_directions:
                direction_id = _direction_id(direction)
                future_direction = future_directions.get(direction_id)
                if future_direction is None:
                    skipped_missing_future_direction += 1
                    continue
                try:
                    features = _features(current, direction, assumptions)
                except _IncompleteQuoteError:
                    skipped_incomplete_features += 1
                    continue
                labels = _labels(future_direction, assumptions)
                examples.append(
                    PointInTimeExample(
                        schema_version=SCHEMA_VERSION,
                        pair_id=pair_id,
                        direction_id=direction_id,
                        feature_timestamp=feature_timestamp,
                        label_timestamp=label_timestamp,
                        features=features,
                        labels=labels,
                    )
                )

    examples.sort(key=_example_sort_key)
    validate_point_in_time_examples(examples)
    return DatasetBuildResult(
        examples=tuple(examples),
        snapshots_seen=snapshots_seen,
        candidate_directions=candidate_directions,
        skipped_no_future_snapshot=skipped_no_future_snapshot,
        skipped_missing_future_direction=skipped_missing_future_direction,
        skipped_incomplete_features=skipped_incomplete_features,
    )


def chronological_split(
    examples: Sequence[PointInTimeExample],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> ChronologicalSplit:
    """Split on timestamp boundaries and purge labels crossing a later split."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1")
    if not 0 < validation_fraction < 1 or train_fraction + validation_fraction >= 1:
        raise ValueError("validation_fraction must leave a non-empty test fraction")
    validate_point_in_time_examples(examples)

    timestamps = sorted({example.feature_timestamp for example in examples})
    if len(timestamps) < 3:
        raise ValueError("at least three distinct feature timestamps are required")
    validation_index = max(1, int(len(timestamps) * train_fraction))
    test_index = max(
        validation_index + 1,
        int(len(timestamps) * (train_fraction + validation_fraction)),
    )
    if test_index >= len(timestamps):
        raise ValueError("split fractions leave no distinct test timestamp")
    validation_start = timestamps[validation_index]
    test_start = timestamps[test_index]

    train = tuple(
        example
        for example in examples
        if example.feature_timestamp < validation_start
        and example.label_timestamp < validation_start
    )
    validation = tuple(
        example
        for example in examples
        if validation_start <= example.feature_timestamp < test_start
        and example.label_timestamp < test_start
    )
    test = tuple(
        example for example in examples if example.feature_timestamp >= test_start
    )
    if not train or not validation or not test:
        raise ValueError("purged chronological split produced an empty partition")

    split = ChronologicalSplit(train=train, validation=validation, test=test)
    validate_chronological_split(split)
    return split


def validate_point_in_time_examples(examples: Sequence[PointInTimeExample]) -> None:
    """Fail closed on schema drift, malformed rows, and time leakage."""
    identities: set[tuple[str, str, datetime]] = set()
    for example in examples:
        if example.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version: {example.schema_version}")
        if len(example.features) != len(FEATURE_NAMES):
            raise ValueError("feature vector does not match schema")
        if len(example.labels) != len(LABEL_NAMES):
            raise ValueError("label vector does not match schema")
        if example.label_timestamp <= example.feature_timestamp:
            raise ValueError("label timestamp must be after feature timestamp")
        identity = (example.pair_id, example.direction_id, example.feature_timestamp)
        if identity in identities:
            raise ValueError(f"duplicate example identity: {identity}")
        identities.add(identity)


def validate_chronological_split(split: ChronologicalSplit) -> None:
    """Prove partitions are ordered and earlier labels cannot cross boundaries."""
    for name, partition in (
        ("train", split.train),
        ("validation", split.validation),
        ("test", split.test),
    ):
        if not partition:
            raise ValueError(f"{name} partition is empty")
        validate_point_in_time_examples(partition)

    validation_start = min(item.feature_timestamp for item in split.validation)
    test_start = min(item.feature_timestamp for item in split.test)
    if max(item.label_timestamp for item in split.train) >= validation_start:
        raise ValueError("train labels overlap the validation feature period")
    if max(item.label_timestamp for item in split.validation) >= test_start:
        raise ValueError("validation labels overlap the test feature period")
    if max(item.feature_timestamp for item in split.train) >= validation_start:
        raise ValueError("train features overlap the validation period")
    if max(item.feature_timestamp for item in split.validation) >= test_start:
        raise ValueError("validation features overlap the test period")


def _features(
    snapshot: Mapping[str, Any],
    direction: Mapping[str, Any],
    assumptions: CostAssumptions,
) -> tuple[float, ...]:
    token = str(direction["token"]).lower()
    buy_platform = str(direction["buy_platform"])
    sell_platform = str(direction["sell_platform"])
    buy_quote = _quote(snapshot, buy_platform, token)
    sell_quote = _quote(snapshot, sell_platform, token)
    buy_price = float(direction["buy_price"])
    sell_price = float(direction["sell_price"])
    gross_edge = sell_price - buy_price
    estimated_cost = _estimated_cost(
        buy_price,
        sell_price,
        buy_platform,
        sell_platform,
        assumptions,
    )
    observation_times = [
        parse_timestamp(snapshot["polymarket"]["timestamp"]),
        parse_timestamp(snapshot["kalshi"]["timestamp"]),
    ]
    return (
        buy_price,
        sell_price,
        float(buy_quote["ask_size"]),
        float(sell_quote["bid_size"]),
        float(buy_quote["spread"]),
        float(sell_quote["spread"]),
        gross_edge,
        estimated_cost,
        gross_edge - estimated_cost,
        min(float(buy_quote["ask_size"]), float(sell_quote["bid_size"])),
        abs((observation_times[1] - observation_times[0]).total_seconds()),
    )


def _labels(
    direction: Mapping[str, Any], assumptions: CostAssumptions
) -> tuple[float, float, bool]:
    buy_price = float(direction["buy_price"])
    sell_price = float(direction["sell_price"])
    future_gross_edge = sell_price - buy_price
    future_net_edge = future_gross_edge - _estimated_cost(
        buy_price,
        sell_price,
        str(direction["buy_platform"]),
        str(direction["sell_platform"]),
        assumptions,
    )
    return (future_gross_edge, future_net_edge, future_net_edge > 0)


def _estimated_cost(
    buy_price: float,
    sell_price: float,
    buy_platform: str,
    sell_platform: str,
    assumptions: CostAssumptions,
) -> float:
    return (
        buy_price * _fee_rate(assumptions, buy_platform)
        + sell_price * _fee_rate(assumptions, sell_platform)
        + 2 * assumptions.gas_cost_per_leg
        + 2 * assumptions.slippage_per_leg
    )


def _fee_rate(assumptions: CostAssumptions, platform: str) -> float:
    if platform == "polymarket":
        return assumptions.polymarket_taker_fee
    if platform == "kalshi":
        return assumptions.kalshi_taker_fee
    raise ValueError(f"unsupported platform: {platform}")


def _pair_id(snapshot: Mapping[str, Any]) -> str:
    pair = snapshot["pair"]
    return f"{pair['polymarket_id']}:{pair['kalshi_ticker']}"


def _validated_directions(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    directions = snapshot.get("directions")
    if not isinstance(directions, list):
        raise ValueError("snapshot directions must be a list")
    return directions


def _direction_id(direction: Mapping[str, Any]) -> str:
    return ":".join(
        (
            str(direction["token"]).upper(),
            str(direction["buy_platform"]),
            str(direction["sell_platform"]),
        )
    )


def _quote(snapshot: Mapping[str, Any], platform: str, token: str) -> Mapping[str, Any]:
    quote = snapshot[platform][token]
    required = ("ask_size", "bid_size", "spread")
    if any(quote.get(field) is None for field in required):
        raise _IncompleteQuoteError(f"incomplete {platform} {token.upper()} quote")
    return quote


def _first_snapshot_at_or_after(
    snapshots: Sequence[Mapping[str, Any]], start: int, target: datetime
) -> Mapping[str, Any] | None:
    for snapshot in snapshots[start:]:
        if parse_timestamp(snapshot["timestamp"]) >= target:
            return snapshot
    return None


def _example_sort_key(example: PointInTimeExample) -> tuple[datetime, str, str]:
    return (example.feature_timestamp, example.pair_id, example.direction_id)


class _IncompleteQuoteError(ValueError):
    pass
