"""
Polymarket US API Client
========================

Client for the CFTC-regulated Polymarket US exchange (api.polymarket.us).
Uses the official ``polymarket-us`` SDK with Ed25519 API key authentication.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from polymarket_client.api import BasePolymarketClient
from polymarket_client.models import (
    Market,
    Order,
    OrderBook,
    OrderBookSide,
    OrderSide,
    OrderStatus,
    Position,
    PriceLevel,
    TokenOrderBook,
    TokenType,
    Trade,
)
from polymarket_us_client.parsing import (
    group_us_markets,
    incremental_us_fill_trades,
    parse_amount,
    parse_us_order,
    parse_us_orderbook,
)

logger = logging.getLogger(__name__)


class PolymarketUSClient(BasePolymarketClient):
    """Async client for Polymarket US."""

    def __init__(
        self,
        *,
        key_id: str = "",
        secret_key: str = "",
        api_base_url: str = "https://api.polymarket.us",
        gateway_base_url: str = "https://gateway.polymarket.us",
        timeout: float = 30.0,
        dry_run: bool = True,
    ):
        self.key_id = key_id
        self.secret_key = secret_key
        self.api_base_url = api_base_url
        self.gateway_base_url = gateway_base_url
        self.timeout = timeout
        self.dry_run = dry_run

        self._sdk: Any = None
        self._markets_cache: dict[str, Market] = {}
        self._slug_index: dict[str, tuple[str, TokenType]] = {}
        self._order_slugs: dict[str, str] = {}

        self._simulated_orders: dict[str, Order] = {}
        self._simulated_trades: list[Trade] = []

    async def __aenter__(self) -> "PolymarketUSClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self.dry_run:
            logger.info("Polymarket US client connected (dry_run=True)")
            return

        if not self.key_id or not self.secret_key:
            raise RuntimeError("polymarket_us_key_id and polymarket_us_secret_key are required for live trading")

        from polymarket_us import AsyncPolymarketUS

        self._sdk = AsyncPolymarketUS(
            key_id=self.key_id,
            secret_key=self.secret_key,
            api_base_url=self.api_base_url,
            gateway_base_url=self.gateway_base_url,
            timeout=self.timeout,
        )
        logger.info("Polymarket US client connected (live)")

    async def disconnect(self) -> None:
        if self._sdk is not None:
            await self._sdk.close()
            self._sdk = None

    def _cache_market(self, market: Market) -> None:
        self._markets_cache[market.market_id] = market
        if market.yes_token_id:
            self._slug_index[market.yes_token_id] = (market.market_id, TokenType.YES)
        if market.no_token_id:
            self._slug_index[market.no_token_id] = (market.market_id, TokenType.NO)

    def resolve_token_id(self, market_id: str, token_type: TokenType) -> str:
        market = self._markets_cache.get(market_id)
        if not market:
            raise ValueError(f"Unknown market_id {market_id}; load markets before placing orders")
        slug = market.yes_token_id if token_type == TokenType.YES else market.no_token_id
        if not slug:
            raise ValueError(f"Market {market_id} is missing {token_type.value} slug")
        return slug

    async def get_usdc_balance(self) -> Optional[float]:
        if self.dry_run or not self._sdk:
            return None
        try:
            response = await self._sdk.account.balances()
            balances = response.get("balances", [])
            if not balances:
                return None
            primary = balances[0]
            buying_power = primary.get("buyingPower")
            if buying_power is not None:
                return float(buying_power)
            return float(primary.get("currentBalance", 0))
        except Exception as exc:
            logger.warning("Failed to fetch Polymarket US balance: %s", exc)
            return None

    async def refresh_order(
        self,
        local_order: Order,
        *,
        fee_rate: float = 0.015,
    ) -> tuple[Order, list[Trade]]:
        if self.dry_run or not self._sdk:
            return local_order, []

        payload = await self._sdk.orders.retrieve(local_order.order_id)
        remote = parse_us_order(
            payload,
            market_id=local_order.market_id,
            token_type=local_order.token_type,
            strategy_tag=local_order.strategy_tag,
        )
        trades = incremental_us_fill_trades(
            remote,
            local_order.filled_size,
            fee_rate=fee_rate,
        )
        return remote, trades

    async def list_markets(self, filters: Optional[dict] = None) -> list[Market]:
        filters = filters or {}
        max_markets = int(filters.get("max_markets", 5000) or 5000)

        if self.dry_run and not self._sdk:
            return self._generate_simulated_markets(min(max_markets, 20))

        if not self._sdk:
            raise RuntimeError("Polymarket US SDK is not connected")

        all_details: list[dict[str, Any]] = []
        offset = 0
        page_size = 100

        while len(all_details) < max_markets:
            response = await self._sdk.markets.list(
                {
                    "limit": page_size,
                    "offset": offset,
                    "active": True,
                    "closed": False,
                }
            )
            batch = response.get("markets", [])
            if not batch:
                break
            all_details.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        markets = group_us_markets(all_details)
        if max_markets:
            markets = markets[:max_markets]

        for market in markets:
            self._cache_market(market)

        logger.info("Loaded %s Polymarket US markets", len(markets))
        return markets

    async def get_market(self, market_id: str) -> Market:
        if market_id in self._markets_cache:
            return self._markets_cache[market_id]

        if self.dry_run and not self._sdk:
            market = self._generate_simulated_markets(1)[0]
            market.market_id = market_id
            self._cache_market(market)
            return market

        if not self._sdk:
            raise RuntimeError("Polymarket US SDK is not connected")

        try:
            response = await self._sdk.markets.retrieve_by_slug(market_id)
        except Exception:
            response = await self._sdk.markets.retrieve(int(market_id))

        detail = response.get("market", response)
        grouped = group_us_markets([detail])
        if grouped:
            market = grouped[0]
        else:
            slug = str(detail.get("slug") or market_id)
            market = Market(
                market_id=slug,
                condition_id=str(detail.get("id") or ""),
                question=str(detail.get("title") or slug),
                yes_token_id=slug,
                no_token_id="",
                active=bool(detail.get("active", True)),
                closed=bool(detail.get("closed", False)),
            )
        self._cache_market(market)
        return market

    async def get_orderbook(self, market_id: str) -> OrderBook:
        market = await self.get_market(market_id)

        if self.dry_run and not self._sdk:
            return self._generate_simulated_orderbook(market_id)

        if not market.yes_token_id:
            return OrderBook(market_id=market_id, timestamp=datetime.now(timezone.utc))

        yes_book = await self._sdk.markets.book(market.yes_token_id)
        no_book = (
            await self._sdk.markets.book(market.no_token_id)
            if market.no_token_id
            else {"bids": [], "offers": []}
        )
        return parse_us_orderbook(market_id, yes_book, no_book)

    async def stream_orderbook(
        self,
        market_ids: list[str],
        use_simulation: bool = False,
    ) -> AsyncIterator[tuple[str, OrderBook]]:
        if use_simulation or (self.dry_run and not self._sdk):
            while True:
                for market_id in market_ids:
                    yield market_id, self._generate_simulated_orderbook(market_id)
                await asyncio.sleep(1.0)
            return

        while True:
            for market_id in market_ids:
                try:
                    yield market_id, await self.get_orderbook(market_id)
                except Exception as exc:
                    logger.warning("Failed to poll US orderbook for %s: %s", market_id, exc)
            await asyncio.sleep(1.0)

    async def get_positions(self) -> dict[str, dict[TokenType, Position]]:
        if self.dry_run or not self._sdk:
            return {}

        try:
            response = await self._sdk.portfolio.positions()
        except Exception as exc:
            logger.warning("Failed to fetch Polymarket US positions: %s", exc)
            return {}

        positions: dict[str, dict[TokenType, Position]] = {}
        raw_positions = response.get("positions", {})
        if isinstance(raw_positions, list):
            iterable = ((str(item.get("marketSlug") or ""), item) for item in raw_positions)
        else:
            iterable = raw_positions.items()

        for slug, item in iterable:
            market_id, token_type = self._slug_index.get(slug, (slug, TokenType.YES))
            quantity = float(item.get("netPosition") or item.get("qtyAvailable") or 0)
            if quantity == 0:
                continue
            positions.setdefault(market_id, {})[token_type] = Position(
                market_id=market_id,
                token_type=token_type,
                size=quantity,
                avg_entry_price=float(item.get("avgPrice") or item.get("avgPx", {}).get("value", 0) if isinstance(item.get("avgPx"), dict) else item.get("avgPx") or 0),
            )
        return positions

    async def place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = "",
    ) -> Order:
        if self.dry_run:
            order_id = f"us_order_{uuid.uuid4().hex[:12]}"
            order = Order(
                order_id=order_id,
                market_id=market_id,
                token_type=token_type,
                side=side,
                price=price,
                size=size,
                status=OrderStatus.OPEN,
                strategy_tag=strategy_tag,
            )
            self._simulated_orders[order_id] = order
            logger.info("[DRY RUN US] Placing order: %s", order)
            return order

        if not self._sdk:
            raise RuntimeError("Polymarket US SDK is not connected")

        slug = self.resolve_token_id(market_id, token_type)
        intent = "ORDER_INTENT_BUY_LONG" if side == OrderSide.BUY else "ORDER_INTENT_SELL_LONG"
        quantity = max(1, int(round(size)))

        try:
            response = await self._sdk.orders.create(
                {
                    "marketSlug": slug,
                    "intent": intent,
                    "type": "ORDER_TYPE_LIMIT",
                    "price": {"value": f"{price:.4f}", "currency": "USD"},
                    "quantity": quantity,
                    "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
                }
            )
            order_id = str(response.get("id") or "")
            if not order_id and response.get("executions"):
                order_id = str(response["executions"][0].get("order", {}).get("id") or "")
            if not order_id:
                raise RuntimeError(f"Order placement returned no order id: {response}")

            payload = await self._sdk.orders.retrieve(order_id)
            order = parse_us_order(
                payload,
                market_id=market_id,
                token_type=token_type,
                strategy_tag=strategy_tag,
            )
            self._order_slugs[order.order_id] = slug
            logger.info("Polymarket US order placed: %s", order.order_id)
            return order
        except Exception as exc:
            logger.error("Polymarket US order rejected: %s", exc)
            raise RuntimeError(f"Order rejected: {exc}") from exc

    async def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            if order_id in self._simulated_orders:
                self._simulated_orders[order_id].status = OrderStatus.CANCELLED
            return

        if not self._sdk:
            raise RuntimeError("Polymarket US SDK is not connected")

        slug = self._order_slugs.get(order_id)
        if not slug:
            open_orders = await self.get_open_orders()
            for order in open_orders:
                if order.order_id == order_id:
                    slug = self.resolve_token_id(order.market_id, order.token_type)
                    break
        if not slug:
            raise ValueError(f"Cannot resolve market slug for order {order_id}")

        await self._sdk.orders.cancel(order_id, {"marketSlug": slug})

    async def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        if self.dry_run:
            return [
                order for order in self._simulated_orders.values()
                if order.is_open and (market_id is None or order.market_id == market_id)
            ]

        if not self._sdk:
            return []

        params: dict[str, Any] = {}
        if market_id:
            market = self._markets_cache.get(market_id)
            slugs = []
            if market:
                if market.yes_token_id:
                    slugs.append(market.yes_token_id)
                if market.no_token_id:
                    slugs.append(market.no_token_id)
            if slugs:
                params["slugs"] = slugs

        response = await self._sdk.orders.list(params or None)
        orders: list[Order] = []
        for item in response.get("orders", []):
            slug = str(item.get("marketSlug") or "")
            mapped_market_id, token_type = self._slug_index.get(slug, (slug, TokenType.YES))
            if market_id and mapped_market_id != market_id:
                continue
            order = parse_us_order(
                {"order": item},
                market_id=mapped_market_id,
                token_type=token_type,
            )
            self._order_slugs[order.order_id] = slug
            orders.append(order)
        return orders

    async def get_trades(self, market_id: Optional[str] = None, limit: int = 100) -> list[Trade]:
        if self.dry_run:
            simulated_trades = self._simulated_trades
            if market_id:
                simulated_trades = [
                    trade for trade in simulated_trades if trade.market_id == market_id
                ]
            return simulated_trades[-limit:]

        if not self._sdk:
            return []

        try:
            response = await self._sdk.portfolio.activities({"limit": limit})
        except Exception as exc:
            logger.warning("Failed to fetch Polymarket US activities: %s", exc)
            return []

        trades: list[Trade] = []
        for item in response.get("activities", []):
            if item.get("type") != "ACTIVITY_TYPE_TRADE":
                continue
            trade_payload = item.get("trade") or {}
            slug = str(trade_payload.get("marketSlug") or "")
            mapped_market_id, token_type = self._slug_index.get(slug, (slug, TokenType.YES))
            if market_id and mapped_market_id != market_id:
                continue
            trades.append(
                Trade(
                    trade_id=str(trade_payload.get("id") or uuid.uuid4().hex),
                    order_id="",
                    market_id=mapped_market_id,
                    token_type=token_type,
                    side=OrderSide.BUY,
                    price=parse_amount(trade_payload.get("price")),
                    size=float(trade_payload.get("qty") or 0),
                    fee=0.0,
                    timestamp=datetime.now(timezone.utc),
                    is_simulated=False,
                    simulation_label="live_us",
                )
            )
        return trades[-limit:]

    def simulate_fill(self, order_id: str, fill_size: Optional[float] = None) -> Optional[Trade]:
        order = self._simulated_orders.get(order_id)
        if not order or not order.is_open:
            return None

        delta = fill_size if fill_size is not None else order.remaining_size
        delta = min(delta, order.remaining_size)
        order.filled_size += delta
        if order.filled_size >= order.size:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

        trade = Trade(
            trade_id=f"sim_us_{uuid.uuid4().hex[:8]}",
            order_id=order.order_id,
            market_id=order.market_id,
            token_type=order.token_type,
            side=order.side,
            price=order.price,
            size=delta,
            fee=0.0,
            timestamp=datetime.now(timezone.utc),
            is_simulated=True,
            simulation_label="dry_run_us",
        )
        self._simulated_trades.append(trade)
        return trade

    def _generate_simulated_markets(self, count: int) -> list[Market]:
        markets: list[Market] = []
        for index in range(count):
            market_id = f"us-event-{index}"
            markets.append(
                Market(
                    market_id=market_id,
                    condition_id=str(index),
                    question=f"Simulated US market {index}",
                    yes_token_id=f"{market_id}-yes",
                    no_token_id=f"{market_id}-no",
                    active=True,
                    volume_24h=100_000,
                    liquidity=50_000,
                )
            )
        return markets

    def _generate_simulated_orderbook(self, market_id: str) -> OrderBook:
        yes_mid = 0.50 + random.uniform(-0.20, 0.20)
        no_mid = 1.0 - yes_mid + random.uniform(-0.03, 0.03)
        spread = 0.03

        def levels(mid: float, is_bid: bool) -> list[PriceLevel]:
            result = []
            for index in range(5):
                offset = (index + 1) * 0.01
                price = mid - spread / 2 - offset if is_bid else mid + spread / 2 + offset
                price = max(0.01, min(0.99, price))
                result.append(PriceLevel(price=round(price, 2), size=round(random.uniform(50, 500), 2)))
            return result

        return OrderBook(
            market_id=market_id,
            yes=TokenOrderBook(
                token_type=TokenType.YES,
                bids=OrderBookSide(levels=levels(yes_mid, True)),
                asks=OrderBookSide(levels=levels(yes_mid, False)),
            ),
            no=TokenOrderBook(
                token_type=TokenType.NO,
                bids=OrderBookSide(levels=levels(no_mid, True)),
                asks=OrderBookSide(levels=levels(no_mid, False)),
            ),
            timestamp=datetime.now(timezone.utc),
        )
