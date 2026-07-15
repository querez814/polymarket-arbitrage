"""Read-only authoritative restart reconciliation for two-leg executions.

The coordinator never places, retries, cancels, or hedges an order.  It reads
venue state through a narrow adapter contract, validates the complete snapshot
before journaling monotonic order observations, and reports safe resumption only
when journaled account exposure exactly matches authoritative positions and no
submitted execution remains live, ambiguous, or residually exposed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Protocol

from core.execution_journal import ExecutionJournal
from core.two_leg_execution import (
    ExecutionPhase,
    LegPhase,
    LegState,
    TwoLegExecution,
)

_POSITION_TOLERANCE = 1e-9


class RecoveryBlockedError(RuntimeError):
    """Raised when authoritative state cannot be obtained or trusted."""


@dataclass(frozen=True)
class OrderLookup:
    venue: str
    market_id: str
    idempotency_key: str
    venue_order_id: str | None


@dataclass(frozen=True)
class AuthoritativeOrder:
    venue: str
    market_id: str
    idempotency_key: str | None
    venue_order_id: str | None
    phase: LegPhase
    cumulative_filled_size: float

    def __post_init__(self) -> None:
        for field_name in ("venue", "market_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str)
            or not self.idempotency_key.strip()
        ):
            raise ValueError("idempotency_key must be a non-empty string when set")
        if self.venue_order_id is not None and (
            not isinstance(self.venue_order_id, str) or not self.venue_order_id.strip()
        ):
            raise ValueError("venue_order_id must be a non-empty string when set")
        if not isinstance(self.phase, LegPhase) or self.phase in {
            LegPhase.PLANNED,
            LegPhase.SUBMITTING,
            LegPhase.UNKNOWN,
        }:
            raise ValueError("authoritative order phase must be venue-observed")
        if (
            isinstance(self.cumulative_filled_size, bool)
            or not math.isfinite(self.cumulative_filled_size)
            or self.cumulative_filled_size < 0
        ):
            raise ValueError("cumulative_filled_size must be finite and non-negative")


@dataclass(frozen=True)
class AuthoritativePosition:
    market_id: str
    signed_size: float

    def __post_init__(self) -> None:
        if not isinstance(self.market_id, str) or not self.market_id.strip():
            raise ValueError("market_id must be a non-empty string")
        if isinstance(self.signed_size, bool) or not math.isfinite(self.signed_size):
            raise ValueError("signed_size must be finite")


class AuthoritativeVenueReader(Protocol):
    """Venue adapter whose methods must perform authoritative read-only calls."""

    async def read_order(self, lookup: OrderLookup) -> AuthoritativeOrder | None: ...

    async def list_open_orders(self) -> tuple[AuthoritativeOrder, ...]: ...

    async def list_positions(self) -> tuple[AuthoritativePosition, ...]: ...


@dataclass(frozen=True)
class RecoveryReport:
    safe_to_resume: bool
    reconciled_execution_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    journal_token: str


class ExecutionStartupGate:
    """Admit new two-leg plans only after a current safe recovery proof.

    The gate deliberately does not place, retry, cancel, or hedge orders.  It
    closes the startup-to-journal seam by requiring authoritative recovery and
    an unchanged, exclusively owned journal before a new immutable plan can be
    persisted.  Venue adapters must still provide a separate, durable
    submission integration before live mutation is possible.
    """

    def __init__(
        self,
        journal: ExecutionJournal,
        report: RecoveryReport,
        required_venues: frozenset[str],
    ) -> None:
        if not report.safe_to_resume or report.blockers:
            raise RecoveryBlockedError(
                "execution startup blocked by authoritative recovery"
            )
        if journal.snapshot_token() != report.journal_token:
            raise RecoveryBlockedError(
                "execution journal changed after authoritative recovery"
            )
        self._journal = journal
        self._journal_token = report.journal_token
        self._required_venues = frozenset(_normalize_required_venues(required_venues))

    @classmethod
    async def establish(
        cls,
        journal: ExecutionJournal,
        readers: Mapping[str, AuthoritativeVenueReader],
        *,
        required_venues: frozenset[str],
    ) -> "ExecutionStartupGate":
        """Run authoritative recovery and return a gate only when it is safe."""
        report = await reconcile_restart(
            journal,
            readers,
            required_venues=required_venues,
        )
        return cls(journal, report, required_venues)

    def persist_execution_plan(self, execution: TwoLegExecution) -> TwoLegExecution:
        """Persist a pristine plan if the recovery proof is still current."""
        self._require_current_proof()
        execution_venues = {
            leg.intent.venue.strip().lower() for leg in execution.legs.values()
        }
        if execution_venues != self._required_venues:
            raise RecoveryBlockedError(
                "execution venues do not exactly match recovered venues"
            )
        persisted = self._journal.create_execution(execution)
        self._journal_token = self._journal.snapshot_token()
        return persisted

    def _require_current_proof(self) -> None:
        if self._journal.snapshot_token() != self._journal_token:
            raise RecoveryBlockedError(
                "execution journal changed after authoritative recovery"
            )


async def reconcile_restart(
    journal: ExecutionJournal,
    readers: Mapping[str, AuthoritativeVenueReader],
    *,
    required_venues: frozenset[str],
) -> RecoveryReport:
    """Reconcile journaled executions without performing any venue mutation."""
    normalized_readers = _normalize_readers(readers)
    normalized_required = _normalize_required_venues(required_venues)
    if set(normalized_readers) != normalized_required:
        raise RecoveryBlockedError(
            "authoritative readers do not exactly match required venues"
        )
    executions, initial_token = journal.load_all_with_token()
    observations: list[tuple[str, str, AuthoritativeOrder]] = []
    reconciled_ids: set[str] = set()

    # Validate a complete order-read pass against disposable replayed aggregates
    # before appending any venue observation to the durable journal.
    for execution in executions:
        for leg_id in sorted(execution.legs):
            leg = execution.legs[leg_id]
            if leg.phase is LegPhase.PLANNED:
                continue
            reader = _reader_for(normalized_readers, leg.intent.venue)
            lookup = OrderLookup(
                venue=leg.intent.venue,
                market_id=leg.intent.market_id,
                idempotency_key=leg.idempotency_key,
                venue_order_id=leg.venue_order_id,
            )
            try:
                observation = await reader.read_order(lookup)
            except Exception as exc:
                raise RecoveryBlockedError(
                    f"authoritative order read failed for {leg.intent.venue}"
                ) from exc
            if observation is None:
                raise RecoveryBlockedError(
                    f"authoritative order identity is unresolved for {leg.intent.venue}"
                )
            _validate_order_identity(leg, observation)
            changed = _observation_changes(leg, observation)
            execution.reconcile_leg(
                leg_id,
                phase=observation.phase,
                cumulative_filled_size=observation.cumulative_filled_size,
                venue_order_id=observation.venue_order_id,
            )
            if changed:
                observations.append((execution.execution_id, leg_id, observation))
            reconciled_ids.add(execution.execution_id)

    if journal.snapshot_token() != initial_token:
        raise RecoveryBlockedError("execution journal changed during order reads")

    for execution_id, leg_id, observation in observations:
        journal.reconcile_leg(
            execution_id,
            leg_id,
            phase=observation.phase,
            cumulative_filled_size=observation.cumulative_filled_size,
            venue_order_id=observation.venue_order_id,
        )

    recovered, proof_token = journal.load_all_with_token()
    expected_positions: dict[tuple[str, str], float] = {}
    for execution in recovered:
        for leg in execution.legs.values():
            key = (leg.intent.venue.strip().lower(), leg.intent.market_id)
            expected_positions[key] = expected_positions.get(key, 0.0) + (
                leg.intent.side.exposure_sign * leg.filled_size
            )

    blockers: list[str] = []
    expected_open_orders: dict[tuple[str, str], LegState] = {}
    for execution in recovered:
        for leg in execution.legs.values():
            if leg.phase is LegPhase.OPEN:
                assert leg.venue_order_id is not None
                expected_open_orders[
                    (leg.intent.venue.strip().lower(), leg.venue_order_id)
                ] = leg

    observed_open_orders: set[tuple[str, str]] = set()
    observed_positions: dict[tuple[str, str], float] = {}
    for venue, reader in sorted(normalized_readers.items()):
        try:
            open_orders = await reader.list_open_orders()
            positions = await reader.list_positions()
        except Exception as exc:
            raise RecoveryBlockedError(
                f"authoritative account snapshot failed for {venue}"
            ) from exc

        for order in open_orders:
            if order.venue.strip().lower() != venue or order.phase is not LegPhase.OPEN:
                raise RecoveryBlockedError(
                    f"authoritative open-order snapshot is invalid for {venue}"
                )
            assert order.venue_order_id is not None
            key = (venue, order.venue_order_id)
            if key in observed_open_orders:
                raise RecoveryBlockedError(
                    f"authoritative open-order snapshot has duplicates for {venue}"
                )
            observed_open_orders.add(key)
            expected_leg = expected_open_orders.get(key)
            if expected_leg is None or (
                expected_leg.idempotency_key != order.idempotency_key
                or expected_leg.intent.market_id != order.market_id
            ):
                blockers.append(f"{venue}:{order.venue_order_id}:untracked_open_order")

        for position in positions:
            key = (venue, position.market_id)
            if key in observed_positions:
                raise RecoveryBlockedError(
                    f"authoritative position snapshot has duplicates for {venue}"
                )
            observed_positions[key] = position.signed_size

    for venue_order_key in sorted(set(expected_open_orders) - observed_open_orders):
        venue, order_id = venue_order_key
        blockers.append(f"{venue}:{order_id}:missing_open_order")

    for venue_market in sorted(set(expected_positions) | set(observed_positions)):
        venue, market_id = venue_market
        expected_size = expected_positions.get(venue_market, 0.0)
        observed_size = observed_positions.get(venue_market, 0.0)
        if not math.isclose(
            observed_size,
            expected_size,
            rel_tol=0.0,
            abs_tol=_POSITION_TOLERANCE,
        ):
            blockers.append(f"{venue}:{market_id}:position_mismatch")

    for execution in recovered:
        if execution.phase not in {ExecutionPhase.PLANNED, ExecutionPhase.COMPLETE}:
            blockers.append(f"{execution.execution_id}:{execution.phase.value}")

    if journal.snapshot_token() != proof_token:
        raise RecoveryBlockedError("execution journal changed during account reads")

    return RecoveryReport(
        safe_to_resume=not blockers,
        reconciled_execution_ids=tuple(sorted(reconciled_ids)),
        blockers=tuple(blockers),
        journal_token=proof_token,
    )


def _normalize_readers(
    readers: Mapping[str, AuthoritativeVenueReader],
) -> dict[str, AuthoritativeVenueReader]:
    normalized: dict[str, AuthoritativeVenueReader] = {}
    for venue, reader in readers.items():
        if not isinstance(venue, str) or not venue.strip():
            raise ValueError("reader venue must be a non-empty string")
        key = venue.strip().lower()
        if key in normalized:
            raise ValueError("reader venues must be unique case-insensitively")
        normalized[key] = reader
    return normalized


def _normalize_required_venues(required_venues: frozenset[str]) -> set[str]:
    if not isinstance(required_venues, frozenset) or not required_venues:
        raise ValueError("required_venues must be a non-empty frozenset")
    normalized: set[str] = set()
    for venue in required_venues:
        if not isinstance(venue, str) or not venue.strip():
            raise ValueError("required venue must be a non-empty string")
        normalized.add(venue.strip().lower())
    if len(normalized) != len(required_venues):
        raise ValueError("required venues must be unique case-insensitively")
    return normalized


def _reader_for(
    readers: Mapping[str, AuthoritativeVenueReader], venue: str
) -> AuthoritativeVenueReader:
    try:
        return readers[venue.strip().lower()]
    except KeyError as exc:
        raise RecoveryBlockedError(
            f"authoritative reader is unavailable for {venue}"
        ) from exc


def _validate_order_identity(leg: LegState, observation: AuthoritativeOrder) -> None:
    if observation.venue.strip().lower() != leg.intent.venue.strip().lower():
        raise RecoveryBlockedError("authoritative order venue does not match intent")
    if observation.market_id != leg.intent.market_id:
        raise RecoveryBlockedError("authoritative order market does not match intent")
    if observation.idempotency_key != leg.idempotency_key:
        raise RecoveryBlockedError(
            "authoritative order idempotency key does not match intent"
        )
    if (
        leg.venue_order_id is not None
        and observation.venue_order_id != leg.venue_order_id
    ):
        raise RecoveryBlockedError(
            "authoritative venue order id does not match journal"
        )


def _observation_changes(leg: LegState, observation: AuthoritativeOrder) -> bool:
    return (
        leg.phase is not observation.phase
        or leg.venue_order_id != observation.venue_order_id
        or not math.isclose(
            leg.filled_size,
            observation.cumulative_filled_size,
            rel_tol=0.0,
            abs_tol=_POSITION_TOLERANCE,
        )
    )
