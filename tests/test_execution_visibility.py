import pytest

from core.execution import ExecutionConfig, ExecutionEngine
from core.portfolio import Portfolio
from core.risk_manager import RiskConfig, RiskManager
from polymarket_client import PolymarketClient
from polymarket_client.models import OrderSide, TokenType
from utils.paper_trade_store import PaperTradeStore


@pytest.mark.asyncio
async def test_open_order_snapshots_include_visibility_fields():
    client = PolymarketClient(dry_run=True)
    risk_manager = RiskManager(RiskConfig(trade_only_high_volume=False))
    engine = ExecutionEngine(
        client=client,
        risk_manager=risk_manager,
        portfolio=Portfolio(),
        config=ExecutionConfig(
            dry_run=True,
            strategy_order_timeouts={"market_making": 12.0},
        ),
    )
    await client.connect()
    try:
        order = await client.place_order(
            market_id="market-1",
            token_type=TokenType.YES,
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            strategy_tag="market_making",
        )
        engine._track_order(order)
        risk_manager.reserve_strategy_exposure(order.strategy_tag, order.notional)

        snapshot = engine.get_open_order_snapshots()[0]
        paper_order = engine.get_order_history_snapshots()[0]

        assert snapshot["type"] == "open_order"
        assert snapshot["remaining_notional"] == 5.0
        assert snapshot["strategy_tag"] == "market_making"
        assert snapshot["timeout_seconds"] == 12.0
        assert snapshot["execution_mode"] == "paper"
        assert snapshot["is_paper"] is True
        assert paper_order["type"] == "paper_order"
        assert paper_order["order_id"] == order.order_id
        assert paper_order["pnl_source"] == "paper"
        assert engine.get_open_notional_by_strategy() == {"market_making": 5.0}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_strategy_reservation_released_on_full_fill():
    client = PolymarketClient(dry_run=True)
    risk_manager = RiskManager(RiskConfig(trade_only_high_volume=False))
    engine = ExecutionEngine(
        client=client,
        risk_manager=risk_manager,
        portfolio=Portfolio(),
        config=ExecutionConfig(dry_run=True),
    )
    await client.connect()
    try:
        order = await client.place_order(
            market_id="market-1",
            token_type=TokenType.YES,
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            strategy_tag="bundle_arb",
        )
        engine._track_order(order)
        risk_manager.reserve_strategy_exposure(order.strategy_tag, order.notional)

        trade = client.simulate_fill(order.order_id)
        engine.handle_fill(trade)

        assert risk_manager.get_strategy_exposure("bundle_arb") == 0.0
        assert engine.get_open_order_snapshots() == []
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_execution_engine_records_paper_events_when_store_injected(tmp_path):
    client = PolymarketClient(dry_run=True)
    risk_manager = RiskManager(RiskConfig(trade_only_high_volume=False))
    store = PaperTradeStore(str(tmp_path / "paper_trades.db"))
    engine = ExecutionEngine(
        client=client,
        risk_manager=risk_manager,
        portfolio=Portfolio(),
        config=ExecutionConfig(dry_run=True),
        paper_trade_store=store,
    )
    await client.connect()
    try:
        order = await client.place_order(
            market_id="market-1",
            token_type=TokenType.YES,
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            strategy_tag="bundle_arb",
        )
        engine._track_order(order)
        engine._record_paper_event(
            event_type="placed",
            order=order,
            reason_code="paper_order_placed",
        )
        risk_manager.reserve_strategy_exposure(order.strategy_tag, order.notional)

        trade = client.simulate_fill(order.order_id)
        engine.handle_fill(trade)

        events = store.events_for_order(order.order_id)
        assert [event.event_type for event in events] == ["placed", "filled"]
        assert events[0].reason_code == "paper_order_placed"
        assert events[1].reason_code == "hypothetical_paper_fill"
        assert events[1].trade_id == trade.trade_id
    finally:
        await client.disconnect()
        store.close()
