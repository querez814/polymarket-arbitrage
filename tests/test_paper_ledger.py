"""
Tests for paper ledger simulation accounting.
"""

from polymarket_client.models import (
    Opportunity,
    OpportunityType,
    Order,
    OrderSide,
    OrderStatus,
    Signal,
    TokenType,
    Trade,
)
from core.paper_ledger import PaperLedger


def make_signal(size: float = 10, price: float = 0.6) -> Signal:
    opportunity = Opportunity(
        opportunity_id="opp_1",
        opportunity_type=OpportunityType.BUNDLE_LONG,
        market_id="m1",
        edge=0.03,
        suggested_size=size,
        metadata={"depth_walked": {"levels_walked": 2}},
    )
    return Signal(
        signal_id="sig_1",
        action="place_orders",
        market_id="m1",
        opportunity=opportunity,
        orders=[
            {
                "token_type": TokenType.YES,
                "side": OrderSide.BUY,
                "price": price,
                "size": size,
                "strategy_tag": "bundle_arb",
            }
        ],
    )


def test_paper_ledger_records_detection_order_fill_and_exposure():
    ledger = PaperLedger(one_sided_exposure_cap=100)
    signal = make_signal()
    order = Order(
        order_id="o1",
        market_id="m1",
        token_type=TokenType.YES,
        side=OrderSide.BUY,
        price=0.6,
        size=10,
        status=OrderStatus.OPEN,
        strategy_tag="bundle_arb",
    )
    trade = Trade(
        trade_id="t1",
        order_id="o1",
        market_id="m1",
        token_type=TokenType.YES,
        side=OrderSide.BUY,
        price=0.6,
        size=10,
    )

    ledger.record_signal(signal)
    ledger.record_order(order)
    ledger.record_fill(trade)

    summary = ledger.summary()
    assert summary["orders_submitted"] == 1
    assert summary["fills"] == 1
    assert summary["empty_message"] == ""
    assert summary["open_exposure"]["m1"]["bundle_arb"] == 6


def test_paper_ledger_empty_summary_says_no_fills_yet():
    ledger = PaperLedger()

    assert ledger.summary()["empty_message"] == "No paper fills yet"


def test_paper_ledger_one_sided_exposure_cap_rejects_projection():
    ledger = PaperLedger(one_sided_exposure_cap=5)

    assert ledger.would_exceed_one_sided_cap(make_signal(size=10, price=0.6)) is True
