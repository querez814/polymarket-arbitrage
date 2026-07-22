"""Production execution/recovery adapter for Kalshi Predictions V2."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from core.execution_recovery import (
    AuthoritativeOrder,
    AuthoritativePosition,
    OrderLookup,
)
from core.two_leg_execution import LegIntent, LegPhase, LegSide
from core.venue_execution import (
    PreparedVenueOrder,
    VenueMutationAmbiguousError,
)
from kalshi_client.api import KalshiClient, KalshiMutationAmbiguousError
from kalshi_client.orders import CreateOrderV2Request, KalshiOrder


def _plain_decimal(value: float) -> str:
    decimal = Decimal(str(value))
    return format(decimal, "f")


def _phase(order: KalshiOrder) -> LegPhase:
    if order.status == "resting":
        return LegPhase.OPEN
    if order.status == "executed":
        return LegPhase.FILLED
    if order.status == "canceled":
        return LegPhase.CANCELLED
    raise ValueError(f"unsupported Kalshi order status: {order.status}")


def _authoritative(order: KalshiOrder) -> AuthoritativeOrder:
    return AuthoritativeOrder(
        venue="kalshi",
        market_id=order.ticker,
        idempotency_key=order.client_order_id,
        venue_order_id=order.order_id,
        phase=_phase(order),
        cumulative_filled_size=float(order.fill_count),
    )


class KalshiVenueAdapter:
    """Map the current fixed-point Kalshi API to durable execution contracts."""

    def __init__(self, client: KalshiClient, *, max_pages: int = 100) -> None:
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        self._client = client
        self._max_pages = max_pages

    async def available_collateral(self) -> float | None:
        return await self._client.get_balance_dollars()

    async def prepare_ioc(
        self,
        intent: LegIntent,
        *,
        idempotency_key: str,
        size: float,
    ) -> PreparedVenueOrder:
        if intent.venue.strip().lower() != "kalshi":
            raise ValueError("Kalshi adapter received another venue")
        request = CreateOrderV2Request(
            ticker=intent.market_id,
            client_order_id=idempotency_key,
            side="bid" if intent.side is LegSide.BUY else "ask",
            count=_plain_decimal(size),
            price=_plain_decimal(intent.limit_price),
            time_in_force="immediate_or_cancel",
            self_trade_prevention_type="taker_at_cross",
            cancel_order_on_pause=True,
        )
        request.to_payload()
        return PreparedVenueOrder(
            venue="kalshi",
            market_id=intent.market_id,
            idempotency_key=idempotency_key,
            requested_size=size,
            venue_order_id=None,
            payload=request,
        )

    async def submit_prepared(
        self, prepared: PreparedVenueOrder
    ) -> AuthoritativeOrder:
        if not isinstance(prepared.payload, CreateOrderV2Request):
            raise TypeError("Kalshi prepared payload is invalid")
        try:
            result = await self._client.create_order_v2(prepared.payload)
        except KalshiMutationAmbiguousError as exc:
            raise VenueMutationAmbiguousError(str(exc)) from exc
        client_id = result.client_order_id or prepared.idempotency_key
        filled = float(result.fill_count)
        remaining = float(result.remaining_count)
        phase = LegPhase.FILLED if remaining == 0 and filled > 0 else LegPhase.CANCELLED
        return AuthoritativeOrder(
            venue="kalshi",
            market_id=prepared.market_id,
            idempotency_key=client_id,
            venue_order_id=result.order_id,
            phase=phase,
            cumulative_filled_size=filled,
        )

    async def cancel_open(
        self, order: AuthoritativeOrder
    ) -> AuthoritativeOrder:
        if not order.venue_order_id:
            raise ValueError("Kalshi cancellation requires venue_order_id")
        try:
            await self._client.cancel_order_v2(
                order.venue_order_id, market_ticker=order.market_id
            )
        except KalshiMutationAmbiguousError as exc:
            raise VenueMutationAmbiguousError(str(exc)) from exc
        observed = await self._client.get_order(order.venue_order_id)
        if observed is None:
            raise VenueMutationAmbiguousError(
                "Kalshi cancellation succeeded but order reconciliation failed"
            )
        return _authoritative(observed)

    async def read_order(self, lookup: OrderLookup) -> AuthoritativeOrder | None:
        if lookup.venue.strip().lower() != "kalshi":
            raise ValueError("Kalshi adapter received another venue")
        if lookup.venue_order_id:
            order = await self._client.get_order(lookup.venue_order_id)
            return _authoritative(order) if order else None
        for status in ("resting", "canceled", "executed"):
            cursor: str | None = None
            seen: set[str] = set()
            for _ in range(self._max_pages):
                page = await self._client.get_orders(
                    ticker=lookup.market_id,
                    status=status,
                    limit=1000,
                    cursor=cursor,
                )
                matches = [
                    item
                    for item in page.orders
                    if item.client_order_id == lookup.idempotency_key
                ]
                if len(matches) > 1:
                    raise ValueError("Kalshi client order id is not unique")
                if matches:
                    return _authoritative(matches[0])
                if not page.cursor:
                    break
                if page.cursor in seen:
                    raise ValueError("Kalshi order pagination cursor repeated")
                seen.add(page.cursor)
                cursor = page.cursor
            else:
                raise RuntimeError("Kalshi order pagination exceeded max_pages")
        return None

    async def list_open_orders(self) -> tuple[AuthoritativeOrder, ...]:
        return tuple(_authoritative(item) for item in await self._all_resting_orders())

    async def list_positions(self) -> tuple[AuthoritativePosition, ...]:
        raw_positions = await self._client.get_all_positions(
            max_pages=self._max_pages
        )
        result: list[AuthoritativePosition] = []
        for raw in raw_positions:
            if not isinstance(raw, dict):
                raise ValueError("Kalshi market position must be an object")
            ticker = raw.get("ticker")
            position = raw.get("position_fp")
            if not isinstance(ticker, str) or not ticker:
                raise ValueError("Kalshi market position ticker is missing")
            if not isinstance(position, str):
                raise ValueError("Kalshi market position must be fixed-point text")
            size = float(Decimal(position))
            if size:
                result.append(AuthoritativePosition(ticker, size))
        return tuple(result)

    async def _all_resting_orders(self) -> tuple[KalshiOrder, ...]:
        orders: list[KalshiOrder] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(self._max_pages):
            page = await self._client.get_orders(
                status="resting", limit=1000, cursor=cursor
            )
            orders.extend(page.orders)
            if not page.cursor:
                return tuple(orders)
            if page.cursor in seen:
                raise ValueError("Kalshi order pagination cursor repeated")
            seen.add(page.cursor)
            cursor = page.cursor
        raise RuntimeError("Kalshi order pagination exceeded max_pages")
