"""Durable, fail-closed execution of one locked cross-venue arbitrage pair."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from core.execution_journal import ExecutionJournal
from core.execution_recovery import AuthoritativeOrder
from core.two_leg_execution import LegIntent, LegPhase, TwoLegExecution


class VenueMutationAmbiguousError(RuntimeError):
    """The venue may have accepted a mutation; authoritative recovery is required."""


class VenueExecutionError(RuntimeError):
    """The locked-arbitrage execution contract was violated."""


@dataclass(frozen=True)
class PreparedVenueOrder:
    """A non-mutating, venue-specific IOC order prepared for one durable leg."""

    venue: str
    market_id: str
    idempotency_key: str
    requested_size: float
    venue_order_id: str | None
    payload: Any


class VenueExecutionAdapter(Protocol):
    async def available_collateral(self) -> float | None: ...

    async def prepare_ioc(
        self,
        intent: LegIntent,
        *,
        idempotency_key: str,
        size: float,
    ) -> PreparedVenueOrder: ...

    async def submit_prepared(
        self, prepared: PreparedVenueOrder
    ) -> AuthoritativeOrder: ...

    async def cancel_open(
        self, order: AuthoritativeOrder
    ) -> AuthoritativeOrder: ...


class LockedArbitrageExecutor:
    """Execute scarce-leg-first IOC, then hedge only its confirmed fill.

    The immutable two-leg plan and stable idempotency keys must already be in
    the journal. Every state transition is committed before the next possible
    account mutation. Ambiguous mutations are never retried here.
    """

    def __init__(
        self,
        journal: ExecutionJournal,
        adapters: Mapping[str, VenueExecutionAdapter],
    ) -> None:
        self._journal = journal
        self._adapters = {
            venue.strip().lower(): adapter for venue, adapter in adapters.items()
        }
        if not self._adapters or len(self._adapters) != len(adapters):
            raise ValueError("venue adapters must have unique non-empty names")

    async def execute(
        self, execution_id: str, *, first_leg_id: str
    ) -> TwoLegExecution:
        execution = self._journal.load_execution(execution_id)
        if any(leg.phase is not LegPhase.PLANNED for leg in execution.legs.values()):
            raise VenueExecutionError("execution must be pristine before submission")
        try:
            first = execution.legs[first_leg_id]
        except KeyError as exc:
            raise ValueError(f"unknown first_leg_id: {first_leg_id}") from exc
        second_leg_id = next(
            leg_id for leg_id in execution.legs if leg_id != first_leg_id
        )

        execution = await self._submit_leg(
            execution_id,
            first_leg_id,
            requested_size=first.intent.size,
        )
        first = execution.legs[first_leg_id]
        if first.phase is LegPhase.UNKNOWN:
            return execution
        if first.phase is LegPhase.OPEN:
            execution = await self._cancel_open(execution_id, first_leg_id)
            first = execution.legs[first_leg_id]
            if first.phase is LegPhase.UNKNOWN:
                return execution
        if not first.phase.is_terminal:
            raise VenueExecutionError("first IOC leg did not reach a terminal state")

        hedge_size = first.filled_size
        if hedge_size == 0:
            return self._journal.skip_unsubmitted_leg(execution_id, second_leg_id)

        execution = await self._submit_leg(
            execution_id,
            second_leg_id,
            requested_size=hedge_size,
        )
        second = execution.legs[second_leg_id]
        if second.phase is LegPhase.UNKNOWN:
            return execution
        if second.phase is LegPhase.OPEN:
            execution = await self._cancel_open(execution_id, second_leg_id)
        return execution

    async def _submit_leg(
        self,
        execution_id: str,
        leg_id: str,
        *,
        requested_size: float,
    ) -> TwoLegExecution:
        execution = self._journal.start_submission(execution_id, leg_id)
        leg = execution.legs[leg_id]
        adapter = self._adapter_for(leg.intent.venue)
        prepared = await adapter.prepare_ioc(
            leg.intent,
            idempotency_key=leg.idempotency_key,
            size=requested_size,
        )
        self._validate_prepared(leg.intent, leg.idempotency_key, requested_size, prepared)
        if prepared.venue_order_id:
            execution = self._journal.record_prepared_order_id(
                execution_id, leg_id, prepared.venue_order_id
            )
        try:
            observed = await adapter.submit_prepared(prepared)
        except VenueMutationAmbiguousError:
            return self._journal.mark_submission_ambiguous(execution_id, leg_id)
        self._validate_observation(
            leg.intent, leg.idempotency_key, requested_size, observed
        )
        phase = observed.phase
        if phase is LegPhase.FILLED and requested_size < leg.intent.size:
            # The IOC filled its entire reduced hedge request, not the original
            # full-size immutable plan.
            phase = LegPhase.CANCELLED
        return self._journal.reconcile_leg(
            execution_id,
            leg_id,
            phase=phase,
            cumulative_filled_size=observed.cumulative_filled_size,
            venue_order_id=observed.venue_order_id,
        )

    async def _cancel_open(
        self, execution_id: str, leg_id: str
    ) -> TwoLegExecution:
        execution = self._journal.load_execution(execution_id)
        leg = execution.legs[leg_id]
        assert leg.venue_order_id is not None
        current = AuthoritativeOrder(
            venue=leg.intent.venue,
            market_id=leg.intent.market_id,
            idempotency_key=leg.idempotency_key,
            venue_order_id=leg.venue_order_id,
            phase=leg.phase,
            cumulative_filled_size=leg.filled_size,
        )
        try:
            observed = await self._adapter_for(leg.intent.venue).cancel_open(current)
        except VenueMutationAmbiguousError:
            return self._journal.mark_submission_ambiguous(execution_id, leg_id)
        self._validate_observation(
            leg.intent, leg.idempotency_key, leg.intent.size, observed
        )
        if observed.phase is LegPhase.OPEN:
            raise VenueExecutionError("cancel returned an open order")
        return self._journal.reconcile_leg(
            execution_id,
            leg_id,
            phase=observed.phase,
            cumulative_filled_size=observed.cumulative_filled_size,
            venue_order_id=observed.venue_order_id,
        )

    def _adapter_for(self, venue: str) -> VenueExecutionAdapter:
        try:
            return self._adapters[venue.strip().lower()]
        except KeyError as exc:
            raise VenueExecutionError(f"missing venue adapter for {venue}") from exc

    @staticmethod
    def _validate_prepared(
        intent: LegIntent,
        idempotency_key: str,
        requested_size: float,
        prepared: PreparedVenueOrder,
    ) -> None:
        if prepared.venue.strip().lower() != intent.venue.strip().lower():
            raise VenueExecutionError("prepared order venue mismatch")
        if prepared.market_id != intent.market_id:
            raise VenueExecutionError("prepared order market mismatch")
        if prepared.idempotency_key != idempotency_key:
            raise VenueExecutionError("prepared order idempotency mismatch")
        if prepared.requested_size != requested_size:
            raise VenueExecutionError("prepared order size mismatch")

    @staticmethod
    def _validate_observation(
        intent: LegIntent,
        idempotency_key: str,
        requested_size: float,
        observed: AuthoritativeOrder,
    ) -> None:
        if observed.venue.strip().lower() != intent.venue.strip().lower():
            raise VenueExecutionError("venue observation mismatch")
        if observed.market_id != intent.market_id:
            raise VenueExecutionError("market observation mismatch")
        if observed.idempotency_key != idempotency_key:
            raise VenueExecutionError("idempotency observation mismatch")
        if observed.cumulative_filled_size > requested_size:
            raise VenueExecutionError("venue filled more than requested")
