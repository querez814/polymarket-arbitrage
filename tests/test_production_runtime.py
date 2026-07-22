from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.cross_platform_arb import CrossPlatformOpportunity, MarketPair
from core.execution_economics import PairEconomics
from core.execution_journal import ExecutionJournal
from core.execution_recovery import AuthoritativeOrder, AuthoritativePosition
from core.operations import PersistentOperatorControls
from core.production_runtime import ProductionArbitrageRuntime, RuntimeNotReadyError
from core.two_leg_execution import ExecutionPhase, LegPhase
from core.venue_execution import PreparedVenueOrder


@dataclass
class StubVenue:
    venue: str
    fills: list[float]
    requested_sizes: list[float] = field(default_factory=list)
    collateral: float | None = 1_000.0

    async def available_collateral(self):
        return self.collateral

    async def prepare_ioc(self, intent, *, idempotency_key, size):
        self.requested_sizes.append(size)
        return PreparedVenueOrder(
            self.venue,
            intent.market_id,
            idempotency_key,
            size,
            f"{self.venue}-{len(self.requested_sizes)}",
            object(),
        )

    async def submit_prepared(self, prepared):
        filled = self.fills.pop(0)
        return AuthoritativeOrder(
            self.venue,
            prepared.market_id,
            prepared.idempotency_key,
            prepared.venue_order_id,
            LegPhase.FILLED if filled == prepared.requested_size else LegPhase.CANCELLED,
            filled,
        )

    async def cancel_open(self, order):
        raise AssertionError("IOC test order must not remain open")

    async def read_order(self, lookup):
        return None

    async def list_open_orders(self):
        return ()

    async def list_positions(self):
        return ()


def _opportunity() -> CrossPlatformOpportunity:
    pair = MarketPair(
        polymarket_id="condition-1",
        polymarket_question="Question?",
        kalshi_ticker="TICKER-1",
        kalshi_title="Question?",
        similarity_score=0.99,
    )
    return CrossPlatformOpportunity(
        opportunity_id="opp-1",
        market_pair=pair,
        buy_platform="polymarket",
        sell_platform="kalshi",
        token="YES",
        buy_price=0.40,
        sell_price=0.65,
        gross_edge=0.25,
        net_edge=0.20,
        edge_pct=0.50,
        suggested_size=10.0,
        max_size=10.0,
        buy_liquidity=10.0,
        sell_liquidity=4.0,
    )


def _economics(*, age_seconds: int = 0) -> PairEconomics:
    return PairEconomics(
        pair_id="poly:condition-1|kalshi:TICKER-1",
        polymarket_market_id="condition-1",
        kalshi_ticker="TICKER-1",
        polymarket_fee_rate=Decimal("0.07"),
        polymarket_fee_exponent=Decimal("1"),
        polymarket_taker_only=True,
        polymarket_order_gas_cost=Decimal("0"),
        polymarket_gas_source="offchain_clob_order",
        kalshi_fee_type="quadratic",
        kalshi_fee_multiplier=Decimal("1"),
        observed_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


@pytest.mark.asyncio
async def test_runtime_owns_recovery_admission_and_executes_scarcer_leg_first(tmp_path):
    poly = StubVenue("polymarket", fills=[10.0])
    kalshi = StubVenue("kalshi", fills=[10.0])
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            require_private_stream=False,
        )
        await runtime.start()
        await runtime.resume("x" * 32, reason="test operator approval")
        result = await runtime.execute_opportunity(_opportunity(), _economics())
        await runtime.stop()

    controls.close()
    assert result.phase is ExecutionPhase.COMPLETE
    assert kalshi.requested_sizes == [10.0]
    assert poly.requested_sizes == [10.0]


