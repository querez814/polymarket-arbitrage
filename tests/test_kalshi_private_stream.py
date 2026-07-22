from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_client.private_stream import (
    KalshiFillEvent,
    KalshiPrivateStream,
    KalshiUserOrderEvent,
    parse_private_event,
)


def _fill_message():
    return json.dumps(
        {
            "type": "fill",
            "sid": 1,
            "msg": {
                "trade_id": "trade-1",
                "order_id": "order-1",
                "market_ticker": "TICKER",
                "client_order_id": "client-1",
                "is_taker": True,
                "side": "yes",
                "yes_price_dollars": "0.6500",
                "count_fp": "2.00",
                "fee_cost": "0.0100",
                "action": "buy",
                "outcome_side": "yes",
                "book_side": "bid",
                "ts": 1,
                "ts_ms": 1000,
                "post_position_fp": "2.00",
                "purchased_side": "yes",
            },
        }
    )


def _user_order_message():
    return json.dumps(
        {
            "type": "user_order",
            "sid": 2,
            "msg": {
                "order_id": "order-1",
                "client_order_id": "client-1",
                "ticker": "TICKER",
                "status": "canceled",
                "outcome_side": "yes",
                "book_side": "bid",
                "fill_count_fp": "2.00",
                "remaining_count_fp": "0.00",
                "initial_count_fp": "2.00",
                "last_updated_ts_ms": 1001,
            },
        }
    )


def test_private_stream_parses_fill_and_user_order_payloads():
    fill = parse_private_event(_fill_message())
    order = parse_private_event(_user_order_message())

    assert isinstance(fill, KalshiFillEvent) and fill.count == 2
    assert isinstance(order, KalshiUserOrderEvent) and order.status == "canceled"
    assert parse_private_event('{"type":"subscribed","id":1}') is None


class FakeSocket:
    def __init__(self, events):
        self.events = events
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        return json.dumps({"type": "subscribed", "id": 1})

    def __aiter__(self):
        async def generate():
            for event in self.events:
                yield event
            raise OSError("disconnect")

        return generate()


class FakeConnection:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_reconnect_path_subscribes_then_reconciles_before_dispatch(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "kalshi.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    socket = FakeSocket([_fill_message(), _fill_message(), _user_order_message()])
    actions = []

    def connector(*args, **kwargs):
        actions.append("connect")
        assert "KALSHI-ACCESS-SIGNATURE" in kwargs["additional_headers"]
        return FakeConnection(socket)

    async def reconcile():
        actions.append("reconcile")

    async def on_event(event):
        actions.append(("event", event.dedupe_key))

    async def on_disconnect():
        actions.append("disconnect")

    async def sleeper(delay):
        actions.append(("sleep", delay))
        raise asyncio.CancelledError

    stream = KalshiPrivateStream(
        api_key_id="api-key",
        private_key_path=str(path),
        connector=connector,
        sleeper=sleeper,
    )
    with pytest.raises(asyncio.CancelledError):
        await stream.run(
            on_event=on_event,
            reconcile_rest=reconcile,
            on_disconnect=on_disconnect,
        )

    assert socket.sent[0]["params"]["channels"] == ["fill", "user_orders"]
    assert actions[1] == "reconcile"
    assert "disconnect" in actions
    assert len([item for item in actions if isinstance(item, tuple) and item[0] == "event"]) == 2
