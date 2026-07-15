"""Fail-closed G14 inference subordinate to deterministic trading controls.

This module does not authorize execution.  Callers must first apply their
deterministic opportunity and risk gates and pass the resulting eligibility and
size cap.  The model can only reject an eligible candidate or reduce that cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Sequence

from utils.ml_artifact import ModelArtifact, load_model_artifact
from utils.ml_dataset import InferenceFeatures, validate_inference_features


@dataclass(frozen=True)
class InferencePolicyConfig:
    minimum_probability: float = 0.5
    maximum_size_fraction: float = 1.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_probability, bool)
            or not isfinite(self.minimum_probability)
            or not 0 <= self.minimum_probability <= 1
        ):
            raise ValueError("minimum_probability must be finite and within [0, 1]")
        if (
            isinstance(self.maximum_size_fraction, bool)
            or not isfinite(self.maximum_size_fraction)
            or not 0 < self.maximum_size_fraction <= 1
        ):
            raise ValueError("maximum_size_fraction must be finite and within (0, 1]")


@dataclass(frozen=True)
class InferenceCandidate:
    observation: InferenceFeatures
    deterministic_eligible: bool
    deterministic_size_cap: float


@dataclass(frozen=True)
class InferenceDecision:
    pair_id: str
    direction_id: str
    observed_at: datetime
    approved: bool
    probability: float | None
    size_cap: float
    reason: str


class SubordinateInferencePolicy:
    """Ranks, filters, and downsizes only after deterministic approval."""

    def __init__(
        self,
        artifact: ModelArtifact | None,
        config: InferencePolicyConfig = InferencePolicyConfig(),
        *,
        unavailable_reason: str | None = None,
    ) -> None:
        if (artifact is None) != (unavailable_reason is not None):
            raise ValueError("unavailable policy state is inconsistent")
        self._artifact = artifact
        self._config = config
        self._unavailable_reason = unavailable_reason

    @classmethod
    def load(
        cls,
        path: Path,
        config: InferencePolicyConfig = InferencePolicyConfig(),
    ) -> "SubordinateInferencePolicy":
        """Load safely; any artifact failure yields a rejecting policy."""
        try:
            artifact = load_model_artifact(path)
        except (OSError, ValueError):
            return cls(None, config, unavailable_reason="model_artifact_unavailable")
        return cls(artifact, config)

    def evaluate(self, candidate: InferenceCandidate) -> InferenceDecision:
        identity = _identity(candidate.observation)
        if not isinstance(candidate.deterministic_eligible, bool):
            return InferenceDecision(*identity, False, None, 0.0, "invalid_gate_result")
        if (
            isinstance(candidate.deterministic_size_cap, bool)
            or not isfinite(candidate.deterministic_size_cap)
            or candidate.deterministic_size_cap <= 0
        ):
            return InferenceDecision(*identity, False, None, 0.0, "invalid_size_cap")
        if not candidate.deterministic_eligible:
            return InferenceDecision(
                *identity, False, None, 0.0, "deterministic_gate_rejected"
            )
        if self._artifact is None:
            return InferenceDecision(
                *identity,
                False,
                None,
                0.0,
                self._unavailable_reason or "model_unavailable",
            )
        try:
            validate_inference_features(candidate.observation)
            if (
                candidate.observation.observed_at
                <= self._artifact.metadata.training_label_end
            ):
                return InferenceDecision(
                    *identity, False, None, 0.0, "artifact_not_point_in_time"
                )
            probability = self._artifact.model.predict_features(candidate.observation)
        except ValueError:
            return InferenceDecision(
                *identity, False, None, 0.0, "model_inference_failed"
            )
        if probability < self._config.minimum_probability:
            return InferenceDecision(
                *identity, False, probability, 0.0, "below_probability_threshold"
            )
        size_cap = min(
            candidate.deterministic_size_cap,
            candidate.deterministic_size_cap
            * self._config.maximum_size_fraction
            * probability,
        )
        if not isfinite(size_cap) or size_cap <= 0:
            return InferenceDecision(
                *identity, False, probability, 0.0, "invalid_model_size"
            )
        return InferenceDecision(
            *identity, True, probability, size_cap, "model_approved"
        )

    def rank(
        self, candidates: Sequence[InferenceCandidate]
    ) -> tuple[InferenceDecision, ...]:
        """Return deterministic approved-first ranking without changing eligibility."""
        decisions = [self.evaluate(candidate) for candidate in candidates]
        return tuple(
            sorted(
                decisions,
                key=lambda decision: (
                    not decision.approved,
                    -(
                        decision.probability
                        if decision.probability is not None
                        else -1.0
                    ),
                    str(decision.observed_at),
                    decision.pair_id,
                    decision.direction_id,
                ),
            )
        )


def _identity(observation: InferenceFeatures) -> tuple[str, str, datetime]:
    return observation.pair_id, observation.direction_id, observation.observed_at
