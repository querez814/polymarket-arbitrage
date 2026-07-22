from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.execution_journal import ExecutionJournal
from core.execution_recovery import AuthoritativeOrder
from core.two_leg_execution import (
    ExecutionPhase,
    LegIntent,
    LegPhase,
    LegSide,
    TwoLegExecution,
)
from core.venue_execution import (
    LockedArbitrageExecutor,
    PreparedVenueOrder,
    VenueMutationAmbiguousError,
)


def _execution(execution_id: str = "matrix") -> TwoLegExecution:
    return TwoLegExecution(
        execution_id,
        LegIntent("scarce", "polymarket", "condition-1", LegSide.BUY, 0.40, 10.0),
        LegIntent("hedge", "kalshi", "ticker-1", LegSide.SELL, 0.65, 10.0),
    )


@dataclass
class StubAdapter:
    venue: str
    submit_phase: LegPhase = LegPhase.CANCELLED
    submit_fill: float = 0.0
    submit_ambiguous: bool = False
    prepared_id: bool = True
    cancel_phase: LegPhase = LegPhase.CANCELLED
    cancel_fill: float = 0.0
    cancel_ambiguous: bool = False
    requested_sizes: list[float] = field(default_factory=list)
    submit_calls: int = 0
    cancel_calls: int = 0

    async def prepare_ioc(self, intent, *, idempotency_key, size):
        self.requested_sizes.append(size)
        return PreparedVenueOrder(
            venue=self.venue,
            market_id=intent.market_id,
            idempotency_key=idempotency_key,
            requested_size=size,
            venue_order_id=f"{self.venue}-prepared" if self.prepared_id else None,
            payload=object(),
        )

    async def submit_prepared(self, prepared):
        self.submit_calls += 1
        if self.submit_ambiguous:
            raise VenueMutationAmbiguousError("ambiguous")
        return AuthoritativeOrder(
            self.venue,
            prepared.market_id,
            prepared.idempotency_key,
            prepared.venue_order_id or f"{self.venue}-accepted",
            self.submit_phase,
            self.submit_fill,
        )

    async def cancel_open(self, order):
        self.cancel_calls += 1
        if self.cancel_ambiguous:
            raise VenueMutationAmbiguousError("ambiguous cancel")
        return AuthoritativeOrder(
            order.venue,
            order.market_id,
            order.idempotency_key,
            order.venue_order_id,
            self.cancel_phase,
            self.cancel_fill,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "first_phase",
        "first_fill",
        "second_phase",
        "second_fill",
        "second_ambiguous",
        "expected_phase",
        "expected_residual",
    ),
    [
        (LegPhase.CANCELLED, 0.0, LegPhase.CANCELLED, 0.0, False, ExecutionPhase.COMPLETE, 0.0),
        (LegPhase.CANCELLED, 4.0, LegPhase.FILLED, 4.0, False, ExecutionPhase.COMPLETE, 0.0),
        (
            LegPhase.CANCELLED, 4.0, LegPhase.CANCELLED, 2.0, False,
            ExecutionPhase.RESIDUAL_EXPOSURE, 2.0,
        ),
        (
            LegPhase.CANCELLED, 4.0, LegPhase.REJECTED, 0.0, False,
            ExecutionPhase.RESIDUAL_EXPOSURE, 4.0,
        ),
        (
            LegPhase.CANCELLED, 4.0, LegPhase.CANCELLED, 0.0, True,
            ExecutionPhase.RECOVERY_REQUIRED, 4.0,
        ),
        (LegPhase.FILLED, 10.0, LegPhase.FILLED, 10.0, False, ExecutionPhase.COMPLETE, 0.0),
        (
            LegPhase.FILLED, 10.0, LegPhase.CANCELLED, 6.0, False,
            ExecutionPhase.RESIDUAL_EXPOSURE, 4.0,
        ),
        (
            LegPhase.FILLED, 10.0, LegPhase.REJECTED, 0.0, False,
            ExecutionPhase.RESIDUAL_EXPOSURE, 10.0,
        ),
        (
            LegPhase.FILLED, 10.0, LegPhase.CANCELLED, 0.0, True,
            ExecutionPhase.RECOVERY_REQUIRED, 10.0,
        ),
    ],
)
async def test_two_leg_failure_matrix(
    tmp_path,
    first_phase,
    first_fill,
    second_phase,
    second_fill,
    second_ambiguous,
    expected_phase,
    expected_residual,
):
    first = StubAdapter(
        "polymarket", submit_phase=first_phase, submit_fill=first_fill
    )
    second = StubAdapter(
        "kalshi",
        submit_phase=second_phase,
        submit_fill=second_fill,
        submit_ambiguous=second_ambiguous,
        prepared_id=False,
    )
    with ExecutionJournal(tmp_path / "journal.sqlite3") as journal:
        journal.create_execution(_execution())
        result = await LockedArbitrageExecutor(
            journal, {"polymarket": first, "kalshi": second}
        ).execute("matrix", first_leg_id="scarce")

    assert result.phase is expected_phase
    assert result.realized_residual_size == expected_residual
    if first_fill == 0:
        assert result.legs["hedge"].phase is LegPhase.SKIPPED
        assert second.submit_calls == 0
    else:
        assert second.requested_sizes == [first_fill]


@pytest.mark.asyncio
async def test_ambiguous_first_submission_stops_before_hedge_and_keeps_precomputed_id(
    tmp_path,
):
    first = StubAdapter("polymarket", submit_ambiguous=True)
    second = StubAdapter("kalshi", prepared_id=False)
    path = tmp_path / "journal.sqlite3"
    with ExecutionJournal(path) as journal:
        journal.create_execution(_execution())
        result = await LockedArbitrageExecutor(
            journal, {"polymarket": first, "kalshi": second}
        ).execute("matrix", first_leg_id="scarce")

    with ExecutionJournal(path) as restarted:
        replayed = restarted.load_execution("matrix")

    assert result.phase is ExecutionPhase.RECOVERY_REQUIRED
    assert replayed.legs["scarce"].venue_order_id == "polymarket-prepared"
    assert second.submit_calls == 0


@pytest.mark.asyncio
async def test_unexpected_open_ioc_is_cancelled_before_hedging(tmp_path):
    first = StubAdapter(
        "polymarket",
        submit_phase=LegPhase.OPEN,
        submit_fill=2.0,
        cancel_phase=LegPhase.CANCELLED,
        cancel_fill=3.0,
    )
    second = StubAdapter("kalshi", submit_phase=LegPhase.FILLED, submit_fill=3.0)
    with ExecutionJournal(tmp_path / "journal.sqlite3") as journal:
        journal.create_execution(_execution())
        result = await LockedArbitrageExecutor(
            journal, {"polymarket": first, "kalshi": second}
        ).execute("matrix", first_leg_id="scarce")

    assert first.cancel_calls == 1
    assert second.requested_sizes == [3.0]
    assert result.phase is ExecutionPhase.COMPLETE


@pytest.mark.asyncio
async def test_ambiguous_cancel_stops_before_hedging(tmp_path):
    first = StubAdapter(
        "polymarket",
        submit_phase=LegPhase.OPEN,
        submit_fill=2.0,
        cancel_ambiguous=True,
    )
    second = StubAdapter("kalshi")
    with ExecutionJournal(tmp_path / "journal.sqlite3") as journal:
        journal.create_execution(_execution())
        result = await LockedArbitrageExecutor(
            journal, {"polymarket": first, "kalshi": second}
        ).execute("matrix", first_leg_id="scarce")

    assert result.phase is ExecutionPhase.RECOVERY_REQUIRED
    assert second.submit_calls == 0
