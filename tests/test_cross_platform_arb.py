"""
Tests for cross-platform paper synthetic arbitrage gates.
"""

import pytest

from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from core.cross_platform_arb import CrossPlatformArbEngine, MarketPair


def make_order_book(market_id: str, yes_ask: float, no_ask: float) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            token_type=TokenType.YES,
            bids=OrderBookSide(levels=[PriceLevel(price=yes_ask - 0.02, size=100)]),
            asks=OrderBookSide(levels=[PriceLevel(price=yes_ask, size=100)]),
        ),
        no=TokenOrderBook(
            token_type=TokenType.NO,
            bids=OrderBookSide(levels=[PriceLevel(price=no_ask - 0.02, size=100)]),
            asks=OrderBookSide(levels=[PriceLevel(price=no_ask, size=100)]),
        ),
    )


def test_cross_platform_synthetic_rejects_unapproved_fuzzy_pair():
    engine = CrossPlatformArbEngine(min_edge=0.01, polymarket_taker_fee=0, kalshi_taker_fee=0, gas_cost=0)
    pair = MarketPair(
        polymarket_id="poly_1",
        kalshi_ticker="KALSHI-1",
        polymarket_question="Will test pass?",
        kalshi_title="Will test pass?",
        similarity_score=0.95,
        resolution_match="fuzzy",
    )

    opportunity = engine.check_synthetic_paper_arbitrage(
        pair,
        make_order_book("poly_1", yes_ask=0.40, no_ask=0.70),
        make_order_book("kalshi_1", yes_ask=0.70, no_ask=0.50),
    )

    assert opportunity is None


def test_cross_platform_synthetic_allows_exact_pair():
    engine = CrossPlatformArbEngine(min_edge=0.01, polymarket_taker_fee=0, kalshi_taker_fee=0, gas_cost=0)
    pair = MarketPair(
        polymarket_id="poly_1",
        kalshi_ticker="KALSHI-1",
        polymarket_question="Will test pass?",
        kalshi_title="Will test pass?",
        similarity_score=1.0,
        resolution_match="exact",
    )

    opportunity = engine.check_synthetic_paper_arbitrage(
        pair,
        make_order_book("poly_1", yes_ask=0.40, no_ask=0.70),
        make_order_book("kalshi_1", yes_ask=0.70, no_ask=0.50),
    )

    assert opportunity is not None
    assert opportunity.net_edge == pytest.approx(0.1)
