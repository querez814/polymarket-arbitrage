"""
Tests for execution behavior across scanner and paper modes.
"""

import pytest

from polymarket_client.models import (
    Opportunity,
    OpportunityType,
    OrderSide,
    Signal,
    TokenType,
)
from core.execution import ExecutionConfig, ExecutionEngine
from core.paper_ledger import PaperLedger
from core.portfolio import Portfolio
from core.risk_manager import RiskConfig, RiskManager


class FakeClient:
    async def place_order(self, **kwargs):  # pragma: no cover - should not be called here
        raise AssertionError("scanner mode must not place orders")

    async def cancel_order(self, order_id):  # pragma: no cover - should not be called here
        raise AssertionError("scanner mode must not cancel orders")


def make_signal() -> Signal:
    return Signal(
        signal_id="sig_1",
        action="place_orders",
        market_id="m1",
        opportunity=Opportunity(
            opportunity_id="opp_1",
            opportunity_type=OpportunityType.BUNDLE_LONG,
            market_id="m1",
            edge=0.03,
            suggested_size=10,
        ),
        orders=[
            {
                "token_type": TokenType.YES,
                "side": OrderSide.BUY,
                "price": 0.45,
                "size": 10,
                "strategy_tag": "bundle_arb",
            }
        ],
    )


@pytest.mark.asyncio
async def test_scanner_never_creates_paper_orders():
    ledger = PaperLedger()
    engine = ExecutionEngine(
        client=FakeClient(),
        risk_manager=RiskManager(RiskConfig(trade_only_high_volume=False)),
        portfolio=Portfolio(initial_balance=100),
        config=ExecutionConfig(
            dry_run=True,
            execution_enabled=False,
            trading_mode="scanner",
        ),
        paper_ledger=ledger,
    )

    await engine.submit_signal(make_signal())

    assert engine.get_open_orders() == []
    assert engine.stats.signals_rejected == 1
    assert ledger.summary()["orders_submitted"] == 0
    assert ledger.summary()["rejections"] == 1
