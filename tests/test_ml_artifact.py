import hashlib
import json
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_ml_model import separable_split
from utils.ml_artifact import (
    ModelArtifactMetadata,
    create_model_artifact,
    deserialize_model_artifact,
    load_model_artifact,
    save_model_artifact,
    serialize_model_artifact,
)
from utils.ml_dataset import CostAssumptions
from utils.ml_model import TrainingConfig, train_and_evaluate


def artifact():
    split = separable_split()
    config = TrainingConfig(iterations=20, calibration_bins=5)
    model = train_and_evaluate(split, config).model
    metadata = ModelArtifactMetadata(
        created_at=datetime(2026, 6, 24, 13, tzinfo=timezone.utc),
        dataset_sha256="a" * 64,
        training_feature_start=split.train[0].feature_timestamp,
        training_feature_end=split.train[-1].feature_timestamp,
        training_label_end=split.train[-1].label_timestamp,
        cost_assumptions=CostAssumptions(
            polymarket_taker_fee=0.01,
            kalshi_taker_fee=0.02,
            gas_cost_per_leg=0.001,
            slippage_per_leg=0.002,
            decision_latency=timedelta(seconds=2),
            label_horizon=timedelta(seconds=30),
            max_label_delay=timedelta(seconds=5),
        ),
        training_config=config,
    )
    return create_model_artifact(model, metadata)


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def resign(envelope):
    envelope["payload_sha256"] = hashlib.sha256(
        canonical_json(envelope["payload"])
    ).hexdigest()
    return canonical_json(envelope) + b"\n"


def test_artifact_round_trip_is_deterministic_and_preserves_provenance():
    expected = artifact()

    first = serialize_model_artifact(expected)
    second = serialize_model_artifact(expected)
    loaded = deserialize_model_artifact(first)

    assert first == second
    assert loaded == expected
    assert loaded.metadata.dataset_sha256 == "a" * 64
    assert loaded.metadata.cost_assumptions.slippage_per_leg == 0.002
    assert loaded.model.predict_probability(separable_split().test[0]) == pytest.approx(
        expected.model.predict_probability(separable_split().test[0])
    )


def test_save_is_atomic_private_and_loads_regular_file(tmp_path):
    path = tmp_path / "ranker.json"

    save_model_artifact(path, artifact())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_model_artifact(path) == artifact()
    assert not list(tmp_path.glob(".ranker.json.*"))


def test_load_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_bytes(serialize_model_artifact(artifact()))
    link = tmp_path / "ranker.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="opened safely"):
        load_model_artifact(link)
    with pytest.raises(ValueError, match="symlink"):
        save_model_artifact(link, artifact())


def test_deserialize_rejects_checksum_tampering():
    envelope = json.loads(serialize_model_artifact(artifact()))
    envelope["payload"]["model"]["intercept"] += 1

    with pytest.raises(ValueError, match="checksum"):
        deserialize_model_artifact(canonical_json(envelope))


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda payload: payload.update({"unexpected": True}),
            "fields do not match",
        ),
        (
            lambda payload: payload.update({"artifact_format_version": "unsupported"}),
            "format version",
        ),
        (
            lambda payload: payload["model"].update(
                {"dataset_schema_version": "future-schema"}
            ),
            "schema version",
        ),
        (
            lambda payload: payload["model"].update({"training_examples": True}),
            "integer",
        ),
    ],
)
def test_deserialize_rejects_resigned_but_invalid_payloads(mutation, message):
    envelope = json.loads(serialize_model_artifact(artifact()))
    mutation(envelope["payload"])

    with pytest.raises(ValueError, match=message):
        deserialize_model_artifact(resign(envelope))


def test_create_rejects_unverifiable_or_future_dated_metadata():
    valid = artifact()

    with pytest.raises(ValueError, match="dataset_sha256"):
        create_model_artifact(
            valid.model, replace(valid.metadata, dataset_sha256="not-a-digest")
        )
    with pytest.raises(ValueError, match="creation cannot precede"):
        create_model_artifact(
            valid.model,
            replace(
                valid.metadata,
                created_at=valid.metadata.training_feature_start,
            ),
        )


def test_deserialize_rejects_noncanonical_encoding():
    envelope = json.loads(serialize_model_artifact(artifact()))
    pretty = json.dumps(envelope, indent=2).encode()

    with pytest.raises(ValueError, match="canonical"):
        deserialize_model_artifact(pretty)
