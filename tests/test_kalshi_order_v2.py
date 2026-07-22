from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml

from kalshi_client import (
    CreateOrderV2Request,
    KalshiClient,
    KalshiMutationAmbiguousError,
)

ROOT = Path(__file__).resolve().parents[1]


def _order_payload(**overrides):
    payload = {
        "order_id": "order-1",
        "user_id": "user-1",
        "client_order_id": "client-1",
        "ticker": "KXTEST-YES",
        "outcome_side": "yes",
        "book_side": "bid",
        "type": "limit",
        "status": "resting",
        "yes_price_dollars": "0.450000",
        "no_price_dollars": "0.550000",
        "fill_count_fp": "2.50",
        "remaining_count_fp": "7.50",
        "initial_count_fp": "10.00",
        "taker_fees_dollars": "0.000000",
        "maker_fees_dollars": "0.000000",
        "taker_fill_cost_dollars": "0.000000",
        "maker_fill_cost_dollars": "0.000000",
    }
    payload.update(overrides)
    return payload


def test_pinned_specs_contain_required_order_and_lifecycle_contracts():
    rest = yaml.safe_load((ROOT / "specs/kalshi/predictions-openapi.yaml").read_text())
    websocket = yaml.safe_load(
        (ROOT / "specs/kalshi/predictions-asyncapi.yaml").read_text()
    )

    assert rest["openapi"] == "3.0.0"
    assert (
        rest["paths"]["/portfolio/events/orders"]["post"]["operationId"]
        == "CreateOrderV2"
    )
    assert (
        rest["paths"]["/portfolio/events/orders/{order_id}"]["delete"]["operationId"]
        == "CancelOrderV2"
    )
    required = set(rest["components"]["schemas"]["CreateOrderV2Request"]["required"])
    assert {"ticker", "side", "count", "price", "time_in_force"} <= required
    assert websocket["asyncapi"] == "3.0.0"
    assert {"fill", "user_orders"} <= set(websocket["channels"])


def test_create_order_template_is_fixed_point_and_reconciliation_safe():
    request = CreateOrderV2Request(
        ticker="KXTEST-YES",
        client_order_id="stable-attempt-id",
        side="bid",
        count="10.00",
        price="0.450000",
    )

    assert request.to_payload() == {
        "ticker": "KXTEST-YES",
        "client_order_id": "stable-attempt-id",
        "side": "bid",
        "count": "10.00",
        "price": "0.450000",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": True,
        "reduce_only": False,
        "subaccount": 0,
        "exchange_index": 0,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_order_id", ""),
        ("count", "1.001"),
        ("count", "0"),
        ("price", "1e-1"),
        ("price", "1.000001"),
        ("side", "yes"),
    ],
)
def test_create_order_template_rejects_unsafe_or_non_spec_values(field, value):
    kwargs = {
        "ticker": "KXTEST-YES",
        "client_order_id": "client-1",
        "side": "bid",
        "count": "1.00",
        "price": "0.45",
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        CreateOrderV2Request(**kwargs).to_payload()


@pytest.mark.asyncio
async def test_create_order_v2_is_disabled_in_dry_run():
    client = KalshiClient(dry_run=True)
    request = CreateOrderV2Request(
        ticker="KXTEST-YES",
        client_order_id="client-1",
        side="bid",
        count="1.00",
        price="0.45",
    )

    with pytest.raises(RuntimeError, match="disabled in dry-run"):
        await client.create_order_v2(request)


@pytest.mark.asyncio
async def test_create_order_v2_uses_current_endpoint_and_parses_partial_fill(
    monkeypatch,
):
    client = KalshiClient(dry_run=False)
    captured = {}

    async def fake_mutation(method, endpoint, **kwargs):
        captured.update(method=method, endpoint=endpoint, **kwargs)
        return {
            "order_id": "order-1",
            "client_order_id": "client-1",
            "fill_count": "2.50",
            "remaining_count": "7.50",
            "average_fill_price": "0.450000",
            "average_fee_paid": "0.001250",
            "ts_ms": 1715793600123,
        }

    monkeypatch.setattr(client, "_mutating_request_once", fake_mutation)
    result = await client.create_order_v2(
        CreateOrderV2Request(
            ticker="KXTEST-YES",
            client_order_id="client-1",
            side="bid",
            count="10.00",
            price="0.450000",
        )
    )

    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/portfolio/events/orders"
    assert captured["reconciliation_id"] == "client_order_id=client-1"
    assert result.fill_count == Decimal("2.50")
    assert result.remaining_count == Decimal("7.50")


@pytest.mark.asyncio
async def test_mutation_transport_error_is_never_blindly_retried(monkeypatch):
    client = KalshiClient(dry_run=False, max_retries=9)

    class FailingClient:
        calls = 0

        async def request(self, method, url, **kwargs):
            self.calls += 1
            request = httpx.Request(method, url)
            raise httpx.ReadTimeout("outcome unknown", request=request)

    transport = FailingClient()
    client._client = transport
    monkeypatch.setattr(client, "_auth_headers", lambda method, endpoint: {})

    with pytest.raises(KalshiMutationAmbiguousError, match="reconcile"):
        await client._mutating_request_once(
            "POST",
            "/portfolio/events/orders",
            json_body={},
            reconciliation_id="client_order_id=client-1",
        )

    assert transport.calls == 1


@pytest.mark.asyncio
async def test_cancel_order_v2_uses_current_endpoint(monkeypatch):
    client = KalshiClient(dry_run=False)
    captured = {}

    async def fake_mutation(method, endpoint, **kwargs):
        captured.update(method=method, endpoint=endpoint, **kwargs)
        return {
            "order_id": "order/1",
            "client_order_id": "client-1",
            "reduced_by": "7.50",
            "ts_ms": 1715793660456,
        }

    monkeypatch.setattr(client, "_mutating_request_once", fake_mutation)
    result = await client.cancel_order_v2("order/1")

    assert captured["method"] == "DELETE"
    assert captured["endpoint"] == "/portfolio/events/orders/order%2F1"
    assert captured["reconciliation_id"] == "order_id=order/1"
    assert result.reduced_by == Decimal("7.50")


@pytest.mark.asyncio
async def test_get_order_parses_authoritative_partial_fill(monkeypatch):
    client = KalshiClient(dry_run=True)

    async def fake_request(method, endpoint, **kwargs):
        assert method == "GET"
        assert endpoint == "/portfolio/orders/order-1"
        assert kwargs["authenticated"] is True
        return {"order": _order_payload()}

    monkeypatch.setattr(client, "_request", fake_request)
    order = await client.get_order("order-1")

    assert order is not None
    assert order.status == "resting"
    assert order.fill_count == Decimal("2.50")
    assert order.remaining_count == Decimal("7.50")


@pytest.mark.asyncio
async def test_get_orders_builds_typed_reconciliation_page(monkeypatch):
    client = KalshiClient(dry_run=True)

    async def fake_request(method, endpoint, **kwargs):
        assert endpoint == "/portfolio/orders"
        assert kwargs["params"] == {
            "limit": 25,
            "ticker": "KXTEST-YES",
            "status": "resting",
        }
        return {"orders": [_order_payload()], "cursor": "next-page"}

    monkeypatch.setattr(client, "_request", fake_request)
    page = await client.get_orders(ticker="KXTEST-YES", status="resting", limit=25)

    assert page.cursor == "next-page"
    assert len(page.orders) == 1
    assert page.orders[0].client_order_id == "client-1"
