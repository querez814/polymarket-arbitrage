from dataclasses import replace

import pytest

from tests.test_ml_artifact import artifact
from tests.test_ml_model import separable_split
from utils.ml_artifact import save_model_artifact
from utils.ml_inference import (
    InferenceCandidate,
    InferencePolicyConfig,
    SubordinateInferencePolicy,
)


def candidate(index=0, *, eligible=True, size_cap=100.0):
    observation = separable_split().test[index].inference_features()
    return InferenceCandidate(observation, eligible, size_cap)


def test_inference_input_contains_no_future_label_fields():
    observation = candidate().observation

    assert not hasattr(observation, "labels")
    assert not hasattr(observation, "label_timestamp")


def test_model_cannot_override_deterministic_rejection():
    policy = SubordinateInferencePolicy(artifact())

    decision = policy.evaluate(candidate(1, eligible=False))

    assert decision.approved is False
    assert decision.probability is None
    assert decision.size_cap == 0
    assert decision.reason == "deterministic_gate_rejected"


def test_model_can_only_reduce_deterministic_size_cap():
    policy = SubordinateInferencePolicy(
        artifact(),
        InferencePolicyConfig(minimum_probability=0, maximum_size_fraction=0.5),
    )

    decision = policy.evaluate(candidate(1, size_cap=40))

    assert decision.approved is True
    assert 0 < decision.size_cap <= 20


def test_threshold_filters_low_ranked_observation():
    policy = SubordinateInferencePolicy(
        artifact(), InferencePolicyConfig(minimum_probability=0.9)
    )

    decision = policy.evaluate(candidate(0))

    assert decision.approved is False
    assert decision.size_cap == 0
    assert decision.reason == "below_probability_threshold"


def test_corrupt_or_missing_artifact_loads_as_rejecting_policy(tmp_path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not an artifact")

    for path in (corrupt, tmp_path / "missing.json"):
        decision = SubordinateInferencePolicy.load(path).evaluate(candidate(1))
        assert decision.approved is False
        assert decision.reason == "model_artifact_unavailable"


def test_valid_artifact_loads_and_ranks_deterministically(tmp_path):
    path = tmp_path / "model.json"
    save_model_artifact(path, artifact())
    policy = SubordinateInferencePolicy.load(
        path, InferencePolicyConfig(minimum_probability=0)
    )

    ranked = policy.rank((candidate(0), candidate(1), candidate(1, eligible=False)))

    assert [item.approved for item in ranked] == [True, True, False]
    assert ranked[0].probability >= ranked[1].probability


@pytest.mark.parametrize("size_cap", [True, 0, -1, float("nan"), float("inf")])
def test_invalid_deterministic_size_cap_fails_closed(size_cap):
    decision = SubordinateInferencePolicy(artifact()).evaluate(
        candidate(1, size_cap=size_cap)
    )

    assert decision.approved is False
    assert decision.reason == "invalid_size_cap"


def test_schema_drift_fails_closed_at_inference():
    malformed = replace(candidate(1).observation, schema_version="future")

    decision = SubordinateInferencePolicy(artifact()).evaluate(
        InferenceCandidate(malformed, True, 10)
    )

    assert decision.approved is False
    assert decision.reason == "model_inference_failed"


def test_artifact_cannot_score_observation_from_its_training_period():
    training_observation = separable_split().train[0].inference_features()

    decision = SubordinateInferencePolicy(artifact()).evaluate(
        InferenceCandidate(training_observation, True, 10)
    )

    assert decision.approved is False
    assert decision.probability is None
    assert decision.reason == "artifact_not_point_in_time"


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_probability", True),
        ("minimum_probability", -0.1),
        ("maximum_size_fraction", True),
        ("maximum_size_fraction", 0),
    ],
)
def test_invalid_policy_configuration_is_rejected(field, value):
    with pytest.raises(ValueError):
        InferencePolicyConfig(**{field: value})
