"""
Polymarket CLOB SDK bridge
==========================

Wraps the synchronous ``py_clob_client_v2`` SDK for async callers and maps
exchange payloads into this project's models.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from polymarket_client.models import Order, OrderSide, OrderStatus, TokenType, Trade

logger = logging.getLogger(__name__)

FIXED_DECIMALS = 1_000_000


def parse_fixed_amount(value: Any) -> float:
    """Parse Polymarket fixed-math amounts (6 decimal places)."""
    if value is None or value == "":
        return 0.0
    return float(value) / FIXED_DECIMALS


def parse_price(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def clob_side_to_order_side(side: str) -> OrderSide:
    normalized = (side or "").lower()
    if normalized in ("buy", "b"):
        return OrderSide.BUY
    if normalized in ("sell", "s"):
        return OrderSide.SELL
    raise ValueError(f"Unknown order side: {side}")


def order_side_to_clob_side(side: OrderSide) -> str:
    return "BUY" if side == OrderSide.BUY else "SELL"


def outcome_to_token_type(outcome: str) -> TokenType:
    normalized = (outcome or "").strip().lower()
    if normalized in ("yes", "y"):
        return TokenType.YES
    if normalized in ("no", "n"):
        return TokenType.NO
    return TokenType.YES


def map_clob_order_status(status: str, filled_size: float, original_size: float) -> OrderStatus:
    normalized = (status or "").upper()
    if "CANCEL" in normalized:
        return OrderStatus.CANCELLED
    if "REJECT" in normalized or "INVALID" in normalized:
        return OrderStatus.REJECTED
    if "EXPIR" in normalized:
        return OrderStatus.EXPIRED
    if original_size > 0 and filled_size >= original_size:
        return OrderStatus.FILLED
    if filled_size > 0:
        return OrderStatus.PARTIALLY_FILLED
    if "LIVE" in normalized or "OPEN" in normalized:
        return OrderStatus.OPEN
    return OrderStatus.OPEN


def parse_open_order(
    data: dict[str, Any],
    *,
    market_id: str = "",
    token_type: Optional[TokenType] = None,
    strategy_tag: str = "",
) -> Order:
    """Map a Polymarket OpenOrder payload to our Order model."""
    order_id = str(data.get("id") or data.get("orderID") or data.get("order_id") or "")
    original_size = parse_fixed_amount(data.get("original_size"))
    filled_size = parse_fixed_amount(data.get("size_matched"))
    price = parse_price(data.get("price"))
    side = clob_side_to_order_side(str(data.get("side", "buy")))
    resolved_token = token_type or outcome_to_token_type(str(data.get("outcome", "yes")))
    resolved_market = market_id or str(data.get("market") or "")
    created_at = _parse_timestamp(data.get("created_at"))

    return Order(
        order_id=order_id,
        market_id=resolved_market,
        token_type=resolved_token,
        side=side,
        price=price,
        size=original_size,
        filled_size=filled_size,
        status=map_clob_order_status(str(data.get("status", "")), filled_size, original_size),
        strategy_tag=strategy_tag,
        created_at=created_at,
        updated_at=datetime.now(timezone.utc),
    )


def incremental_fill_trades(
    order: Order,
    previous_filled_size: float,
    *,
    fee_rate: float = 0.0,
) -> list[Trade]:
    """Build Trade records for newly matched size on an order."""
    delta = order.filled_size - previous_filled_size
    if delta <= 0:
        return []

    notional = delta * order.price
    fee = notional * fee_rate
    return [
        Trade(
            trade_id=f"fill_{uuid.uuid4().hex[:12]}",
            order_id=order.order_id,
            market_id=order.market_id,
            token_type=order.token_type,
            side=order.side,
            price=order.price,
            size=delta,
            fee=fee,
            timestamp=datetime.now(timezone.utc),
            is_simulated=False,
            simulation_label="live",
        )
    ]


def _parse_timestamp(value: Any) -> datetime:
    if value is None or value == "":
        return datetime.now(timezone.utc)
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


class ClobTradingBridge:
    """Async-friendly wrapper around the official Polymarket CLOB client."""

    def __init__(
        self,
        *,
        host: str,
        chain_id: int,
        private_key: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
    ):
        self.host = host
        self.chain_id = chain_id
        self.private_key = private_key
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._client: Any = None

    @property
    def is_ready(self) -> bool:
        return self._client is not None

    def connect(self) -> None:
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=self.api_key,
            api_secret=self.api_secret,
            api_passphrase=self.passphrase,
        )
        client = ClobClient(
            host=self.host,
            chain_id=self.chain_id,
            key=self.private_key,
            creds=creds,
        )
        client.set_api_creds(creds)
        self._client = client
        logger.info("CLOB trading bridge connected (L2 auth)")

    async def run(self, func, *args, **kwargs):
        if not self._client:
            raise RuntimeError("CLOB trading bridge is not connected")
        loop = asyncio.get_running_loop()
        bound = lambda: func(*args, **kwargs)
        return await loop.run_in_executor(None, bound)

    async def place_limit_order(
        self,
        *,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
    ) -> dict[str, Any]:
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType

        order_args = OrderArgsV2(
            token_id=token_id,
            price=price,
            size=size,
            side=order_side_to_clob_side(side),
        )
        return await self.run(
            self._client.create_and_post_order,
            order_args,
            None,
            OrderType.GTC,
            False,
            False,
        )

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self.run(self._client.get_order, order_id)

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return await self.run(self._client.get_open_orders, None, True)

    async def cancel_order(self, order_id: str) -> Any:
        from py_clob_client_v2.clob_types import OrderPayload

        return await self.run(self._client.cancel_order, OrderPayload(orderID=order_id))

    async def cancel_all_orders(self) -> Any:
        return await self.run(self._client.cancel_all)

    async def get_usdc_balance(self) -> Optional[float]:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        try:
            data = await self.run(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
        except Exception as exc:
            logger.warning("Failed to fetch USDC balance: %s", exc)
            return None

        if not isinstance(data, dict):
            return None

        balance = data.get("balance")
        if balance is None:
            return None
        return parse_fixed_amount(balance)
