"""Fail-closed state model for one normalized two-venue execution.

This module deliberately contains no exchange I/O.  It models the state that a
future durable execution journal and reconciliation loop must persist before
either venue adapter may be allowed to mutate an account.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from enum import Enum

_IDEMPOTENCY_NAMESPACE = uuid.UUID("f6b28a28-8b72-4f22-aece-a30f9ec4ab79")
_SIZE_TOLERANCE = 1e-9


class LegSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def exposure_sign(self) -> int:
        return 1 if self is LegSide.BUY else -1


class LegPhase(str, Enum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    OPEN = "open"
    UNKNOWN = "unknown"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in {self.FILLED, self.CANCELLED, self.REJECTED}


class ExecutionPhase(str, Enum):
    PLANNED = "planned"
    IN_FLIGHT = "in_flight"
    RECOVERY_REQUIRED = "recovery_required"
    RESIDUAL_EXPOSURE = "residual_exposure"
    COMPLETE = "complete"


@dataclass(frozen=True)
class LegIntent:
    """Immutable venue intent expressed in normalized contract units."""

    leg_id: str
    venue: str
    market_id: str
    side: LegSide
    limit_price: float
    size: float

    def __post_init__(self) -> None:
        for field_name in ("leg_id", "venue", "market_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.side, LegSide):
            raise ValueError("side must be a LegSide")
        if (
            isinstance(self.limit_price, bool)
            or not math.isfinite(self.limit_price)
            or not 0 < self.limit_price < 1
        ):
            raise ValueError("limit_price must be finite and strictly between 0 and 1")
        if (
            isinstance(self.size, bool)
            or not math.isfinite(self.size)
            or self.size <= 0
        ):
            raise ValueError("size must be finite and positive")


@dataclass
class LegState:
    intent: LegIntent
    idempotency_key: str
    phase: LegPhase = LegPhase.PLANNED
    venue_order_id: str | None = None
    filled_size: float = 0.0

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.intent.size - self.filled_size)


class TwoLegExecution:
    """Aggregate state for two opposing, equal-sized normalized legs.

    Idempotency keys are UUIDv5 values derived only from immutable intent, so a
    restart reconstructing the same execution id and plan obtains the same
    per-leg keys.  Callers must persist the execution id before submission.
    """

    def __init__(self, execution_id: str, first: LegIntent, second: LegIntent):
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution_id must be a non-empty string")
        if first.leg_id == second.leg_id:
            raise ValueError("leg ids must be distinct")
        if first.venue.strip().lower() == second.venue.strip().lower():
            raise ValueError("two-leg execution requires distinct venues")
        if first.side is second.side:
            raise ValueError("two-leg execution requires opposing sides")
        if not math.isclose(
            first.size, second.size, rel_tol=0.0, abs_tol=_SIZE_TOLERANCE
        ):
            raise ValueError("two-leg execution requires equal normalized sizes")

        self.execution_id = execution_id
        self.legs = {
            intent.leg_id: LegState(
                intent=intent,
                idempotency_key=self._derive_idempotency_key(execution_id, intent),
            )
            for intent in (first, second)
        }

    @staticmethod
    def _derive_idempotency_key(execution_id: str, intent: LegIntent) -> str:
        identity = "\x1f".join(
            (
                execution_id,
                intent.leg_id,
                intent.venue.strip().lower(),
                intent.market_id,
            )
        )
        return str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, identity))

    def _leg(self, leg_id: str) -> LegState:
        try:
            return self.legs[leg_id]
        except KeyError as exc:
            raise ValueError(f"unknown leg_id: {leg_id}") from exc

    def start_submission(self, leg_id: str) -> None:
        leg = self._leg(leg_id)
        if leg.phase is not LegPhase.PLANNED:
            raise ValueError(f"cannot start submission from {leg.phase.value}")
        leg.phase = LegPhase.SUBMITTING

    def mark_submission_ambiguous(self, leg_id: str) -> None:
        leg = self._leg(leg_id)
        if leg.phase not in {LegPhase.SUBMITTING, LegPhase.OPEN}:
            raise ValueError(f"cannot mark {leg.phase.value} submission ambiguous")
        leg.phase = LegPhase.UNKNOWN

    def reconcile_leg(
        self,
        leg_id: str,
        *,
        phase: LegPhase,
        cumulative_filled_size: float,
        venue_order_id: str | None = None,
    ) -> None:
        """Apply one authoritative, cumulative venue observation.

        Fill quantities may only increase. Terminal venue state never reopens,
        though repeated terminal observations may disclose a later-indexed fill
        that raced with cancellation.
        """
        leg = self._leg(leg_id)
        if leg.phase is LegPhase.PLANNED:
            raise ValueError("cannot reconcile a leg before submission starts")
        if phase in {LegPhase.PLANNED, LegPhase.SUBMITTING, LegPhase.UNKNOWN}:
            raise ValueError("reconciliation phase must be an observed venue state")
        if isinstance(cumulative_filled_size, bool) or not math.isfinite(
            cumulative_filled_size
        ):
            raise ValueError("cumulative_filled_size must be finite")
        if cumulative_filled_size + _SIZE_TOLERANCE < leg.filled_size:
            raise ValueError("cumulative filled size cannot decrease")
        if cumulative_filled_size > leg.intent.size + _SIZE_TOLERANCE:
            raise ValueError("cumulative filled size exceeds intended size")
        if phase is LegPhase.FILLED and not math.isclose(
            cumulative_filled_size,
            leg.intent.size,
            rel_tol=0.0,
            abs_tol=_SIZE_TOLERANCE,
        ):
            raise ValueError("filled phase requires the full intended size")
        if phase is LegPhase.REJECTED and cumulative_filled_size != 0:
            raise ValueError("rejected leg cannot contain fills")
        if phase in {LegPhase.OPEN, LegPhase.FILLED, LegPhase.CANCELLED} and (
            not isinstance(venue_order_id, str) or not venue_order_id.strip()
        ):
            raise ValueError("observed accepted order requires a venue_order_id")
        if (
            leg.venue_order_id
            and venue_order_id
            and leg.venue_order_id != venue_order_id
        ):
            raise ValueError("venue_order_id cannot change")
        if leg.phase.is_terminal and phase is not leg.phase:
            raise ValueError("terminal leg cannot transition to a different phase")
        if leg.phase.is_terminal and phase is LegPhase.OPEN:
            raise ValueError("terminal leg cannot reopen")

        leg.filled_size = min(cumulative_filled_size, leg.intent.size)
        leg.venue_order_id = leg.venue_order_id or venue_order_id
        leg.phase = phase

    @property
    def realized_residual_size(self) -> float:
        return sum(
            leg.intent.side.exposure_sign * leg.filled_size
            for leg in self.legs.values()
        )

    @property
    def potential_residual_range(self) -> tuple[float, float]:
        """Return min/max residual if every possibly-live remainder fills."""
        minimum = maximum = self.realized_residual_size
        for leg in self.legs.values():
            if leg.phase in {LegPhase.SUBMITTING, LegPhase.OPEN, LegPhase.UNKNOWN}:
                signed_remaining = leg.intent.side.exposure_sign * leg.remaining_size
                minimum += min(0.0, signed_remaining)
                maximum += max(0.0, signed_remaining)
        return minimum, maximum

    @property
    def phase(self) -> ExecutionPhase:
        legs = tuple(self.legs.values())
        if any(leg.phase is LegPhase.UNKNOWN for leg in legs):
            return ExecutionPhase.RECOVERY_REQUIRED
        if not math.isclose(
            self.realized_residual_size, 0.0, rel_tol=0.0, abs_tol=_SIZE_TOLERANCE
        ):
            return ExecutionPhase.RESIDUAL_EXPOSURE
        if all(leg.phase is LegPhase.PLANNED for leg in legs):
            return ExecutionPhase.PLANNED
        if all(leg.phase.is_terminal for leg in legs):
            return ExecutionPhase.COMPLETE
        return ExecutionPhase.IN_FLIGHT