@pytest.mark.asyncio
async def test_runtime_rejects_stale_costs_before_persisting_or_mutating(tmp_path):
    poly = StubVenue("polymarket", fills=[])
    kalshi = StubVenue("kalshi", fills=[])
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            require_private_stream=False,
        )
        await runtime.start()
        await runtime.resume("x" * 32, reason="test operator approval")
        with pytest.raises(RuntimeNotReadyError, match="stale"):
            await runtime.execute_opportunity(_opportunity(), _economics(age_seconds=31))
        assert journal.load_all() == ()
        await runtime.stop()

    controls.close()
    assert poly.requested_sizes == []
    assert kalshi.requested_sizes == []


@pytest.mark.asyncio
async def test_runtime_restart_requires_fresh_operator_arming(tmp_path):
    token = "x" * 32
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token=token
    )
    await controls.resume(token, reason="armed before process restart")

    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={
                "polymarket": StubVenue("polymarket", fills=[]),
                "kalshi": StubVenue("kalshi", fills=[]),
            },
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            require_private_stream=False,
        )
        await runtime.start()
        status = runtime.status()
        await runtime.stop()

    controls.close()
    assert status.halted is True
    assert status.ready is False


@pytest.mark.asyncio
async def test_runtime_panic_and_status_require_operator_authentication(tmp_path):
    token = "x" * 32
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token=token
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={
                "polymarket": StubVenue("polymarket", fills=[]),
                "kalshi": StubVenue("kalshi", fills=[]),
            },
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            require_private_stream=False,
        )
        await runtime.start()
        await runtime.resume(token, reason="operator approved")
        with pytest.raises(Exception, match="authentication failed"):
            runtime.operator_status("wrong-token")
        await runtime.panic(token, reason="operator requested stop")
        status = runtime.operator_status(token)
        await runtime.stop()

    controls.close()
    assert status.halted is True
    assert status.reason == "operator requested stop"


@pytest.mark.asyncio
async def test_runtime_owns_authoritative_detection_and_execution_for_a_pair(tmp_path):
    opportunity = _opportunity()
    economics = _economics()

    class EconomicsProvider:
        async def quote_pair(self, pair):
            assert pair is opportunity.market_pair
            return economics

    class Detector:
        def check_arbitrage(self, pair, polymarket_book, kalshi_book, *, economics):
            assert pair is opportunity.market_pair
            assert polymarket_book == "poly-book"
            assert kalshi_book == "kalshi-book"
            assert economics is not None
            return opportunity

    token = "x" * 32
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token=token
    )
    poly = StubVenue("polymarket", fills=[10.0])
    kalshi = StubVenue("kalshi", fills=[10.0])
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            economics_provider=EconomicsProvider(),
            detector=Detector(),
            require_private_stream=False,
        )
        await runtime.start()
        await runtime.resume(token, reason="operator approved")
        evaluation = await runtime.evaluate_pair(
            opportunity.market_pair, "poly-book", "kalshi-book"
        )
        await runtime.stop()

    controls.close()
    assert evaluation.opportunity is opportunity
    assert evaluation.economics is economics
    assert evaluation.execution is not None
    assert evaluation.execution.phase is ExecutionPhase.COMPLETE


@pytest.mark.asyncio
async def test_runtime_reserves_durable_two_leg_attempt_budget_before_mutation(tmp_path):
    token = "x" * 32
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token=token
    )
    poly = StubVenue("polymarket", fills=[10.0])
    kalshi = StubVenue("kalshi", fills=[10.0])
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            max_order_attempts_per_minute=2,
            max_daily_order_attempts=10,
            require_private_stream=False,
        )
        await runtime.start()
        await runtime.resume(token, reason="operator approved")
        await runtime.execute_opportunity(_opportunity(), _economics())
        with pytest.raises(RuntimeNotReadyError, match="per-minute"):
            await runtime.execute_opportunity(_opportunity(), _economics())
        await runtime.stop()

    controls.close()
    assert poly.requested_sizes == [10.0]
    assert kalshi.requested_sizes == [10.0]


