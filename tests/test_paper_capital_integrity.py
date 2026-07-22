from core.execution import ExecutionEngine, ExecutionConfig
from core.portfolio import Portfolio
from core.risk_manager import RiskConfig, RiskManager
from polymarket_client.models import Order, OrderSide, TokenType, Trade


def test_paper_fill_cannot_credit_cash_for_an_unowned_sale():
    portfolio = Portfolio(initial_balance=1_000.0)
    engine = ExecutionEngine(
        client=object(),
        risk_manager=RiskManager(RiskConfig()),
        portfolio=portfolio,
        config=ExecutionConfig(dry_run=True),
    )
    trade = Trade(
        trade_id="paper-short",
        order_id="untracked-paper-order",
        market_id="market-1",
        token_type=TokenType.YES,
        side=OrderSide.SELL,
        price=0.60,
        size=100.0,
        fee=0.90,
        is_simulated=True,
    )

    engine.handle_fill(trade)

    assert portfolio.cash_balance == 1_000.0
    assert portfolio.get_position("market-1", TokenType.YES) is None


def test_paper_order_admission_requires_cash_or_owned_inventory():
    portfolio = Portfolio(initial_balance=1_000.0)
    engine = ExecutionEngine(
        client=object(),
        risk_manager=RiskManager(RiskConfig()),
        portfolio=portfolio,
        config=ExecutionConfig(dry_run=True),
    )

    oversized_buy = Order(
        order_id="buy",
        market_id="market-1",
        token_type=TokenType.YES,
        side=OrderSide.BUY,
        price=0.60,
        size=2_000.0,
    )
    naked_sell = Order(
        order_id="sell",
        market_id="market-1",
        token_type=TokenType.YES,
        side=OrderSide.SELL,
        price=0.60,
        size=1.0,
    )

    assert engine._paper_collateral_available(oversized_buy) is False
    assert engine._paper_collateral_available(naked_sell) is False
