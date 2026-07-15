from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.execution_journal import ExecutionJournal
from core.execution_recovery import (
    AuthoritativeOrder,
    AuthoritativePosition,
    ExecutionStartupGate,
    OrderLookup,
    RecoveryBlockedError,
    reconcile_restart,
)
from core.two_leg_execution import (
    ExecutionPhase,
    LegIntent,
    LegPhase,
    LegSide,
    TwoLegExecution,
)

VENUES = frozenset({"polymarket", "kalshi"})


def _execution(execution_id: str = "exec-42") -> TwoLegExecution:
    return TwoLegExecution(
        execution_id,
        LegIntent("poly", "polymarket", "condition-1", LegSide.BUY, 0.40, 10.0),
        LegIntent("kalshi", "kalshi", "ticker-1", LegSide.SELL, 0.65, 10.0),
    )


@dataclass
class StubReader:
    orders: dict[str, AuthoritativeOrder | None]
    positions: dict[str, float]
    account_orders: tuple[AuthoritativeOrder, ...] = ()

    async def read_order(self, lookup: OrderLookup) -> AuthoritativeOrder | None:
        return self.orders.get(lookup.idempotency_key)

    async def read_position(self, market_id: str) -> AuthoritativePosition:
        return AuthoritativePosition(market_id, self.positions[market_id])

    async def list_open_orders(self) -> tuple[AuthoritativeOrder, ...]:
        return self.account_orders

    async def list_positions(self) -> tuple[AuthoritativePosition, ...]:
        return tuple(
            AuthoritativePosition(market_id, size)
            for market_id, size in sorted(self.positions.items())
            if size != 0
        )


@pytest.mark.asyncio
async def test_restart_reconciles_orders_then_proves_positions_match_journal(tmp_path):
    path = tmp_path / "executions.sqlite3"
    execution = _execution()
    poly_key = execution.legs["poly"].idempotency_key
    kalshi_key = execution.legs["kalshi"].idempotency_key
    with ExecutionJournal(path) as journal:
        journal.create_execution(execution)
        journal.start_submission("exec-42", "poly")
        journal.mark_submission_ambiguous("exec-42", "poly")
        journal.start_submission("exec-42", "kalshi")

        report = await reconcile_restart(
            journal,
            {
                "polymarket": StubReader(
                    {
                        poly_key: AuthoritativeOrder(
                            venue="polymarket",
                            market_id="condition-1",
                            idempotency_key=poly_key,
                            venue_order_id="poly-order-1",
                            phase=LegPhase.CANCELLED,
                            cumulative_filled_size=3.0,
                        )
                    },
                    {"condition-1": 3.0},
                ),
                "kalshi": StubReader(
                    {
                        kalshi_key: AuthoritativeOrder(
                            venue="kalshi",
                            market_id="ticker-1",
                            idempotency_key=kalshi_key,
                            venue_order_id="kalshi-order-1",
                            phase=LegPhase.CANCELLED,
                            cumulative_filled_size=3.0,
                        )
                    },
                    {"ticker-1": -3.0},
                ),
            },
            required_venues=VENUES,
        )

        recovered = journal.load_execution("exec-42")

    assert report.safe_to_resume is True
    assert report.reconciled_execution_ids == ("exec-42",)
    assert recovered.phase is ExecutionPhase.COMPLETE
    assert recovered.realized_residual_size == 0.0


@pytest.mark.asyncio
async def test_startup_gate_admits_plan_only_after_safe_authoritative_recovery(
    tmp_path,
):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        gate = await ExecutionStartupGate.establish(
            journal,
            {
                "polymarket": StubReader({}, {}),
                "kalshi": StubReader({}, {}),
            },
            required_venues=VENUES,
        )

        persisted = gate.persist_execution_plan(_execution())

        assert persisted.execution_id == "exec-42"
        assert journal.load_execution("exec-42").phase is ExecutionPhase.PLANNED