@pytest.mark.asyncio
async def test_runtime_normalizes_size_before_journal_and_venue_preparation(tmp_path):
    opportunity = _opportunity()
    opportunity.suggested_size = 3.14159
    poly = StubVenue("polymarket", fills=[3.14])
    kalshi = StubVenue("kalshi", fills=[3.14])
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            require_private_stream=False,
        )
        await runtime.start()
        await runtime.resume("x" * 32, reason="operator approved")
        result = await runtime.execute_opportunity(opportunity, _economics())
        await runtime.stop()
    controls.close()

    assert result.phase is ExecutionPhase.COMPLETE
    assert poly.requested_sizes == [3.14]
    assert kalshi.requested_sizes == [3.14]


@pytest.mark.asyncio
async def test_runtime_rejects_aggregate_exposure_and_insufficient_collateral(tmp_path):
    opportunity = _opportunity()
    poly = StubVenue("polymarket", fills=[], collateral=1.0)
    kalshi = StubVenue("kalshi", fills=[])
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        exposure_runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            max_strategy_exposure=15.0,
            require_private_stream=False,
        )
        await exposure_runtime.start()
        await exposure_runtime.resume("x" * 32, reason="operator approved")
        with pytest.raises(RuntimeNotReadyError, match="strategy exposure"):
            await exposure_runtime.execute_opportunity(opportunity, _economics())
        await exposure_runtime.stop()

        collateral_runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            require_collateral=True,
            require_private_stream=False,
        )
        await collateral_runtime.start()
        await collateral_runtime.resume("x" * 32, reason="operator approved")
        with pytest.raises(RuntimeNotReadyError, match="collateral"):
            await collateral_runtime.execute_opportunity(opportunity, _economics())
        await collateral_runtime.stop()
    controls.close()
    assert poly.requested_sizes == []
    assert kalshi.requested_sizes == []


@pytest.mark.asyncio
async def test_runtime_rechecks_opportunity_freshness_after_recovery(tmp_path):
    class SlowVenue(StubVenue):
        async def list_open_orders(self):
            await asyncio.sleep(0.01)
            return ()

    poly = SlowVenue("polymarket", fills=[])
    kalshi = SlowVenue("kalshi", fills=[])
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            opportunity_max_age=timedelta(milliseconds=5),
            require_private_stream=False,
        )
        await runtime.start()
        await runtime.resume("x" * 32, reason="operator approved")
        opportunity = _opportunity()
        with pytest.raises(RuntimeNotReadyError, match="stale before mutation"):
            await runtime.execute_opportunity(opportunity, _economics())
        assert journal.load_all() == ()
        await runtime.stop()
    controls.close()
    assert poly.requested_sizes == []
    assert kalshi.requested_sizes == []


@pytest.mark.asyncio
async def test_private_stream_disconnect_closes_admission_and_requires_rearm(tmp_path):
    disconnect = asyncio.Event()

    class Stream:
        async def run(
            self,
            *,
            on_event,
            reconcile_rest,
            on_disconnect,
            market_tickers=(),
            max_reconnect_delay=30.0,
        ):
            await reconcile_rest()
            await disconnect.wait()
            await on_disconnect()
            await asyncio.Event().wait()

    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    with ExecutionJournal(tmp_path / "executions.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={
                "polymarket": StubVenue("polymarket", fills=[]),
                "kalshi": StubVenue("kalshi", fills=[]),
            },
            controls=controls,
            min_net_edge=0.01,
            max_order_notional=15.0,
            economics_max_age=timedelta(seconds=30),
            private_stream=Stream(),
        )
        await runtime.start()
        for _ in range(20):
            if runtime.status().private_stream_ready:
                break
            await asyncio.sleep(0)
        await runtime.resume("x" * 32, reason="operator approved")
        disconnect.set()
        for _ in range(20):
            if runtime.status().last_error:
                break
            await asyncio.sleep(0)
        status = runtime.status()
        await runtime.stop()
    controls.close()

    assert status.private_stream_ready is False
    assert status.halted is True
    assert status.last_error == "private_stream_disconnected"
