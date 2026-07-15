from datetime import datetime, timezone

import pytest

from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from utils.training_data import build_training_snapshot, edge_direction, quote_from_token_book


def token_book(token_type, bid, ask, bid_size=10, ask_size=20):
    return TokenOrderBook(
        token_type=token_type,
        bids=OrderBookSide(levels=[PriceLevel(price=bid, size=bid_size)]),
        asks=OrderBookSide(levels=[PriceLevel(price=ask, size=ask_size)]),
    )


def orderbook(market_id, yes_bid, yes_ask, no_bid, no_ask):
    return OrderBook(
        market_id=market_id,
        yes=token_book(TokenType.YES, yes_bid, yes_ask),
        no=token_book(TokenType.NO, no_bid, no_ask),
        timestamp=datetime(2026, 6, 24, 12, tzinfo=timezone.utc),
    )


def test_quote_from_token_book_extracts_top_of_book_features():
    quote = quote_from_token_book(token_book(TokenType.YES, 0.49, 0.51, 5, 8))

    assert quote == {
        "bid": 0.49,
        "ask": 0.51,
        "bid_size": 5.0,
        "ask_size": 8.0,
        "mid": 0.5,
        "spread": 0.020000000000000018,
    }


def test_edge_direction_calculates_cost_adjusted_edge():
    direction = edge_direction(
        token="YES",
        buy_platform="polymarket",
        sell_platform="kalshi",
        buy_price=0.40,
        sell_price=0.45,
        buy_size=100,
        sell_size=80,
        polymarket_taker_fee=0.01,
        kalshi_taker_fee=0.02,
        gas_cost=0.001,
    )

    assert direction["gross_edge"] == pytest.approx(0.05)
    assert direction["estimated_cost"] == pytest.approx(0.015)
    assert direction["net_edge"] == pytest.approx(0.035)
    assert direction["max_size"] == 80


def test_build_training_snapshot_includes_pair_books_and_best_direction():
    snapshot = build_training_snapshot(
        timestamp="2026-06-24T12:00:00Z",
        polymarket_id="poly-1",
        kalshi_ticker="kalshi-1",
        polymarket_question="Will Team A win?",
        kalshi_title="Team A vs Team B Winner?",
        polymarket_orderbook=orderbook("poly-1", 0.39, 0.40, 0.59, 0.60),
        kalshi_orderbook=orderbook("kalshi:kalshi-1", 0.45, 0.46, 0.54, 0.55),
        polymarket_taker_fee=0.0,
        kalshi_taker_fee=0.0,
        gas_cost=0.0,
    )

    assert snapshot["source"] == "live-orderbook"
    assert snapshot["pair"]["polymarket_id"] == "poly-1"
    assert snapshot["pair"]["kalshi_ticker"] == "kalshi-1"
    assert len(snapshot["directions"]) == 4
    assert snapshot["best_net_edge"] == pytest.approx(0.05)
    assert snapshot["best_direction"]["token"] == "YES"
    assert snapshot["best_direction"]["buy_platform"] == "polymarket"
    assert snapshot["best_direction"]["sell_platform"] == "kalshi"
