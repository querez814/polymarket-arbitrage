"""Tests for Polymarket US payload parsing."""

from polymarket_client.models import OrderSide, OrderStatus, TokenType
from polymarket_us_client.parsing import (
    group_us_markets,
    map_us_order_state,
    parse_amount,
    parse_us_order,
    parse_us_orderbook,
)


def test_parse_amount_handles_usd_object():
    assert parse_amount({"value": "0.55", "currency": "USD"}) == 0.55


def test_group_us_markets_pairs_event_outcomes():
    details = [
        {
            "id": 1,
            "slug": "event-yes",
            "eventSlug": "super-bowl-winner",
            "title": "Will Team A win?",
            "outcome": "Yes",
            "active": True,
            "closed": False,
            "volume": 1000,
            "liquidity": 500,
        },
        {
            "id": 2,
            "slug": "event-no",
            "eventSlug": "super-bowl-winner",
            "title": "Will Team B win?",
            "outcome": "No",
            "active": True,
            "closed": False,
            "volume": 900,
            "liquidity": 400,
        },
    ]

    markets = group_us_markets(details)
    assert len(markets) == 1
    market = markets[0]
    assert market.market_id == "super-bowl-winner"
    assert market.yes_token_id == "event-yes"
    assert market.no_token_id == "event-no"


def test_parse_us_orderbook_maps_offers_to_asks():
    book = parse_us_orderbook(
        "event-1",
        {"bids": [{"px": {"value": "0.40", "currency": "USD"}, "qty": "10"}], "offers": [{"px": {"value": "0.42", "currency": "USD"}, "qty": "8"}]},
        {"bids": [{"px": {"value": "0.58", "currency": "USD"}, "qty": "12"}], "offers": [{"px": {"value": "0.60", "currency": "USD"}, "qty": "6"}]},
    )

    assert book.best_ask_yes == 0.42
    assert book.best_bid_no == 0.58


def test_parse_us_order_partial_fill():
    order = parse_us_order(
        {
            "order": {
                "id": "ord_1",
                "marketSlug": "event-yes",
                "side": "ORDER_SIDE_BUY",
                "price": {"value": "0.50", "currency": "USD"},
                "quantity": 10,
                "cumQuantity": 4,
                "state": "ORDER_STATE_PARTIALLY_FILLED",
            }
        },
        market_id="super-bowl-winner",
        token_type=TokenType.YES,
    )

    assert order.order_id == "ord_1"
    assert order.side == OrderSide.BUY
    assert order.filled_size == 4
    assert order.status == OrderStatus.PARTIALLY_FILLED


def test_map_us_order_state_filled():
    assert map_us_order_state("ORDER_STATE_FILLED", 10, 10) == OrderStatus.FILLED
