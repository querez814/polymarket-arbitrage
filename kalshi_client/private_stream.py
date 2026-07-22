"""Authenticated Kalshi fill/user-order stream with REST gap reconciliation."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import time
from typing import Any, Awaitable, Callable, Mapping

import websockets
from websockets.exceptions import ConnectionClosed

from kalshi_client.auth import auth_headers, load_private_key


@dataclass(frozen=True)
class KalshiFillEvent:
    trade_id: str
    order_id: str
    market_ticker: str
    client_order_id: str | None
    count: Decimal
    yes_price: Decimal
    post_position: Decimal
    outcome_side: str
    book_side: str
    ts_ms: int

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return ("fill", self.trade_id)


@dataclass(frozen=True)
class KalshiUserOrderEvent:
    order_id: str
    client_order_id: str
    market_ticker: str
    status: str
    fill_count: Decimal
    remaining_count: Decimal
    initial_count: Decimal
    outcome_side: str
    book_side: str
    last_updated_ts_ms: int | None

    @property
    def dedupe_key(self) -> tuple[str, str, str, str, str]:
        return (
            "user_order",
            self.order_id,
            self.status,
            str(self.fill_count),
            str(self.remaining_count),
        )


KalshiPrivateEvent = KalshiFillEvent | KalshiUserOrderEvent
EventHandler = Callable[[KalshiPrivateEvent], Awaitable[None]]
ReconcileHandler = Callable[[], Awaitable[None]]
DisconnectHandler = Callable[[], Awaitable[None]]


def _string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _decimal(payload: Mapping[str, Any], field: str) -> Decimal:
    value = payload.get(field)
    if not isinstance(value, str) or not value or "e" in value.lower():
        raise ValueError(f"{field} must be fixed-point text")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be fixed-point text") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def parse_private_event(raw: str | bytes) -> KalshiPrivateEvent | None:
    """Strictly parse private lifecycle messages; ignore control messages."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Kalshi WebSocket message must be an object")
    message_type = value.get("type")
    if message_type not in {"fill", "user_order"}:
        return None
    msg = value.get("msg")
    if not isinstance(msg, dict):
        raise ValueError("Kalshi private message must contain msg object")

    if message_type == "fill":
        outcome_side = _string(msg, "outcome_side")
        book_side = _string(msg, "book_side")
        ts_ms = msg.get("ts_ms")
        if outcome_side not in {"yes", "no"} or book_side not in {"bid", "ask"}:
            raise ValueError("Kalshi fill direction is invalid")
        if not isinstance(ts_ms, int) or ts_ms <= 0:
            raise ValueError("Kalshi fill ts_ms must be positive")
        client_order_id = msg.get("client_order_id")
        if client_order_id is not None and not isinstance(client_order_id, str):
            raise ValueError("client_order_id must be text when present")
        return KalshiFillEvent(
            trade_id=_string(msg, "trade_id"),
            order_id=_string(msg, "order_id"),
            market_ticker=_string(msg, "market_ticker"),
            client_order_id=client_order_id,
            count=_decimal(msg, "count_fp"),
            yes_price=_decimal(msg, "yes_price_dollars"),
            post_position=_decimal(msg, "post_position_fp"),
            outcome_side=outcome_side,
            book_side=book_side,
            ts_ms=ts_ms,
        )

    status = _string(msg, "status")
    outcome_side = _string(msg, "outcome_side")
    book_side = _string(msg, "book_side")
    if status not in {"resting", "canceled", "executed"}:
        raise ValueError("Kalshi user-order status is invalid")
    if outcome_side not in {"yes", "no"} or book_side not in {"bid", "ask"}:
        raise ValueError("Kalshi user-order direction is invalid")
    updated = msg.get("last_updated_ts_ms")
    if updated is not None and (not isinstance(updated, int) or updated <= 0):
        raise ValueError("last_updated_ts_ms must be positive when present")
    return KalshiUserOrderEvent(
        order_id=_string(msg, "order_id"),
        client_order_id=_string(msg, "client_order_id"),
        market_ticker=_string(msg, "ticker"),
        status=status,
        fill_count=_decimal(msg, "fill_count_fp"),
        remaining_count=_decimal(msg, "remaining_count_fp"),
        initial_count=_decimal(msg, "initial_count_fp"),
        outcome_side=outcome_side,
        book_side=book_side,
        last_updated_ts_ms=updated,
    )


class KalshiPrivateStream:
    """Reconnect private streams and reconcile REST before consuming updates."""

    WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    SIGN_PATH = "/trade-api/ws/v2"

    def __init__(
        self,
        *,
        api_key_id: str,
        private_key_path: str,
        ws_url: str = WS_URL,
        max_seen_events: int = 10_000,
        connector: Callable[..., Any] = websockets.connect,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key_id.strip():
            raise ValueError("api_key_id is required")
        if max_seen_events <= 0:
            raise ValueError("max_seen_events must be positive")
        self._api_key_id = api_key_id
        self._private_key = load_private_key(private_key_path)
        self._ws_url = ws_url
        self._connector = connector
        self._sleeper = sleeper
        self._max_seen = max_seen_events
        self._seen_queue: deque[tuple[Any, ...]] = deque()
        self._seen: set[tuple[Any, ...]] = set()

    def handshake_headers(self) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        return auth_headers(
            self._private_key,
            self._api_key_id,
            timestamp_ms,
            "GET",
            self.SIGN_PATH,
        )

    async def run(
        self,
        *,
        on_event: EventHandler,
        reconcile_rest: ReconcileHandler,
        on_disconnect: DisconnectHandler | None = None,
        market_tickers: tuple[str, ...] = (),
        max_reconnect_delay: float = 30.0,
    ) -> None:
        """Run until cancelled; reconnects require a fresh REST reconciliation."""
        delay = 1.0
        while True:
            try:
                async with self._connector(
                    self._ws_url,
                    additional_headers=self.handshake_headers(),
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    await self._subscribe(socket, market_tickers)
                    # Subscription is active before the authoritative snapshot;
                    # messages arriving during REST reconciliation queue in the
                    # socket and are consumed afterward.
                    await reconcile_rest()
                    delay = 1.0
                    async for raw in socket:
                        event = parse_private_event(raw)
                        if event is not None and self._remember(event.dedupe_key):
                            await on_event(event)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, TimeoutError):
                if on_disconnect is not None:
                    await on_disconnect()
                await self._sleeper(delay)
                delay = min(max_reconnect_delay, delay * 2)

    async def _subscribe(self, socket: Any, market_tickers: tuple[str, ...]) -> None:
        params: dict[str, Any] = {"channels": ["fill", "user_orders"]}
        if market_tickers:
            params["market_tickers"] = list(market_tickers)
        await socket.send(json.dumps({"id": 1, "cmd": "subscribe", "params": params}))
        while True:
            raw = await socket.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError("Kalshi subscription response must be an object")
            if response.get("type") == "error":
                raise RuntimeError("Kalshi private-stream subscription rejected")
            if response.get("type") == "subscribed" and response.get("id") == 1:
                return

    def _remember(self, key: tuple[Any, ...]) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_queue.append(key)
        while len(self._seen_queue) > self._max_seen:
            self._seen.remove(self._seen_queue.popleft())
        return True
