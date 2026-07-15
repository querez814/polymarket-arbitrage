"""Deterministic training and honest offline evaluation for G14.

The model estimates whether a later observed top-of-book edge remains positive
after explicit costs.  It does not estimate fill probability or realized PnL,
and its output is not an execution authorization.  Any eventual inference use
must remain subordinate to deterministic opportunity and risk gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite, log, sqrt
from typing import Sequence

from utils.ml_dataset import (
    FEATURE_NAMES,
    SCHEMA_VERSION,
    ChronologicalSplit,
    InferenceFeatures,
    PointInTimeExample,
    validate_chronological_split,
    validate_inference_features,
    validate_point_in_time_examples,
)

MODEL_VERSION = "standardized-logistic-edge-v1"
_PROBABILITY_EPSILON = 1e-15


@dataclass(frozen=True)
class TrainingConfig:
    """Fixed optimization settings; training contains no random operations."""

    learning_rate: float = 0.05
    iterations: int = 2_000
    l2_penalty: float = 0.01
    calibration_bins: int = 10

    def __post_init__(self) -> None:
        if not isfinite(self.learning_rate) or self.learning_rate <= 0:
            raise ValueError("learning_rate must be finite and positive")
        if (
            not isinstance(self.iterations, int)
            or isinstance(self.iterations, bool)
            or self.iterations <= 0
        ):
            raise ValueError("iterations must be a positive integer")
        if not isfinite(self.l2_penalty) or self.l2_penalty < 0:
            raise ValueError("l2_penalty must be finite and non-negative")
        if (
            not isinstance(self.calibration_bins, int)
            or isinstance(self.calibration_bins, bool)
            or self.calibration_bins <= 0
        ):
            raise ValueError("calibration_bins must be a positive integer")


@dataclass(frozen=True)
class LogisticEdgeModel:
    """In-memory model whose schema is explicit and checked at inference."""

    model_version: str
    dataset_schema_version: str
    feature_names: tuple[str, ...]
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    weights: tuple[float, ...]
    intercept: float
    training_prevalence: float
    training_examples: int

    def predict_probability(self, example: PointInTimeExample) -> float:
        validate_point_in_time_examples((example,))
        return self.predict_features(example.inference_features())

    def predict_features(self, observation: InferenceFeatures) -> float:
        """Score a label-free point-in-time observation."""
        validate_inference_features(observation)
        if self.model_version != MODEL_VERSION:
            raise ValueError("model version is unsupported")
        if observation.schema_version != self.dataset_schema_version:
            raise ValueError("example schema does not match model schema")
        if self.feature_names != FEATURE_NAMES:
            raise ValueError("model feature schema is unsupported")
        if not (
            len(observation.features)
            == len(self.feature_means)
            == len(self.feature_scales)
            == len(self.weights)
        ):
            raise ValueError("model dimensions do not match example features")
        score = self.intercept
        for value, mean, scale, weight in zip(
            observation.features,
            self.feature_means,
            self.feature_scales,
            self.weights,
        ):
            if not isfinite(value):
                raise ValueError("example contains a non-finite feature")
            if not isfinite(mean) or not isfinite(scale) or scale <= 0:
                raise ValueError("model normalization is invalid")
            if not isfinite(weight):
                raise ValueError("model contains a non-finite weight")
            score += weight * ((value - mean) / scale)
        if not isfinite(score) or not isfinite(self.intercept):
            raise ValueError("model produced a non-finite score")
        return _sigmoid(score)


@dataclass(frozen=True)
class CalibrationBin:
    lower_bound: float
    upper_bound: float
    sample_count: int
    mean_probability: float
    positive_rate: float


@dataclass(frozen=True)
class BinaryMetrics:
    """Classification and calibration evidence, never a profitability claim."""

    sample_count: int
    positive_rate: float
    brier_score: float
    log_loss: float
    roc_auc: float | None
    expected_calibration_error: float
    calibration_bins: tuple[CalibrationBin, ...]


@dataclass(frozen=True)
class PartitionEvaluation:
    model: BinaryMetrics
    prevalence_baseline: BinaryMetrics


@dataclass(frozen=True)
class TrainingEvaluation:
    model: LogisticEdgeModel
    validation: PartitionEvaluation
    test: PartitionEvaluation


def train_logistic_edge_model(
    examples: Sequence[PointInTimeExample],
    config: TrainingConfig = TrainingConfig(),
) -> LogisticEdgeModel:
    """Fit a standardized logistic model using only the provided examples."""
    validate_point_in_time_examples(examples)
    if not examples:
        raise ValueError("training examples must not be empty")
    labels = [_binary_label(example) for example in examples]
    positives = sum(labels)
    if positives == 0 or positives == len(labels):
        raise ValueError("training labels must contain both classes")
    feature_count = len(FEATURE_NAMES)
    for example in examples:
        if len(example.features) != feature_count:
            raise ValueError("training example does not match feature schema")
        if any(not isfinite(value) for value in example.features):
            raise ValueError("training example contains a non-finite feature")

    means = tuple(
        sum(example.features[index] for example in examples) / len(examples)
        for index in range(feature_count)
    )
    scales = tuple(
        _population_scale(examples, index, means[index])
        for index in range(feature_count)
    )
    matrix = [
        tuple(
            (value - means[index]) / scales[index]
            for index, value in enumerate(example.features)
        )
        for example in examples
    ]
    prevalence = positives / len(labels)
    intercept = log(prevalence / (1 - prevalence))
    weights = [0.0] * feature_count
    for _ in range(config.iterations):
        intercept_gradient = 0.0
        weight_gradients = [0.0] * feature_count
        for row, label in zip(matrix, labels):
            error = (
                _sigmoid(intercept + sum(w * x for w, x in zip(weights, row))) - label
            )
            intercept_gradient += error
            for index, value in enumerate(row):
                weight_gradients[index] += error * value
        count = len(labels)
        intercept -= config.learning_rate * intercept_gradient / count
        for index in range(feature_count):
            gradient = weight_gradients[index] / count
            gradient += config.l2_penalty * weights[index]
            weights[index] -= config.learning_rate * gradient
        if not isfinite(intercept) or any(not isfinite(weight) for weight in weights):
            raise ValueError("model optimization diverged")

    return LogisticEdgeModel(
        model_version=MODEL_VERSION,
        dataset_schema_version=SCHEMA_VERSION,
        feature_names=FEATURE_NAMES,
        feature_means=means,
        feature_scales=scales,
        weights=tuple(weights),
        intercept=intercept,
        training_prevalence=prevalence,
        training_examples=len(examples),
    )


def train_and_evaluate(
    split: ChronologicalSplit,
    config: TrainingConfig = TrainingConfig(),
) -> TrainingEvaluation:
    """Train on train only, then compare model and baseline on later periods."""
    validate_chronological_split(split)
    model = train_logistic_edge_model(split.train, config)
    return TrainingEvaluation(
        model=model,
        validation=_evaluate_partition(model, split.validation, config),
        test=_evaluate_partition(model, split.test, config),
    )


def evaluate_probabilities(
    examples: Sequence[PointInTimeExample],
    probabilities: Sequence[float],
    *,
    calibration_bins: int = 10,
) -> BinaryMetrics:
    """Compute deterministic discrimination and calibration metrics."""
    validate_point_in_time_examples(examples)
    if not examples:
        raise ValueError("evaluation examples must not be empty")
    if len(examples) != len(probabilities):
        raise ValueError("probability count does not match example count")
    if (
        not isinstance(calibration_bins, int)
        or isinstance(calibration_bins, bool)
        or calibration_bins <= 0
    ):
        raise ValueError("calibration_bins must be a positive integer")
    labels = [_binary_label(example) for example in examples]
    checked_probabilities = []
    for probability in probabilities:
        if not isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("probabilities must be finite and within [0, 1]")
        checked_probabilities.append(probability)
    count = len(labels)
    brier = (
        sum(
            (probability - label) ** 2
            for probability, label in zip(checked_probabilities, labels)
        )
        / count
    )
    log_loss = (
        -sum(
            label * log(_clamp_probability(probability))
            + (1 - label) * log(_clamp_probability(1 - probability))
            for probability, label in zip(checked_probabilities, labels)
        )
        / count
    )
    bins = _calibration_bins(labels, checked_probabilities, calibration_bins)
    calibration_error = sum(
        item.sample_count * abs(item.mean_probability - item.positive_rate) / count
        for item in bins
    )
    return BinaryMetrics(
        sample_count=count,
        positive_rate=sum(labels) / count,
        brier_score=brier,
        log_loss=log_loss,
        roc_auc=_roc_auc(labels, checked_probabilities),
        expected_calibration_error=calibration_error,
        calibration_bins=bins,
    )


def _evaluate_partition(
    model: LogisticEdgeModel,
    examples: Sequence[PointInTimeExample],
    config: TrainingConfig,
) -> PartitionEvaluation:
    probabilities = [model.predict_probability(example) for example in examples]
    baseline = [model.training_prevalence] * len(examples)
    return PartitionEvaluation(
        model=evaluate_probabilities(
            examples, probabilities, calibration_bins=config.calibration_bins
        ),
        prevalence_baseline=evaluate_probabilities(
            examples, baseline, calibration_bins=config.calibration_bins
        ),
    )


def _binary_label(example: PointInTimeExample) -> int:
    label = example.labels[2]
    if not isinstance(label, bool):
        raise ValueError("positive-after-costs label must be Boolean")
    return int(label)


def _population_scale(
    examples: Sequence[PointInTimeExample], index: int, mean: float
) -> float:
    variance = sum((example.features[index] - mean) ** 2 for example in examples) / len(
        examples
    )
    scale = sqrt(variance)
    return scale if scale > 1e-12 else 1.0


def _sigmoid(score: float) -> float:
    if score >= 0:
        inverse = exp(-score)
        return 1 / (1 + inverse)
    positive = exp(score)
    return positive / (1 + positive)


def _clamp_probability(probability: float) -> float:
    return min(max(probability, _PROBABILITY_EPSILON), 1 - _PROBABILITY_EPSILON)


def _roc_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float | None:
    positives = [score for label, score in zip(labels, probabilities) if label == 1]
    negatives = [score for label, score in zip(labels, probabilities) if label == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            if positive > negative:
                wins += 1
            elif positive == negative:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def _calibration_bins(
    labels: Sequence[int], probabilities: Sequence[float], bin_count: int
) -> tuple[CalibrationBin, ...]:
    members: list[list[tuple[int, float]]] = [[] for _ in range(bin_count)]
    for label, probability in zip(labels, probabilities):
        index = min(int(probability * bin_count), bin_count - 1)
        members[index].append((label, probability))
    bins = []
    for index, rows in enumerate(members):
        if not rows:
            continue
        bins.append(
            CalibrationBin(
                lower_bound=index / bin_count,
                upper_bound=(index + 1) / bin_count,
                sample_count=len(rows),
                mean_probability=sum(row[1] for row in rows) / len(rows),
                positive_rate=sum(row[0] for row in rows) / len(rows),
            )
        )
    return tuple(bins)
