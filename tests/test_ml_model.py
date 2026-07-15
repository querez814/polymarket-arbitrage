from datetime import datetime, timedelta, timezone

import pytest

from utils.ml_dataset import (
    FEATURE_NAMES,
    SCHEMA_VERSION,
    ChronologicalSplit,
    PointInTimeExample,
)
from utils.ml_model import (
    TrainingConfig,
    evaluate_probabilities,
    train_and_evaluate,
)


def example(minute: int, signal: float, positive: bool) -> PointInTimeExample:
    feature_timestamp = datetime(2026, 6, 24, 12, minute, tzinfo=timezone.utc)
    features = (signal,) + (0.0,) * (len(FEATURE_NAMES) - 1)
    future_edge = 0.02 if positive else -0.02
    return PointInTimeExample(
        schema_version=SCHEMA_VERSION,
        pair_id="poly:kalshi",
        direction_id="YES:polymarket:kalshi",
        feature_timestamp=feature_timestamp,
        label_timestamp=feature_timestamp + timedelta(seconds=30),
        features=features,
        labels=(future_edge, future_edge, positive),
    )


def separable_split() -> ChronologicalSplit:
    train = tuple(
        example(minute, -2.0 if minute % 2 == 0 else 2.0, minute % 2 == 1)
        for minute in range(8)
    )
    validation = tuple(
        example(minute, -1.5 if minute % 2 == 0 else 1.5, minute % 2 == 1)
        for minute in range(9, 13)
    )
    test = tuple(
        example(minute, -1.0 if minute % 2 == 0 else 1.0, minute % 2 == 1)
        for minute in range(14, 18)
    )
    return ChronologicalSplit(train=train, validation=validation, test=test)


def test_training_is_deterministic_and_uses_training_prevalence_baseline():
    split = separable_split()
    config = TrainingConfig(iterations=500, calibration_bins=5)

    first = train_and_evaluate(split, config)
    second = train_and_evaluate(split, config)

    assert first == second
    assert first.model.training_examples == len(split.train)
    assert first.model.training_prevalence == 0.5
    assert (
        first.validation.model.brier_score
        < first.validation.prevalence_baseline.brier_score
    )
    assert first.test.model.roc_auc == 1.0
    assert first.validation.prevalence_baseline.roc_auc == 0.5


def test_metrics_report_calibration_bins_and_handle_single_class_auc():
    rows = [example(0, 1.0, True), example(1, 2.0, True)]

    metrics = evaluate_probabilities(rows, [0.7, 0.9], calibration_bins=5)

    assert metrics.roc_auc is None
    assert metrics.brier_score == pytest.approx(0.05)
    assert metrics.expected_calibration_error == pytest.approx(0.2)
    assert sum(item.sample_count for item in metrics.calibration_bins) == 2


def test_training_fails_closed_when_training_partition_has_one_class():
    split = separable_split()
    one_class_train = tuple(example(index, 1.0, True) for index in range(8))

    with pytest.raises(ValueError, match="both classes"):
        train_and_evaluate(
            ChronologicalSplit(
                train=one_class_train,
                validation=split.validation,
                test=split.test,
            )
        )


@pytest.mark.parametrize("probability", [-0.01, 1.01, float("nan")])
def test_metrics_reject_invalid_probabilities(probability):
    with pytest.raises(ValueError, match="within"):
        evaluate_probabilities([example(0, 1.0, True)], [probability])


@pytest.mark.parametrize("field", ["iterations", "calibration_bins"])
@pytest.mark.parametrize("value", [True, 1.5, 0])
def test_training_config_rejects_non_positive_integers(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        TrainingConfig(**{field: value})


def test_inference_rejects_unknown_model_version():
    trained = train_and_evaluate(separable_split(), TrainingConfig(iterations=10)).model
    unsupported = trained.__class__(
        **{**trained.__dict__, "model_version": "unknown-model"}
    )

    with pytest.raises(ValueError, match="version"):
        unsupported.predict_probability(separable_split().test[0])
