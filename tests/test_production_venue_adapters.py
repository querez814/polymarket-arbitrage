from __future__ import annotations

from decimal import Decimal

import pytest

from core.execution_recovery import OrderLookup
from core.two_leg_execution import LegIntent, LegPhase, LegSide
from kalshi_client.orders import (
    CreateOrderV2Result,
    KalshiOrder,
    KalshiOrdersPage,
)
from kalshi_client.venue_adapter import KalshiVenueAdapter
from polymarket_client.clob_bridge import PreparedClobOrder
from polymarket_client.models import OrderSide, Position, TokenType
from polymarket_client.venue_adapter import (
    PolymarketVenueAdapter,
    idempotency_metadata,
)


def _kalshi_order(*, status="canceled", fill="4.00"):
    return KalshiOrder(
        order_id="kalshi-order",
        client_order_id="11111111-1111-1111-1111-111111111111",
        ticker="TICKER",
        outcome_side="no",
        book_side="ask",
        status=status,
        yes_price_dollars=Decimal("0.6500"),
        no_price_dollars=Decimal("0.3500"),
        fill_count=Decimal(fill),
        remaining_count=Decimal("0.00"),
        initial_count=Decimal("4.00"),
    )


class FakeKalshiClient:
    async def create_order_v2(self, request):
        self.request = request
        return CreateOrderV2Result(
            order_id="kalshi-order",
            client_order_id=request.client_order_id,
            fill_count=Decimal("4.00"),
            remaining_count=Decimal("0.00"),
            average_fill_price=Decimal("0.6500"),
            average_fee_paid=Decimal("0.0010"),
            ts_ms=1,
        )

    async def get_order(self, order_id):
        return _kalshi_order()

    async def get_orders(self, **kwargs):
        return KalshiOrdersPage((_kalshi_order(),), "")

    async def get_all_positions(self, **kwargs):
        return [{"ticker": "TICKER", "position_fp": "-4.00"}]

    async def cancel_order_v2(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_kalshi_adapter_maps_normalized_sell_to_v2_ask_ioc():
    client = FakeKalshiClient()
    adapter = KalshiVenueAdapter(client)  # type: ignore[arg-type]
    intent = LegIntent("k", "kalshi", "TICKER", LegSide.SELL, 0.65, 10.0)
    key = "11111111-1111-1111-1111-111111111111"

    prepared = await adapter.prepare_ioc(intent, idempotency_key=key, size=4.0)
    observed = await adapter.submit_prepared(prepared)

    assert prepared.payload.side == "ask"
    assert prepared.payload.time_in_force == "immediate_or_cancel"
    assert prepared.payload.count == "4.0"
    assert observed.phase is LegPhase.FILLED
    assert observed.cumulative_filled_size == 4.0


@pytest.mark.asyncio
async def test_kalshi_adapter_recovers_by_client_id_and_maps_signed_position():
    adapter = KalshiVenueAdapter(FakeKalshiClient())  # type: ignore[arg-type]
    lookup = OrderLookup(
        "kalshi", "TICKER", "11111111-1111-1111-1111-111111111111", None
    )

    observed = await adapter.read_order(lookup)
    positions = await adapter.list_positions()

    assert observed is not None and observed.venue_order_id == "kalshi-order"
    assert positions[0].signed_size == -4.0


class FakeBridge:
    def __init__(self):
        self.prepared = None

    async def prepare_ioc_order(self, **kwargs):
        self.prepared = kwargs
        return PreparedClobOrder(
            signed_order=object(),
            order_id="0xorder",
            metadata=kwargs["metadata"],
            token_id=kwargs["token_id"],
            side=kwargs["side"],
            price=kwargs["price"],
            size=kwargs["size"],
        )

    async def submit_ioc_order(self, prepared):
        return {"orderID": prepared.order_id}

    async def get_order(self, order_id):
        return {
            "id": order_id,
            "market": "condition-1",
            "status": "ORDER_STATUS_MATCHED",
            "original_size": "4000000",
            "size_matched": "4000000",
        }

    async def get_open_orders(self):
        return []

    async def cancel_order(self, order_id):
        return {"canceled": [order_id], "not_canceled": {}}


class FakePolymarketClient:
    def __init__(self):
        self._clob_bridge = FakeBridge()

    def resolve_token_id(self, market_id, token_type):
        return "yes-token" if token_type is TokenType.YES else "no-token"

    async def get_positions(self):
        return {
            "condition-1": {
                TokenType.YES: Position("condition-1", TokenType.YES, 2.0),
                TokenType.NO: Position("condition-1", TokenType.NO, 5.0),
            }
        }


@pytest.mark.asyncio
async def test_polymarket_adapter_precomputes_hash_and_buys_complement_for_sell():
    client = FakePolymarketClient()
    adapter = PolymarketVenueAdapter(client)  # type: ignore[arg-type]
    key = "11111111-1111-1111-1111-111111111111"
    intent = LegIntent("p", "polymarket", "condition-1", LegSide.SELL, 0.65, 10.0)

    prepared = await adapter.prepare_ioc(intent, idempotency_key=key, size=4.0)
    observed = await adapter.submit_prepared(prepared)

    assert prepared.venue_order_id == "0xorder"
    assert client._clob_bridge.prepared["token_id"] == "no-token"
    assert client._clob_bridge.prepared["side"] is OrderSide.BUY
    assert client._clob_bridge.prepared["price"] == pytest.approx(0.35)
    assert client._clob_bridge.prepared["metadata"] == idempotency_metadata(key)
    assert observed.phase is LegPhase.FILLED


@pytest.mark.asyncio
async def test_polymarket_adapter_pre_post_crash_is_provably_zero_mutation():
    adapter = PolymarketVenueAdapter(FakePolymarketClient())  # type: ignore[arg-type]
    lookup = OrderLookup(
        "polymarket",
        "condition-1",
        "11111111-1111-1111-1111-111111111111",
        None,
    )

    observed = await adapter.read_order(lookup)
    positions = await adapter.list_positions()

    assert observed is not None and observed.phase is LegPhase.REJECTED
    assert observed.cumulative_filled_size == 0
    assert positions[0].signed_size == -3.0