@pytest.mark.asyncio
async def test_startup_gate_refuses_unsafe_recovery_report(tmp_path):
    orphan = AuthoritativeOrder(
        "polymarket",
        "external-market",
        None,
        "external-order",
        LegPhase.OPEN,
        0.0,
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        with pytest.raises(
            RecoveryBlockedError,
            match="startup blocked by authoritative recovery",
        ):
            await ExecutionStartupGate.establish(
                journal,
                {
                    "polymarket": StubReader({}, {}, account_orders=(orphan,)),
                    "kalshi": StubReader({}, {}),
                },
                required_venues=VENUES,
            )

        assert journal.load_all() == ()


@pytest.mark.asyncio
async def test_startup_gate_invalidates_stale_recovery_proof(tmp_path):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        gate = await ExecutionStartupGate.establish(
            journal,
            {
                "polymarket": StubReader({}, {}),
                "kalshi": StubReader({}, {}),
            },
            required_venues=VENUES,
        )
        journal.create_execution(_execution("external-change"))

        with pytest.raises(
            RecoveryBlockedError,
            match="journal changed after authoritative recovery",
        ):
            gate.persist_execution_plan(_execution())


@pytest.mark.asyncio
async def test_startup_gate_requires_plan_to_match_recovered_venues(tmp_path):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        gate = await ExecutionStartupGate.establish(
            journal,
            {
                "polymarket": StubReader({}, {}),
                "kalshi": StubReader({}, {}),
            },
            required_venues=VENUES,
        )
        wrong_venues = TwoLegExecution(
            "exec-other",
            LegIntent(
                "poly",
                "polymarket",
                "condition-1",
                LegSide.BUY,
                0.40,
                10.0,
            ),
            LegIntent(
                "other",
                "other-venue",
                "ticker-1",
                LegSide.SELL,
                0.65,
                10.0,
            ),
        )

        with pytest.raises(
            RecoveryBlockedError,
            match="venues do not exactly match recovered venues",
        ):
            gate.persist_execution_plan(wrong_venues)

        assert journal.load_all() == ()


@pytest.mark.asyncio
async def test_startup_gate_consumes_recovery_proof_after_one_plan(tmp_path):
    polymarket = StubReader({}, {})
    readers = {
        "polymarket": polymarket,
        "kalshi": StubReader({}, {}),
    }
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        gate = await ExecutionStartupGate.establish(
            journal,
            readers,
            required_venues=VENUES,
        )
        gate.persist_execution_plan(_execution())

        # Venue state can drift without changing the local journal.  Reusing the
        # old proof must fail, while a fresh reconciliation observes the drift.
        polymarket.positions["external-market"] = 1.0
        with pytest.raises(
            RecoveryBlockedError,
            match="recovery proof has already been consumed",
        ):
            gate.persist_execution_plan(_execution("exec-second"))
        with pytest.raises(
            RecoveryBlockedError,
            match="startup blocked by authoritative recovery",
        ):
            await ExecutionStartupGate.establish(
                journal,
                readers,
                required_venues=VENUES,
            )

        assert tuple(item.execution_id for item in journal.load_all()) == ("exec-42",)


@pytest.mark.asyncio
async def test_unresolved_ambiguous_order_blocks_without_appending(tmp_path):
    execution = _execution()
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        journal.create_execution(execution)
        journal.start_submission("exec-42", "poly")
        journal.mark_submission_ambiguous("exec-42", "poly")
        before = journal.event_count("exec-42")

        with pytest.raises(RecoveryBlockedError, match="identity is unresolved"):
            await reconcile_restart(
                journal,
                {
                    "polymarket": StubReader({}, {"condition-1": 0.0}),
                    "kalshi": StubReader({}, {"ticker-1": 0.0}),
                },
                required_venues=VENUES,
            )

        recovered = journal.load_execution("exec-42")
        assert journal.event_count("exec-42") == before

    assert recovered.phase is ExecutionPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_identity_mismatch_aborts_complete_read_pass_before_journaling(tmp_path):
    execution = _execution()
    poly_key = execution.legs["poly"].idempotency_key
    kalshi_key = execution.legs["kalshi"].idempotency_key
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        journal.create_execution(execution)
        journal.start_submission("exec-42", "poly")
        journal.start_submission("exec-42", "kalshi")
        before = journal.event_count("exec-42")

        with pytest.raises(RecoveryBlockedError, match="market does not match"):
            await reconcile_restart(
                journal,
                {
                    "polymarket": StubReader(
                        {
                            poly_key: AuthoritativeOrder(
                                "polymarket",
                                "condition-1",
                                poly_key,
                                "poly-order-1",
                                LegPhase.CANCELLED,
                                0.0,
                            )
                        },
                        {"condition-1": 0.0},
                    ),
                    "kalshi": StubReader(
                        {
                            kalshi_key: AuthoritativeOrder(
                                "kalshi",
                                "wrong-market",
                                kalshi_key,
                                "kalshi-order-1",
                                LegPhase.CANCELLED,
                                0.0,
                            )
                        },
                        {"ticker-1": 0.0},
                    ),
                },
                required_venues=VENUES,
            )

        assert journal.event_count("exec-42") == before
        assert all(
            leg.phase is LegPhase.SUBMITTING
            for leg in journal.load_execution("exec-42").legs.values()
        )


@pytest.mark.asyncio
async def test_position_mismatch_and_live_order_keep_recovery_closed(tmp_path):
    execution = _execution()
    poly_key = execution.legs["poly"].idempotency_key
    open_order = AuthoritativeOrder(
        "polymarket",
        "condition-1",
        poly_key,
        "poly-order-1",
        LegPhase.OPEN,
        2.0,
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        journal.create_execution(execution)
        journal.start_submission("exec-42", "poly")

        report = await reconcile_restart(
            journal,
            {
                "polymarket": StubReader(
                    {poly_key: open_order},
                    {"condition-1": 3.0},
                    account_orders=(open_order,),
                ),
                "kalshi": StubReader({}, {"ticker-1": 0.0}),
            },
            required_venues=VENUES,
        )

    assert report.safe_to_resume is False
    assert report.blockers == (
        "polymarket:condition-1:position_mismatch",
        "exec-42:residual_exposure",
    )


@pytest.mark.asyncio
async def test_unchanged_authoritative_snapshot_is_idempotent(tmp_path):
    execution = _execution()
    poly_key = execution.legs["poly"].idempotency_key
    reader = StubReader(
        {
            poly_key: AuthoritativeOrder(
                "polymarket",
                "condition-1",
                poly_key,
                "poly-order-1",
                LegPhase.CANCELLED,
                0.0,
            )
        },
        {"condition-1": 0.0},
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        journal.create_execution(execution)
        journal.start_submission("exec-42", "poly")
        readers = {
            "polymarket": reader,
            "kalshi": StubReader({}, {"ticker-1": 0.0}),
        }

        await reconcile_restart(journal, readers, required_venues=VENUES)
        after_first = journal.event_count("exec-42")
        await reconcile_restart(journal, readers, required_venues=VENUES)

        assert journal.event_count("exec-42") == after_first


@pytest.mark.asyncio
async def test_untracked_account_order_and_position_block_safe_resume(tmp_path):
    orphan = AuthoritativeOrder(
        "polymarket",
        "external-market",
        None,
        "external-order",
        LegPhase.OPEN,
        0.0,
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        report = await reconcile_restart(
            journal,
            {
                "polymarket": StubReader(
                    {}, {"external-market": 4.0}, account_orders=(orphan,)
                ),
                "kalshi": StubReader({}, {}),
            },
            required_venues=VENUES,
        )

    assert report.safe_to_resume is False
    assert report.blockers == (
        "polymarket:external-order:untracked_open_order",
        "polymarket:external-market:position_mismatch",
    )


@pytest.mark.asyncio
async def test_concurrent_journal_change_invalidates_account_snapshot(tmp_path):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:

        class MutatingReader(StubReader):
            async def list_positions(self) -> tuple[AuthoritativePosition, ...]:
                journal.create_execution(_execution("concurrent"))
                return ()

        with pytest.raises(RecoveryBlockedError, match="changed during account reads"):
            await reconcile_restart(
                journal,
                {
                    "polymarket": MutatingReader({}, {}),
                    "kalshi": StubReader({}, {}),
                },
                required_venues=VENUES,
            )


@pytest.mark.asyncio
async def test_missing_required_venue_reader_blocks_even_with_empty_journal(tmp_path):
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        with pytest.raises(RecoveryBlockedError, match="exactly match"):
            await reconcile_restart(
                journal,
                {"polymarket": StubReader({}, {})},
                required_venues=VENUES,
            )
