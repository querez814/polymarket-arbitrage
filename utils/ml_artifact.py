"""Versioned, integrity-checked persistence for G14 model artifacts.

Artifacts contain only an offline-trained ranking model and its provenance.  Loading
an artifact never authorizes execution; any inference use must remain subordinate to
deterministic opportunity and risk gates.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Mapping

from utils.ml_dataset import CostAssumptions, FEATURE_NAMES, SCHEMA_VERSION
from utils.ml_model import (
    MODEL_VERSION,
    LogisticEdgeModel,
    TrainingConfig,
)

ARTIFACT_FORMAT_VERSION = "logistic-edge-artifact-v1"
MODEL_PURPOSE = "rank-future-observed-cost-adjusted-quote-edge"
MAX_ARTIFACT_BYTES = 1_000_000


@dataclass(frozen=True)
class ModelArtifactMetadata:
    """Point-in-time training provenance required to audit an artifact."""

    created_at: datetime
    dataset_sha256: str
    training_feature_start: datetime
    training_feature_end: datetime
    training_label_end: datetime
    cost_assumptions: CostAssumptions
    training_config: TrainingConfig


@dataclass(frozen=True)
class ModelArtifact:
    artifact_format_version: str
    model_purpose: str
    metadata: ModelArtifactMetadata
    model: LogisticEdgeModel


def create_model_artifact(
    model: LogisticEdgeModel, metadata: ModelArtifactMetadata
) -> ModelArtifact:
    """Create and validate an in-memory artifact before it can be persisted."""
    artifact = ModelArtifact(
        artifact_format_version=ARTIFACT_FORMAT_VERSION,
        model_purpose=MODEL_PURPOSE,
        metadata=metadata,
        model=model,
    )
    _validate_artifact(artifact)
    return artifact


def serialize_model_artifact(artifact: ModelArtifact) -> bytes:
    """Return canonical JSON with a checksum over the complete artifact payload."""
    _validate_artifact(artifact)
    payload = _artifact_payload(artifact)
    payload_bytes = _canonical_json(payload)
    envelope = {
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
    encoded = _canonical_json(envelope) + b"\n"
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("model artifact exceeds the maximum size")
    return encoded


def deserialize_model_artifact(encoded: bytes) -> ModelArtifact:
    """Strictly load canonical artifact bytes, rejecting drift or corruption."""
    if not encoded or len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("model artifact size is invalid")
    try:
        envelope = json.loads(
            encoded,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("model artifact is not valid JSON") from exc
    envelope = _require_mapping(envelope, "artifact envelope")
    _require_exact_keys(envelope, {"payload", "payload_sha256"}, "artifact envelope")
    payload = _require_mapping(envelope["payload"], "artifact payload")
    digest = _require_string(envelope["payload_sha256"], "payload_sha256")
    if not _is_sha256(digest):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
    expected = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise ValueError("model artifact checksum mismatch")
    artifact = _artifact_from_payload(payload)
    _validate_artifact(artifact)
    if serialize_model_artifact(artifact) != encoded:
        raise ValueError("model artifact is not canonical")
    return artifact


def save_model_artifact(path: Path, artifact: ModelArtifact) -> None:
    """Atomically persist a private artifact without following destination symlinks."""
    encoded = serialize_model_artifact(artifact)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("refusing to replace a model artifact symlink")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def load_model_artifact(path: Path) -> ModelArtifact:
    """Read a bounded regular file without following symlinks, then validate it."""
    artifact_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact_path, flags)
    except OSError as exc:
        raise ValueError("model artifact cannot be opened safely") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("model artifact must be a regular file")
        if file_stat.st_size <= 0 or file_stat.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("model artifact size is invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            encoded = handle.read(MAX_ARTIFACT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return deserialize_model_artifact(encoded)


def _artifact_payload(artifact: ModelArtifact) -> dict[str, Any]:
    metadata = artifact.metadata
    assumptions = metadata.cost_assumptions
    config = metadata.training_config
    model = artifact.model
    return {
        "artifact_format_version": artifact.artifact_format_version,
        "model_purpose": artifact.model_purpose,
        "metadata": {
            "created_at": _format_datetime(metadata.created_at),
            "dataset_sha256": metadata.dataset_sha256,
            "training_feature_start": _format_datetime(metadata.training_feature_start),
            "training_feature_end": _format_datetime(metadata.training_feature_end),
            "training_label_end": _format_datetime(metadata.training_label_end),
            "cost_assumptions": {
                "polymarket_taker_fee": assumptions.polymarket_taker_fee,
                "kalshi_taker_fee": assumptions.kalshi_taker_fee,
                "gas_cost_per_leg": assumptions.gas_cost_per_leg,
                "slippage_per_leg": assumptions.slippage_per_leg,
                "decision_latency_seconds": assumptions.decision_latency.total_seconds(),
                "label_horizon_seconds": assumptions.label_horizon.total_seconds(),
                "max_label_delay_seconds": assumptions.max_label_delay.total_seconds(),
            },
            "training_config": {
                "learning_rate": config.learning_rate,
                "iterations": config.iterations,
                "l2_penalty": config.l2_penalty,
                "calibration_bins": config.calibration_bins,
            },
        },
        "model": {
            "model_version": model.model_version,
            "dataset_schema_version": model.dataset_schema_version,
            "feature_names": list(model.feature_names),
            "feature_means": list(model.feature_means),
            "feature_scales": list(model.feature_scales),
            "weights": list(model.weights),
            "intercept": model.intercept,
            "training_prevalence": model.training_prevalence,
            "training_examples": model.training_examples,
        },
    }


def _artifact_from_payload(payload: Mapping[str, Any]) -> ModelArtifact:
    _require_exact_keys(
        payload,
        {"artifact_format_version", "model_purpose", "metadata", "model"},
        "artifact payload",
    )
    metadata = _require_mapping(payload["metadata"], "artifact metadata")
    _require_exact_keys(
        metadata,
        {
            "created_at",
            "dataset_sha256",
            "training_feature_start",
            "training_feature_end",
            "training_label_end",
            "cost_assumptions",
            "training_config",
        },
        "artifact metadata",
    )
    costs = _require_mapping(metadata["cost_assumptions"], "cost assumptions")
    _require_exact_keys(
        costs,
        {
            "polymarket_taker_fee",
            "kalshi_taker_fee",
            "gas_cost_per_leg",
            "slippage_per_leg",
            "decision_latency_seconds",
            "label_horizon_seconds",
            "max_label_delay_seconds",
        },
        "cost assumptions",
    )
    config = _require_mapping(metadata["training_config"], "training config")
    _require_exact_keys(
        config,
        {"learning_rate", "iterations", "l2_penalty", "calibration_bins"},
        "training config",
    )
    model = _require_mapping(payload["model"], "model")
    _require_exact_keys(
        model,
        {
            "model_version",
            "dataset_schema_version",
            "feature_names",
            "feature_means",
            "feature_scales",
            "weights",
            "intercept",
            "training_prevalence",
            "training_examples",
        },
        "model",
    )
    return ModelArtifact(
        artifact_format_version=_require_string(
            payload["artifact_format_version"], "artifact_format_version"
        ),
        model_purpose=_require_string(payload["model_purpose"], "model_purpose"),
        metadata=ModelArtifactMetadata(
            created_at=_parse_datetime(metadata["created_at"], "created_at"),
            dataset_sha256=_require_string(
                metadata["dataset_sha256"], "dataset_sha256"
            ),
            training_feature_start=_parse_datetime(
                metadata["training_feature_start"], "training_feature_start"
            ),
            training_feature_end=_parse_datetime(
                metadata["training_feature_end"], "training_feature_end"
            ),
            training_label_end=_parse_datetime(
                metadata["training_label_end"], "training_label_end"
            ),
            cost_assumptions=CostAssumptions(
                polymarket_taker_fee=_require_number(
                    costs["polymarket_taker_fee"], "polymarket_taker_fee"
                ),
                kalshi_taker_fee=_require_number(
                    costs["kalshi_taker_fee"], "kalshi_taker_fee"
                ),
                gas_cost_per_leg=_require_number(
                    costs["gas_cost_per_leg"], "gas_cost_per_leg"
                ),
                slippage_per_leg=_require_number(
                    costs["slippage_per_leg"], "slippage_per_leg"
                ),
                decision_latency=timedelta(
                    seconds=_require_number(
                        costs["decision_latency_seconds"], "decision_latency_seconds"
                    )
                ),
                label_horizon=timedelta(
                    seconds=_require_number(
                        costs["label_horizon_seconds"], "label_horizon_seconds"
                    )
                ),
                max_label_delay=timedelta(
                    seconds=_require_number(
                        costs["max_label_delay_seconds"], "max_label_delay_seconds"
                    )
                ),
            ),
            training_config=TrainingConfig(
                learning_rate=_require_number(config["learning_rate"], "learning_rate"),
                iterations=_require_integer(config["iterations"], "iterations"),
                l2_penalty=_require_number(config["l2_penalty"], "l2_penalty"),
                calibration_bins=_require_integer(
                    config["calibration_bins"], "calibration_bins"
                ),
            ),
        ),
        model=LogisticEdgeModel(
            model_version=_require_string(model["model_version"], "model_version"),
            dataset_schema_version=_require_string(
                model["dataset_schema_version"], "dataset_schema_version"
            ),
            feature_names=tuple(
                _require_string(item, "feature name")
                for item in _require_list(model["feature_names"], "feature_names")
            ),
            feature_means=tuple(
                _require_number(item, "feature mean")
                for item in _require_list(model["feature_means"], "feature_means")
            ),
            feature_scales=tuple(
                _require_number(item, "feature scale")
                for item in _require_list(model["feature_scales"], "feature_scales")
            ),
            weights=tuple(
                _require_number(item, "weight")
                for item in _require_list(model["weights"], "weights")
            ),
            intercept=_require_number(model["intercept"], "intercept"),
            training_prevalence=_require_number(
                model["training_prevalence"], "training_prevalence"
            ),
            training_examples=_require_integer(
                model["training_examples"], "training_examples"
            ),
        ),
    )


def _validate_artifact(artifact: ModelArtifact) -> None:
    if artifact.artifact_format_version != ARTIFACT_FORMAT_VERSION:
        raise ValueError("artifact format version is unsupported")
    if artifact.model_purpose != MODEL_PURPOSE:
        raise ValueError("model purpose is unsupported")
    model = artifact.model
    if model.model_version != MODEL_VERSION:
        raise ValueError("model version is unsupported")
    if model.dataset_schema_version != SCHEMA_VERSION:
        raise ValueError("dataset schema version is unsupported")
    if model.feature_names != FEATURE_NAMES:
        raise ValueError("model feature schema is unsupported")
    if not (
        len(model.feature_means)
        == len(model.feature_scales)
        == len(model.weights)
        == len(FEATURE_NAMES)
    ):
        raise ValueError("model dimensions do not match the feature schema")
    numeric_model_values = (
        *model.feature_means,
        *model.feature_scales,
        *model.weights,
        model.intercept,
        model.training_prevalence,
    )
    if any(not isfinite(value) for value in numeric_model_values):
        raise ValueError("model contains a non-finite value")
    if any(scale <= 0 for scale in model.feature_scales):
        raise ValueError("model normalization scale must be positive")
    if not 0 < model.training_prevalence < 1:
        raise ValueError("model training prevalence must be within (0, 1)")
    if (
        not isinstance(model.training_examples, int)
        or isinstance(model.training_examples, bool)
        or model.training_examples <= 1
    ):
        raise ValueError("model training example count is invalid")

    metadata = artifact.metadata
    for name in (
        "created_at",
        "training_feature_start",
        "training_feature_end",
        "training_label_end",
    ):
        value = getattr(metadata, name)
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be UTC")
    if metadata.training_feature_start > metadata.training_feature_end:
        raise ValueError("training feature period is reversed")
    if metadata.training_label_end <= metadata.training_feature_end:
        raise ValueError("training label end must follow the final feature")
    if metadata.created_at < metadata.training_label_end:
        raise ValueError("artifact creation cannot precede its training labels")
    if not _is_sha256(metadata.dataset_sha256):
        raise ValueError("dataset_sha256 must be a lowercase SHA-256 digest")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("model artifact cannot be encoded canonically") from exc


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, name: str) -> datetime:
    text = _require_string(value, name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must use canonical UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid datetime") from exc
    if _format_datetime(parsed) != text:
        raise ValueError(f"{name} is not canonical")
    return parsed


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], name: str
) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} fields do not match the artifact schema")


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _require_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value
