"""Parse Polymarket US API payloads into shared bot models."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from polymarket_client.models import (
    Market,
    Order,
    OrderBook,
    OrderBookSide,
    OrderSide,
    OrderStatus,
    PriceLevel,
    TokenOrderBook,
    TokenType,
    Trade,
)

YES_OUTCOMES = {"yes", "y", "true"}
NO_OUTCOMES = {"no", "n", "false"}


def parse_amount(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value) if value else 0.0
    if isinstance(value, dict):
        raw = value.get("value")
        return float(raw) if raw not in (None, "") else 0.0
    return 0.0


def map_us_order_state(state: str, filled: float, total: float) -> OrderStatus:
    normalized = (state or "").upper()
    if "CANCEL" in normalized:
        return OrderStatus.CANCELLED
    if "REJECT" in normalized:
        return OrderStatus.REJECTED
    if "EXPIR" in normalized:
        return OrderStatus.EXPIRED
    if total > 0 and filled >= total:
        return OrderStatus.FILLED
    if filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    if normalized in {"ORDER_STATE_NEW", "ORDER_STATE_PENDING_NEW", "ORDER_STATE_PENDING_REPLACE"}:
        return OrderStatus.OPEN
    return OrderStatus.OPEN


def _outcome_rank(outcome: str) -> int:
    normalized = (outcome or "").strip().lower()
    if normalized in YES_OUTCOMES:
        return 0
    if normalized in NO_OUTCOMES:
        return 1
    return 2


def _assign_yes_no(first: dict[str, Any], second: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    first_rank = _outcome_rank(str(first.get("outcome", "")))
    second_rank = _outcome_rank(str(second.get("outcome", "")))
    if first_rank == second_rank:
        return first, second
    if first_rank <= second_rank:
        return first, second
    return second, first


def group_us_markets(details: list[dict[str, Any]]) -> list[Market]:
    """Group US outcome markets into binary markets when possible."""
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in details:
        event_slug = str(item.get("eventSlug") or item.get("slug") or "")
        if not event_slug:
            continue
        by_event[event_slug].append(item)

    markets: list[Market] = []
    for event_slug, members in by_event.items():
        active = [
            m for m in members
            if m.get("active", True) and not m.get("closed", False)
        ]
        if len(active) >= 2:
            active.sort(key=lambda m: float(m.get("volume") or 0), reverse=True)
            yes_detail, no_detail = _assign_yes_no(active[0], active[1])
            markets.append(
                Market(
                    market_id=event_slug,
                    condition_id=str(yes_detail.get("id") or ""),
                    question=str(yes_detail.get("title") or event_slug),
                    description=str(yes_detail.get("description") or ""),
                    yes_token_id=str(yes_detail.get("slug") or ""),
                    no_token_id=str(no_detail.get("slug") or ""),
                    active=True,
                    closed=False,
                    volume_24h=float(yes_detail.get("volume") or 0) + float(no_detail.get("volume") or 0),
                    liquidity=float(yes_detail.get("liquidity") or 0) + float(no_detail.get("liquidity") or 0),
                    category=str(yes_detail.get("eventSlug") or ""),
                )
            )
            continue

        if len(active) == 1:
            detail = active[0]
            slug = str(detail.get("slug") or "")
            markets.append(
                Market(
                    market_id=slug,
                    condition_id=str(detail.get("id") or ""),
                    question=str(detail.get("title") or slug),
                    description=str(detail.get("description") or ""),
                    yes_token_id=slug,
                    no_token_id="",
                    active=True,
                    closed=False,
                    volume_24h=float(detail.get("volume") or 0),
                    liquidity=float(detail.get("liquidity") or 0),
                    category=str(detail.get("eventSlug") or ""),
                )
            )
    return markets


def parse_us_orderbook(market_id: str, yes_book: dict[str, Any], no_book: dict[str, Any]) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        yes=_parse_single_book(TokenType.YES, yes_book),
        no=_parse_single_book(TokenType.NO, no_book),
        timestamp=datetime.now(timezone.utc),
    )


def _parse_single_book(token_type: TokenType, payload: dict[str, Any]) -> TokenOrderBook:
    bids = [
        PriceLevel(price=parse_amount(level.get("px")), size=float(level.get("qty") or 0))
        for level in payload.get("bids", [])[:10]
    ]
    asks = [
        PriceLevel(price=parse_amount(level.get("px")), size=float(level.get("qty") or 0))
        for level in payload.get("offers", [])[:10]
    ]
    return TokenOrderBook(
        token_type=token_type,
        bids=OrderBookSide(levels=bids),
        asks=OrderBookSide(levels=asks),
    )


def parse_us_order(
    payload: dict[str, Any],
    *,
    market_id: str = "",
    token_type: Optional[TokenType] = None,
    strategy_tag: str = "",
) -> Order:
    order = payload.get("order", payload)
    slug = str(order.get("marketSlug") or market_id)
    quantity = float(order.get("quantity") or 0)
    filled = float(order.get("cumQuantity") or 0)
    side = OrderSide.BUY if "BUY" in str(order.get("side", "")).upper() else OrderSide.SELL
    resolved_token = token_type or TokenType.YES

    return Order(
        order_id=str(order.get("id") or ""),
        market_id=market_id or slug,
        token_type=resolved_token,
        side=side,
        price=parse_amount(order.get("price")),
        size=quantity,
        filled_size=filled,
        status=map_us_order_state(str(order.get("state", "")), filled, quantity),
        strategy_tag=strategy_tag,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def incremental_us_fill_trades(
    order: Order,
    previous_filled_size: float,
    *,
    fee_rate: float = 0.0,
) -> list[Trade]:
    delta = order.filled_size - previous_filled_size
    if delta <= 0:
        return []

    notional = delta * order.price
    return [
        Trade(
            trade_id=f"fill_us_{order.order_id}_{int(order.filled_size * 1000)}",
            order_id=order.order_id,
            market_id=order.market_id,
            token_type=order.token_type,
            side=order.side,
            price=order.price,
            size=delta,
            fee=notional * fee_rate,
            timestamp=datetime.now(timezone.utc),
            is_simulated=False,
            simulation_label="live_us",
        )
    ]
