from datetime import timedelta

import pytest

from scripts.backtest_cross_platform import _parse_pair, _read_pairs_file
from core.cross_platform_arb import CrossPlatformArbEngine, MarketPair
from polymarket_client.models import OrderBook, OrderBookSide, PriceLevel, TokenOrderBook, TokenType
from utils.cross_platform_backtest import (
    HistoricalRecord,
    make_all_pairs,
    parse_timestamp,
    run_cross_platform_replay,
)


def record(
    *,
    timestamp="2026-06-24T12:00:00Z",
    platform,
    market_id,
    token="YES",
    price=None,
    bid=None,
    ask=None,
    volume=100.0,
):
    return HistoricalRecord(
        timestamp=parse_timestamp(timestamp),
        platform=platform,
        market_id=market_id,
        token=token,
        price=price,
        bid=bid,
        ask=ask,
        volume=volume,
        source="test",
        raw={},
    )


def test_replay_detects_cross_platform_yes_opportunity():
    polymarket_records = [
        record(platform="polymarket", market_id="poly-1", token="YES", price=0.40),
        record(platform="polymarket", market_id="poly-1", token="NO", price=0.60),
    ]
    kalshi_records = [
        record(platform="kalshi", market_id="kalshi-1", token="YES", bid=0.55, ask=0.57, price=0.56),
    ]

    result = run_cross_platform_replay(
        polymarket_records=polymarket_records,
        kalshi_records=kalshi_records,
        pairs=[("poly-1", "kalshi-1")],
        min_edge=0.01,
        max_time_delta=timedelta(minutes=5),
        assumed_spread=0.02,
        default_liquidity=100.0,
        polymarket_taker_fee=0.0,
        kalshi_taker_fee=0.0,
        gas_cost=0.0,
    )

    assert result.evaluations == 1
    assert result.opportunity_count == 1
    opportunity = result.opportunities[0]
    assert opportunity.token == "YES"
    assert opportunity.buy_platform == "polymarket"
    assert opportunity.sell_platform == "kalshi"
    assert opportunity.net_edge == 0.14


def test_replay_skips_pair_without_overlapping_timestamps():
    polymarket_records = [
        record(timestamp="2026-06-24T12:00:00Z", platform="polymarket", market_id="poly-1", token="YES", price=0.40),
        record(timestamp="2026-06-24T12:00:00Z", platform="polymarket", market_id="poly-1", token="NO", price=0.60),
    ]
    kalshi_records = [
        record(timestamp="2026-06-24T13:00:00Z", platform="kalshi", market_id="kalshi-1", token="YES", bid=0.55, ask=0.57, price=0.56),
    ]

    result = run_cross_platform_replay(
        polymarket_records=polymarket_records,
        kalshi_records=kalshi_records,
        pairs=[("poly-1", "kalshi-1")],
        min_edge=0.01,
        max_time_delta=timedelta(minutes=5),
        assumed_spread=0.02,
        default_liquidity=100.0,
        polymarket_taker_fee=0.0,
        kalshi_taker_fee=0.0,
        gas_cost=0.0,
    )

    assert result.evaluations == 0
    assert result.opportunity_count == 0
    assert result.skipped_pairs == {"poly-1:kalshi-1": "no overlapping timestamps"}


def test_make_all_pairs_uses_markets_from_both_platforms():
    pairs = make_all_pairs(
        [record(platform="polymarket", market_id="poly-1", price=0.4)],
        [record(platform="kalshi", market_id="kalshi-1", bid=0.5, ask=0.6)],
    )

    assert pairs == [("poly-1", "kalshi-1")]


def test_parse_pair_rejects_placeholder_values():
    with pytest.raises(Exception, match="replace POLYMARKET_ID"):
        _parse_pair("POLYMARKET_ID:KALSHI_TICKER")


def test_read_pairs_file_supports_colon_and_csv_lines(tmp_path):
    pairs_file = tmp_path / "pairs.txt"
    pairs_file.write_text(
        "# comments are ignored\n"
        "poly-1:kalshi-1\n"
        "poly-2, kalshi-2\n",
        encoding="utf-8",
    )

    assert _read_pairs_file(pairs_file) == [
        ("poly-1", "kalshi-1"),
        ("poly-2", "kalshi-2"),
    ]


def token_book(token_type, bid, ask, bid_size=100, ask_size=100):
    return TokenOrderBook(
        token_type=token_type,
        bids=OrderBookSide(levels=[PriceLevel(price=bid, size=bid_size)]),
        asks=OrderBookSide(levels=[PriceLevel(price=ask, size=ask_size)]),
    )


def xplat_orderbook(market_id, yes_bid, yes_ask, no_bid, no_ask):
    return OrderBook(
        market_id=market_id,
        yes=token_book(TokenType.YES, yes_bid, yes_ask),
        no=token_book(TokenType.NO, no_bid, no_ask),
    )


def test_cross_platform_engine_returns_multiple_qualifying_directions():
    engine = CrossPlatformArbEngine(
        min_edge=0.01,
        polymarket_taker_fee=0.0,
        kalshi_taker_fee=0.0,
        gas_cost=0.0,
        max_order_size=30.0,
        edge_size_multiplier=2.0,
        max_liquidity_fraction=0.5,
    )
    pair = MarketPair("poly-1", "kalshi-1", "Poly question", "Kalshi title", 1.0)
    polymarket_ob = xplat_orderbook("poly-1", yes_bid=0.60, yes_ask=0.40, no_bid=0.60, no_ask=0.40)
    kalshi_ob = xplat_orderbook("kalshi:kalshi-1", yes_bid=0.55, yes_ask=0.45, no_bid=0.55, no_ask=0.45)

    opportunities = engine.check_arbitrages(pair, polymarket_ob, kalshi_ob)

    assert {(opp.token, opp.buy_platform, opp.sell_platform) for opp in opportunities} == {
        ("YES", "polymarket", "kalshi"),
        ("YES", "kalshi", "polymarket"),
        ("NO", "polymarket", "kalshi"),
        ("NO", "kalshi", "polymarket"),
    }
    assert all(opp.suggested_size <= 30.0 for opp in opportunities)
    assert all(opp.suggested_size <= 50.0 for opp in opportunities)
