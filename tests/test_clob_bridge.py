"""Tests for Polymarket CLOB bridge parsing helpers."""

from unittest.mock import AsyncMock

import pytest

from polymarket_client.api import PolymarketClient
from polymarket_client.clob_bridge import (
    incremental_fill_trades,
    map_clob_order_status,
    parse_fixed_amount,
    parse_open_order,
)
from polymarket_client.models import Order, OrderSide, OrderStatus, TokenType


def test_parse_fixed_amount():
    assert parse_fixed_amount("1000000") == 1.0
    assert parse_fixed_amount(5_000_000) == 5.0
    assert parse_fixed_amount(None) == 0.0


def test_parse_open_order_maps_exchange_payload():
    order = parse_open_order(
        {
            "id": "0xabc",
            "status": "ORDER_STATUS_LIVE",
            "market": "12345",
            "side": "BUY",
            "outcome": "YES",
            "original_size": "5000000",
            "size_matched": "2000000",
            "price": "0.42",
            "created_at": "1700000000",
        },
        strategy_tag="bundle_arb",
    )

    assert order.order_id == "0xabc"
    assert order.market_id == "12345"
    assert order.token_type == TokenType.YES
    assert order.side == OrderSide.BUY
    assert order.size == 5.0
    assert order.filled_size == 2.0
    assert order.price == 0.42
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.strategy_tag == "bundle_arb"


def test_map_clob_order_status_filled():
    assert map_clob_order_status("ORDER_STATUS_LIVE", 5.0, 5.0) == OrderStatus.FILLED


def test_incremental_fill_trades_emits_delta_only():
    order = Order(
        order_id="0xabc",
        market_id="m1",
        token_type=TokenType.YES,
        side=OrderSide.BUY,
        price=0.5,
        size=10.0,
        filled_size=3.0,
        status=OrderStatus.PARTIALLY_FILLED,
    )

    trades = incremental_fill_trades(order, previous_filled_size=1.0, fee_rate=0.015)
    assert len(trades) == 1
    assert trades[0].size == 2.0
    assert trades[0].fee == 2.0 * 0.5 * 0.015
    assert trades[0].is_simulated is False

    assert incremental_fill_trades(order, previous_filled_size=3.0) == []


@pytest.mark.asyncio
async def test_live_open_order_read_fails_closed_without_trading_bridge():
    client = PolymarketClient(dry_run=False)

    with pytest.raises(RuntimeError, match="trading bridge is not initialized"):
        await client.get_open_orders()


@pytest.mark.asyncio
async def test_live_position_read_does_not_convert_failure_to_empty_account():
    client = PolymarketClient(dry_run=False)
    client._request = AsyncMock(side_effect=TimeoutError("venue unavailable"))

    with pytest.raises(RuntimeError, match="authoritative live positions"):
        await client.get_positions()
