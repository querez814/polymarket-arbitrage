"""Strict, spec-backed models for Kalshi Predictions order lifecycle calls."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

_BOOK_SIDES = frozenset({"bid", "ask"})
_TIME_IN_FORCE = frozenset(
    {"fill_or_kill", "good_till_canceled", "immediate_or_cancel"}
)
_SELF_TRADE_PREVENTION = frozenset({"taker_at_cross", "maker"})
_ORDER_STATUSES = frozenset({"resting", "canceled", "executed"})
_OUTCOME_SIDES = frozenset({"yes", "no"})


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_string(payload: Mapping[str, Any], field: str) -> Optional[str]:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string when present")
    return value


def _fixed_point(
    value: object,
    field: str,
    *,
    max_decimal_places: int,
    positive: bool = False,
    maximum: Optional[Decimal] = None,
) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} must be a plain fixed-point string")
    if value.startswith("+") or "e" in value.lower():
        raise ValueError(f"{field} must use plain fixed-point notation")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a valid fixed-point decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be positive")
    if not positive and parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{field} must be finite")
    decimal_places = max(0, -exponent)
    if decimal_places > max_decimal_places:
        raise ValueError(
            f"{field} supports at most {max_decimal_places} decimal places"
        )
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return parsed


def _request_count(value: object, field: str = "count") -> str:
    _fixed_point(value, field, max_decimal_places=2, positive=True)
    return str(value)


def _request_price(value: object, field: str = "price") -> str:
    _fixed_point(
        value,
        field,
        max_decimal_places=6,
        positive=True,
        maximum=Decimal("1"),
    )
    return str(value)


def _response_count(payload: Mapping[str, Any], field: str) -> Decimal:
    return _fixed_point(payload.get(field), field, max_decimal_places=2, positive=False)


def _response_dollars(payload: Mapping[str, Any], field: str) -> Decimal:
    return _fixed_point(payload.get(field), field, max_decimal_places=6, positive=False)


@dataclass(frozen=True)
class CreateOrderV2Request:
    """Safe local template for `POST /portfolio/events/orders`."""

    ticker: str
    client_order_id: str
    side: str
    count: str
    price: str
    time_in_force: str = "immediate_or_cancel"
    self_trade_prevention_type: str = "taker_at_cross"
    expiration_time: Optional[int] = None
    post_only: bool = False
    cancel_order_on_pause: bool = True
    reduce_only: bool = False
    subaccount: int = 0
    order_group_id: Optional[str] = None
    exchange_index: int = 0

    def to_payload(self) -> dict[str, Any]:
        ticker = self.ticker.strip()
        client_order_id = self.client_order_id.strip()
        if not ticker:
            raise ValueError("ticker must be a non-empty string")
        if not client_order_id:
            raise ValueError(
                "client_order_id is required locally for idempotent reconciliation"
            )
        if self.side not in _BOOK_SIDES:
            raise ValueError("side must be 'bid' or 'ask'")
        if self.time_in_force not in _TIME_IN_FORCE:
            raise ValueError("unsupported time_in_force")
        if self.self_trade_prevention_type not in _SELF_TRADE_PREVENTION:
            raise ValueError("unsupported self_trade_prevention_type")
        if self.expiration_time is not None:
            if self.time_in_force != "good_till_canceled":
                raise ValueError(
                    "expiration_time requires good_till_canceled time_in_force"
                )
            if not isinstance(self.expiration_time, int) or self.expiration_time <= 0:
                raise ValueError("expiration_time must be a positive Unix timestamp")
        if not isinstance(self.subaccount, int) or self.subaccount < 0:
            raise ValueError("subaccount must be a non-negative integer")
        if self.exchange_index not in {-1, 0}:
            raise ValueError("exchange_index must be -1 (auto) or 0")

        payload: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": self.side,
            "count": _request_count(self.count),
            "price": _request_price(self.price),
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": self.self_trade_prevention_type,
            "post_only": self.post_only,
            "cancel_order_on_pause": self.cancel_order_on_pause,
            "reduce_only": self.reduce_only,
            "subaccount": self.subaccount,
            "exchange_index": self.exchange_index,
        }
        if self.expiration_time is not None:
            payload["expiration_time"] = self.expiration_time
        if self.order_group_id:
            payload["order_group_id"] = self.order_group_id
        return payload


@dataclass(frozen=True)
class CreateOrderV2Result:
    order_id: str
    client_order_id: Optional[str]
    fill_count: Decimal
    remaining_count: Decimal
    average_fill_price: Optional[Decimal]
    average_fee_paid: Optional[Decimal]
    ts_ms: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CreateOrderV2Result":
        ts_ms = payload.get("ts_ms")
        if not isinstance(ts_ms, int) or ts_ms <= 0:
            raise ValueError("ts_ms must be a positive integer")
        average_fill_price = (
            _response_dollars(payload, "average_fill_price")
            if payload.get("average_fill_price") is not None
            else None
        )
        average_fee_paid = (
            _response_dollars(payload, "average_fee_paid")
            if payload.get("average_fee_paid") is not None
            else None
        )
        return cls(
            order_id=_required_string(payload, "order_id"),
            client_order_id=_optional_string(payload, "client_order_id"),
            fill_count=_response_count(payload, "fill_count"),
            remaining_count=_response_count(payload, "remaining_count"),
            average_fill_price=average_fill_price,
            average_fee_paid=average_fee_paid,
            ts_ms=ts_ms,
        )


@dataclass(frozen=True)
class CancelOrderV2Result:
    order_id: str
    client_order_id: Optional[str]
    reduced_by: Decimal
    ts_ms: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CancelOrderV2Result":
        ts_ms = payload.get("ts_ms")
        if not isinstance(ts_ms, int) or ts_ms <= 0:
            raise ValueError("ts_ms must be a positive integer")
        return cls(
            order_id=_required_string(payload, "order_id"),
            client_order_id=_optional_string(payload, "client_order_id"),
            reduced_by=_response_count(payload, "reduced_by"),
            ts_ms=ts_ms,
        )


@dataclass(frozen=True)
class KalshiOrder:
    """Canonical subset of the current OpenAPI `Order` response."""

    order_id: str
    client_order_id: str
    ticker: str
    outcome_side: str
    book_side: str
    status: str
    yes_price_dollars: Decimal
    no_price_dollars: Decimal
    fill_count: Decimal
    remaining_count: Decimal
    initial_count: Decimal

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "KalshiOrder":
        outcome_side = _required_string(payload, "outcome_side")
        book_side = _required_string(payload, "book_side")
        status = _required_string(payload, "status")
        if outcome_side not in _OUTCOME_SIDES:
            raise ValueError("outcome_side is not recognized")
        if book_side not in _BOOK_SIDES:
            raise ValueError("book_side is not recognized")
        if status not in _ORDER_STATUSES:
            raise ValueError("status is not recognized")
        return cls(
            order_id=_required_string(payload, "order_id"),
            client_order_id=_required_string(payload, "client_order_id"),
            ticker=_required_string(payload, "ticker"),
            outcome_side=outcome_side,
            book_side=book_side,
            status=status,
            yes_price_dollars=_response_dollars(payload, "yes_price_dollars"),
            no_price_dollars=_response_dollars(payload, "no_price_dollars"),
            fill_count=_response_count(payload, "fill_count_fp"),
            remaining_count=_response_count(payload, "remaining_count_fp"),
            initial_count=_response_count(payload, "initial_count_fp"),
        )


@dataclass(frozen=True)
class KalshiOrdersPage:
    orders: tuple[KalshiOrder, ...]
    cursor: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "KalshiOrdersPage":
        raw_orders = payload.get("orders")
        cursor = payload.get("cursor")
        if not isinstance(raw_orders, list):
            raise ValueError("orders must be an array")
        if not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        return cls(
            orders=tuple(KalshiOrder.from_payload(order) for order in raw_orders),
            cursor=cursor,
        )
