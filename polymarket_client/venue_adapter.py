"""Production execution/recovery adapter for the Polymarket Global CLOB."""

from __future__ import annotations

from typing import Any, Mapping
import uuid

from core.execution_recovery import AuthoritativeOrder, AuthoritativePosition, OrderLookup
from core.two_leg_execution import LegIntent, LegPhase, LegSide
from core.venue_execution import PreparedVenueOrder, VenueMutationAmbiguousError
from polymarket_client.api import PolymarketClient
from polymarket_client.clob_bridge import (
    FIXED_DECIMALS,
    PolymarketMutationAmbiguousError,
    PreparedClobOrder,
)
from polymarket_client.models import OrderSide, TokenType


def idempotency_metadata(idempotency_key: str) -> str:
    """Encode one UUID idempotency key into signed V2 bytes32 metadata."""
    return "0x" + uuid.UUID(idempotency_key).hex.ljust(64, "0")


def _fixed_size(value: object, field: str) -> float:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"Polymarket {field} must be fixed-math text")
    return int(value) / FIXED_DECIMALS


def _phase(payload: Mapping[str, Any], filled: float, original: float) -> LegPhase:
    status = str(payload.get("status") or "").upper()
    if filled >= original and original > 0:
        return LegPhase.FILLED
    if "LIVE" in status or "OPEN" in status:
        return LegPhase.OPEN
    if "CANCEL" in status or "EXPIRE" in status:
        return LegPhase.CANCELLED
    if "REJECT" in status or "INVALID" in status:
        return LegPhase.REJECTED
    if "MATCH" in status and filled > 0:
        return LegPhase.CANCELLED
    raise ValueError(f"unsupported Polymarket order status: {status or '<empty>'}")


def _authoritative(
    payload: Mapping[str, Any], *, idempotency_key: str | None
) -> AuthoritativeOrder:
    order_id = str(payload.get("id") or payload.get("orderID") or "")
    market_id = str(payload.get("market") or "")
    if not order_id or not market_id:
        raise ValueError("Polymarket order identity is incomplete")
    original = _fixed_size(payload.get("original_size"), "original_size")
    filled = _fixed_size(payload.get("size_matched"), "size_matched")
    return AuthoritativeOrder(
        venue="polymarket",
        market_id=market_id,
        idempotency_key=idempotency_key,
        venue_order_id=order_id,
        phase=_phase(payload, filled, original),
        cumulative_filled_size=filled,
    )


class PolymarketVenueAdapter:
    """Use signed V2 order hashes to close the POST-response crash window."""

    def __init__(self, client: PolymarketClient) -> None:
        self._client = client

    def _bridge(self):
        bridge = self._client._clob_bridge
        if bridge is None:
            raise RuntimeError("live Polymarket CLOB bridge is not initialized")
        return bridge

    async def available_collateral(self) -> float | None:
        return await self._client.get_usdc_balance()

    async def prepare_ioc(
        self,
        intent: LegIntent,
        *,
        idempotency_key: str,
        size: float,
    ) -> PreparedVenueOrder:
        if intent.venue.strip().lower() != "polymarket":
            raise ValueError("Polymarket adapter received another venue")
        # A normalized SELL buys the complementary NO token. This avoids naked
        # shorting and keeps normalized YES exposure negative.
        token_type = TokenType.YES if intent.side is LegSide.BUY else TokenType.NO
        token_id = self._client.resolve_token_id(intent.market_id, token_type)
        price = intent.limit_price if intent.side is LegSide.BUY else 1.0 - intent.limit_price
        prepared = await self._bridge().prepare_ioc_order(
            token_id=token_id,
            side=OrderSide.BUY,
            price=price,
            size=size,
            metadata=idempotency_metadata(idempotency_key),
        )
        return PreparedVenueOrder(
            venue="polymarket",
            market_id=intent.market_id,
            idempotency_key=idempotency_key,
            requested_size=size,
            venue_order_id=prepared.order_id,
            payload=prepared,
        )

    async def submit_prepared(self, prepared: PreparedVenueOrder) -> AuthoritativeOrder:
        if not isinstance(prepared.payload, PreparedClobOrder):
            raise TypeError("Polymarket prepared payload is invalid")
        try:
            await self._bridge().submit_ioc_order(prepared.payload)
        except PolymarketMutationAmbiguousError as exc:
            raise VenueMutationAmbiguousError(str(exc)) from exc
        payload = await self._read_raw_order(prepared.payload.order_id)
        if payload is None:
            raise VenueMutationAmbiguousError(
                "Polymarket POST returned but its precomputed order id is not authoritative"
            )
        return _authoritative(payload, idempotency_key=prepared.idempotency_key)

    async def cancel_open(self, order: AuthoritativeOrder) -> AuthoritativeOrder:
        if not order.venue_order_id:
            raise ValueError("Polymarket cancellation requires venue_order_id")
        try:
            await self._bridge().cancel_order(order.venue_order_id)
        except PolymarketMutationAmbiguousError as exc:
            raise VenueMutationAmbiguousError(str(exc)) from exc
        payload = await self._read_raw_order(order.venue_order_id)
        if payload is None:
            raise VenueMutationAmbiguousError("Polymarket cancellation could not be reconciled")
        return _authoritative(payload, idempotency_key=order.idempotency_key)

    async def read_order(self, lookup: OrderLookup) -> AuthoritativeOrder | None:
        if lookup.venue.strip().lower() != "polymarket":
            raise ValueError("Polymarket adapter received another venue")
        if not lookup.venue_order_id:
            # This adapter never POSTs until its signed hash is journaled.
            return AuthoritativeOrder(
                venue="polymarket",
                market_id=lookup.market_id,
                idempotency_key=lookup.idempotency_key,
                venue_order_id=None,
                phase=LegPhase.REJECTED,
                cumulative_filled_size=0.0,
            )
        payload = await self._read_raw_order(lookup.venue_order_id)
        return (
            _authoritative(payload, idempotency_key=lookup.idempotency_key)
            if payload is not None
            else None
        )

    async def list_open_orders(self) -> tuple[AuthoritativeOrder, ...]:
        raw_orders = await self._bridge().get_open_orders()
        return tuple(_authoritative(raw, idempotency_key=None) for raw in raw_orders)

    async def list_positions(self) -> tuple[AuthoritativePosition, ...]:
        positions = await self._client.get_positions()
        result: list[AuthoritativePosition] = []
        for market_id, outcomes in sorted(positions.items()):
            yes = outcomes.get(TokenType.YES)
            no = outcomes.get(TokenType.NO)
            signed = (yes.size if yes else 0.0) - (no.size if no else 0.0)
            if signed:
                result.append(AuthoritativePosition(market_id, signed))
        return tuple(result)

    async def _read_raw_order(self, order_id: str) -> Mapping[str, Any] | None:
        try:
            payload = await self._bridge().get_order(order_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        if not payload:
            return None
        if not isinstance(payload, Mapping):
            raise ValueError("Polymarket get-order response must be an object")
        return payload
