"""
Kalshi API Client
=================

Client for interacting with Kalshi prediction market exchange.
Supports public market data endpoints (no authentication required).

API Documentation: https://docs.kalshi.com/getting_started/quick_start_market_data
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, AsyncIterator
from urllib.parse import quote
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_client.auth import auth_headers, load_private_key, sign_path_for_url

from kalshi_client.models import (
    KalshiMarket,
    KalshiOrderBook,
    KalshiEvent,
    KalshiFeeSchedule,
    KalshiSeries,
)
from kalshi_client.orders import (
    CancelOrderV2Result,
    CreateOrderV2Request,
    CreateOrderV2Result,
    KalshiOrder,
    KalshiOrdersPage,
)
from polymarket_client.models import PriceLevel, OrderBook
from utils.http_resilience import EndpointResilience

logger = logging.getLogger(__name__)
_FIXED_POINT_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _fee_schedule(
    fee_type: object, multiplier: object, *, source: str
) -> KalshiFeeSchedule:
    if not isinstance(fee_type, str):
        raise ValueError("Kalshi fee type must be text")
    if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
        raise ValueError("Kalshi fee multiplier must be numeric")
    return KalshiFeeSchedule(fee_type.strip().lower(), float(multiplier), source)


class KalshiMutationAmbiguousError(RuntimeError):
    """A mutation may have reached Kalshi; reconcile before any retry."""


def _parse_orderbook_fp_levels(raw_levels: object, side: str) -> list[PriceLevel]:
    """Validate one current Kalshi fixed-point bid side and return best-first levels."""
    if not isinstance(raw_levels, list):
        raise ValueError(f"orderbook_fp.{side} must be an array")

    levels: list[PriceLevel] = []
    previous_price: Optional[Decimal] = None
    for index, raw_level in enumerate(raw_levels):
        if not isinstance(raw_level, list) or len(raw_level) != 2:
            raise ValueError(f"orderbook_fp.{side}[{index}] must be a two-item array")
        raw_price, raw_size = raw_level
        if not isinstance(raw_price, str) or not isinstance(raw_size, str):
            raise ValueError(
                f"orderbook_fp.{side}[{index}] values must be fixed-point strings"
            )
        if not _FIXED_POINT_PATTERN.fullmatch(
            raw_price
        ) or not _FIXED_POINT_PATTERN.fullmatch(raw_size):
            raise ValueError(
                f"orderbook_fp.{side}[{index}] values must use plain fixed-point notation"
            )

        try:
            price = Decimal(raw_price)
            size = Decimal(raw_size)
        except InvalidOperation as exc:
            raise ValueError(
                f"orderbook_fp.{side}[{index}] contains an invalid decimal"
            ) from exc

        if not price.is_finite() or not (Decimal("0") < price < Decimal("1")):
            raise ValueError(
                f"orderbook_fp.{side}[{index}] price must be finite and between 0 and 1"
            )
        if not size.is_finite() or size <= 0:
            raise ValueError(
                f"orderbook_fp.{side}[{index}] size must be finite and positive"
            )
        if previous_price is not None and price <= previous_price:
            raise ValueError(f"orderbook_fp.{side} prices must be strictly ascending")

        previous_price = price
        levels.append(PriceLevel(price=float(price), size=float(size)))

    levels.reverse()
    return levels


class KalshiClient:
    """
    Async client for Kalshi prediction market API.

    Uses Kalshi's current production Trade API root by default.
    """

    BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key_id: Optional[str] = None,
        private_key_path: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        dry_run: bool = True,
    ):
        """
        Initialize Kalshi client.

        Args:
            base_url: Kalshi trade API base URL (includes /trade-api/v2)
            api_key_id: Kalshi API key UUID (KALSHI-ACCESS-KEY)
            private_key_path: Path to RSA private key PEM from Kalshi key creation
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            dry_run: If True, don't place real orders
        """
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.api_key_id = api_key_id or ""
        self.private_key_path = private_key_path or ""
        self._private_key: Optional[rsa.RSAPrivateKey] = None
        if private_key_path:
            self._private_key = load_private_key(private_key_path)
        self.timeout = timeout
        self.max_retries = max_retries
        self.dry_run = dry_run
        self._client: Optional[httpx.AsyncClient] = None
        self._http_recycle_lock = asyncio.Lock()
        self._http_recycle_cooldown_seconds = 30.0
        self._last_http_recycle_at = float("-inf")
        self._http_pool_recycles = 0
        self._http_recycle_suppressed = 0
        self._last_http_recycle_reason = ""
        self._retired_http_clients: list[httpx.AsyncClient] = []
        self._retired_http_close_failures = 0
        self._http_close_timeout_seconds = 5.0
        self._markets_cache: dict[str, KalshiMarket] = {}
        self._resilience = EndpointResilience()
        self._event_market_cache: dict[
            tuple[str, int, int], tuple[float, list[KalshiMarket]]
        ] = {}
        self.market_list_ttl_seconds = 300.0

    @property
    def request_metrics(self) -> dict[str, float | int | bool]:
        """Return aggregate REST reliability metrics for operational telemetry."""
        return self._resilience.metrics

    @property
    def connection_metrics(self) -> dict[str, int | str]:
        """Return lifecycle evidence for automatic read-pool recovery."""
        return {
            "pool_recycles": self._http_pool_recycles,
            "recycle_suppressed": self._http_recycle_suppressed,
            "retired_pools": len(self._retired_http_clients),
            "retired_pool_close_failures": self._retired_http_close_failures,
            "last_recycle_reason": self._last_http_recycle_reason,
        }

    @property
    def is_authenticated(self) -> bool:
        return bool(self.api_key_id and self._private_key)

    async def __aenter__(self) -> "KalshiClient":
        """Async context manager entry."""
        self._client = self._build_http_client()
        return self

    def _build_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout, headers={"Accept": "application/json"}
        )

    async def _try_close_http_client(self, client: httpx.AsyncClient) -> bool:
        try:
            async with asyncio.timeout(self._http_close_timeout_seconds):
                await client.aclose()
        except TimeoutError:
            self._retired_http_close_failures += 1
            logger.error("Timed out closing exhausted Kalshi HTTP pool")
            return False
        except Exception:
            self._retired_http_close_failures += 1
            logger.exception("Failed closing exhausted Kalshi HTTP pool")
            return False
        return True

    async def _drain_retired_http_clients(self) -> None:
        for client in tuple(self._retired_http_clients):
            if await self._try_close_http_client(client):
                self._retired_http_clients.remove(client)

    async def recycle_http_client(self, *, reason: str) -> bool:
        """Atomically replace a starved paper-read pool with cooldown protection."""
        async with self._http_recycle_lock:
            now = time.monotonic()
            if (
                not self.dry_run
                or now - self._last_http_recycle_at
                < self._http_recycle_cooldown_seconds
            ):
                self._http_recycle_suppressed += 1
                return False
            await self._drain_retired_http_clients()
            if self._retired_http_clients:
                self._http_recycle_suppressed += 1
                logger.error("Kalshi HTTP recycle suppressed until retired pool closes")
                return False
            old_client = self._client
            self._client = self._build_http_client()
            self._last_http_recycle_at = now
            self._http_pool_recycles += 1
            self._last_http_recycle_reason = reason
            if old_client is not None:
                if not await self._try_close_http_client(old_client):
                    self._retired_http_clients.append(old_client)
            return True

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        async with self._http_recycle_lock:
            await self._drain_retired_http_clients()
            if self._client and await self._try_close_http_client(self._client):
                self._client = None
            await self._drain_retired_http_clients()

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """
        Make a GET request to the Kalshi API.

        Args:
            endpoint: API endpoint (without base URL)
            params: Query parameters

        Returns:
            JSON response as dictionary
        """
        if not self._client:
            raise RuntimeError(
                "Client not initialized. Use async with context manager."
            )

        url = f"{self.base_url}{endpoint}"

        for attempt in range(self.max_retries):
            try:
                self._resilience.before_request()
                response = await self._client.get(url, params=params)
                response.raise_for_status()
                self._resilience.record(succeeded=True)
                return response.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                self._resilience.record(succeeded=False)
                if (
                    status in EndpointResilience.RETRYABLE_STATUSES
                    and attempt < self.max_retries - 1
                ):
                    wait_time = self._resilience.retry_delay(
                        attempt, e.response.headers.get("Retry-After")
                    )
                    logger.warning(
                        "Kalshi HTTP %s; retrying in %.2fs", status, wait_time
                    )
                    await asyncio.sleep(wait_time)
                elif status == 404:
                    logger.debug(f"Not found: {endpoint}")
                    return {}
                else:
                    logger.error(f"HTTP error {status}: {e}")
                    raise
            except httpx.RequestError as e:
                self._resilience.record(succeeded=False)
                logger.warning(f"Request error (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self._resilience.retry_delay(attempt))
                else:
                    raise

        return {}

    def _auth_headers(self, method: str, endpoint: str) -> dict[str, str]:
        private_key = self._private_key
        if not self.api_key_id or private_key is None:
            raise RuntimeError(
                "Kalshi API key id and private key path are required for authenticated requests"
            )
        timestamp_ms = str(int(time.time() * 1000))
        sign_path = sign_path_for_url(self.base_url, endpoint)
        return auth_headers(
            private_key,
            self.api_key_id,
            timestamp_ms,
            method,
            sign_path,
        )

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        if not self._client:
            raise RuntimeError(
                "Client not initialized. Use async with context manager."
            )

        url = f"{self.base_url}{endpoint}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            headers.update(self._auth_headers(method, endpoint))

        for attempt in range(self.max_retries):
            try:
                self._resilience.before_request()
                response = await self._client.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
                response.raise_for_status()
                self._resilience.record(succeeded=True)
                if not response.content:
                    return {}
                return response.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                self._resilience.record(succeeded=False)
                if (
                    status in EndpointResilience.RETRYABLE_STATUSES
                    and attempt < self.max_retries - 1
                ):
                    wait_time = self._resilience.retry_delay(
                        attempt, e.response.headers.get("Retry-After")
                    )
                    logger.warning(
                        "Kalshi HTTP %s; retrying in %.2fs", status, wait_time
                    )
                    await asyncio.sleep(wait_time)
                elif status == 404:
                    logger.debug(f"Not found: {endpoint}")
                    return {}
                else:
                    logger.error(f"HTTP error {status}: {e}")
                    raise
            except httpx.RequestError as e:
                self._resilience.record(succeeded=False)
                logger.warning(f"Request error (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self._resilience.retry_delay(attempt))
                else:
                    raise

        return {}

    async def _mutating_request_once(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        reconciliation_id: str,
    ) -> dict[str, Any]:
        """Send one authenticated mutation without ambiguous automatic retries."""
        if not self._client:
            raise RuntimeError(
                "Client not initialized. Use async with context manager."
            )

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Accept": "application/json",
            **self._auth_headers(method, endpoint),
        }
        try:
            response = await self._client.request(
                method.upper(),
                url,
                params=params,
                json=json_body,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise KalshiMutationAmbiguousError(
                "Kalshi mutation outcome is ambiguous; reconcile using "
                f"{reconciliation_id} before retrying"
            ) from exc

        if not response.content:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Kalshi mutation response must be a JSON object")
        return payload

    # =========================================================================
    # EXCHANGE / PORTFOLIO (auth optional for status; balance needs auth)
    # =========================================================================

    async def get_exchange_status(self) -> dict[str, Any]:
        """GET /exchange/status — public, no auth required."""
        return await self._get("/exchange/status")

    async def get_balance(self) -> Optional[dict[str, Any]]:
        """GET /portfolio/balance — requires API key + RSA private key."""
        data = await self._request("GET", "/portfolio/balance", authenticated=True)
        return data or None

    async def get_balance_dollars(self) -> Optional[float]:
        """Available buying power in USD."""
        data = await self.get_balance()
        if not data:
            return None
        if data.get("balance_dollars") is not None:
            return float(data["balance_dollars"])
        if data.get("balance") is not None:
            return float(data["balance"]) / 100.0
        return None

    async def get_positions(self, limit: int = 100) -> list[dict[str, Any]]:
        """GET /portfolio/positions — requires authentication."""
        data = await self._request(
            "GET",
            "/portfolio/positions",
            params={"limit": limit},
            authenticated=True,
        )
        return data.get("market_positions", []) if data else []

    async def get_all_positions(
        self, *, limit: int = 1000, max_pages: int = 100
    ) -> list[dict[str, Any]]:
        """Read every current market position with bounded cursor pagination."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        positions: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        seen: set[str] = set()
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = await self._request(
                "GET", "/portfolio/positions", params=params, authenticated=True
            )
            raw = data.get("market_positions") if data else []
            if not isinstance(raw, list):
                raise ValueError("Kalshi positions response must contain an array")
            positions.extend(raw)
            next_cursor = data.get("cursor", "") if data else ""
            if not isinstance(next_cursor, str):
                raise ValueError("Kalshi positions cursor must be a string")
            if not next_cursor:
                return positions
            if next_cursor in seen:
                raise ValueError("Kalshi positions pagination cursor repeated")
            seen.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError("Kalshi positions pagination exceeded max_pages")

    # =========================================================================
    # ORDERS (current V2 mutations + authoritative REST reconciliation)
    # =========================================================================

    async def create_order_v2(
        self, request: CreateOrderV2Request
    ) -> CreateOrderV2Result:
        """Submit one V2 order exactly once; disabled in dry-run mode."""
        if self.dry_run:
            raise RuntimeError("Kalshi order submission is disabled in dry-run mode")
        payload = request.to_payload()
        data = await self._mutating_request_once(
            "POST",
            "/portfolio/events/orders",
            json_body=payload,
            reconciliation_id=f"client_order_id={request.client_order_id}",
        )
        return CreateOrderV2Result.from_payload(data)

    async def cancel_order_v2(
        self,
        order_id: str,
        *,
        subaccount: int = 0,
        exchange_index: int = 0,
        market_ticker: Optional[str] = None,
    ) -> CancelOrderV2Result:
        """Cancel one V2 order exactly once; disabled in dry-run mode."""
        if self.dry_run:
            raise RuntimeError("Kalshi order cancellation is disabled in dry-run mode")
        if not order_id.strip():
            raise ValueError("order_id must be a non-empty string")
        if subaccount < 0:
            raise ValueError("subaccount must be non-negative")
        if exchange_index not in {-1, 0}:
            raise ValueError("exchange_index must be -1 (auto) or 0")
        if exchange_index == -1 and not market_ticker:
            raise ValueError("market_ticker is required when exchange_index is -1")
        params: dict[str, Any] = {
            "subaccount": subaccount,
            "exchange_index": exchange_index,
        }
        if market_ticker:
            params["market_ticker"] = market_ticker
        data = await self._mutating_request_once(
            "DELETE",
            f"/portfolio/events/orders/{quote(order_id, safe='')}",
            params=params,
            reconciliation_id=f"order_id={order_id}",
        )
        return CancelOrderV2Result.from_payload(data)

    async def get_order(self, order_id: str) -> Optional[KalshiOrder]:
        """Read one authoritative order state for recovery/reconciliation."""
        if not order_id.strip():
            raise ValueError("order_id must be a non-empty string")
        data = await self._request(
            "GET",
            f"/portfolio/orders/{quote(order_id, safe='')}",
            authenticated=True,
        )
        if not data:
            return None
        raw_order = data.get("order")
        if not isinstance(raw_order, Mapping):
            raise ValueError("Kalshi get-order response is missing order")
        return KalshiOrder.from_payload(raw_order)

    async def get_orders(
        self,
        *,
        ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        status: Optional[str] = None,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        subaccount: Optional[int] = None,
    ) -> KalshiOrdersPage:
        """Read a typed order page for startup and ambiguous-outcome recovery."""
        if status is not None and status not in {"resting", "canceled", "executed"}:
            raise ValueError("status must be resting, canceled, or executed")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if min_ts is not None and max_ts is not None and min_ts > max_ts:
            raise ValueError("min_ts cannot be after max_ts")
        if subaccount is not None and subaccount < 0:
            raise ValueError("subaccount must be non-negative")

        params: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("ticker", ticker),
            ("event_ticker", event_ticker),
            ("status", status),
            ("min_ts", min_ts),
            ("max_ts", max_ts),
            ("cursor", cursor),
            ("subaccount", subaccount),
        ):
            if value is not None:
                params[key] = value
        data = await self._request(
            "GET", "/portfolio/orders", params=params, authenticated=True
        )
        return KalshiOrdersPage.from_payload(data)

    # =========================================================================
    # SERIES ENDPOINTS
    # =========================================================================

    async def get_series(self, series_ticker: str) -> Optional[KalshiSeries]:
        """
        Get information about a series.

        Args:
            series_ticker: Series ticker (e.g., "KXHIGHNY")

        Returns:
            KalshiSeries object or None if not found
        """
        data = await self._get(f"/series/{series_ticker}")
        if not data or "series" not in data:
            return None

        s = data["series"]
        return KalshiSeries(
            ticker=s.get("ticker", series_ticker),
            title=s.get("title", ""),
            frequency=s.get("frequency", ""),
            category=s.get("category", ""),
        )

    # =========================================================================
    # EVENTS ENDPOINTS
    # =========================================================================

    async def list_event_markets(
        self,
        *,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> tuple[list[KalshiMarket], Optional[str]]:
        """Return ordinary event markets enriched with parent event context.

        Kalshi's ordinary events endpoint excludes multivariate combo events.
        Nested markets preserve the event title/category needed for safe
        cross-venue candidate matching.
        """
        if not 1 <= limit <= 200:
            raise ValueError("event page limit must be between 1 and 200")
        params: dict[str, Any] = {
            "status": status,
            "limit": limit,
            "with_nested_markets": True,
        }
        if cursor:
            params["cursor"] = cursor
        data = await self._get("/events", params=params)
        raw_events = data.get("events") if isinstance(data, Mapping) else None
        if not isinstance(raw_events, list):
            return [], None

        markets: list[KalshiMarket] = []
        for event in raw_events:
            if not isinstance(event, Mapping):
                continue
            event_title = event.get("title")
            event_category = event.get("category")
            event_series_ticker = event.get("series_ticker")
            raw_markets = event.get("markets")
            if not isinstance(raw_markets, list):
                continue
            for raw_market in raw_markets:
                if not isinstance(raw_market, dict):
                    continue
                market = self._parse_market(
                    raw_market,
                    event_title=(event_title if isinstance(event_title, str) else ""),
                    event_category=(
                        event_category if isinstance(event_category, str) else ""
                    ),
                    event_series_ticker=(
                        event_series_ticker
                        if isinstance(event_series_ticker, str)
                        else ""
                    ),
                )
                if market and market.ticker:
                    markets.append(market)
                    self._markets_cache[market.ticker] = market

        next_cursor = data.get("cursor")
        return markets, (
            next_cursor if isinstance(next_cursor, str) and next_cursor else None
        )

    async def list_all_event_markets(
        self,
        *,
        status: str = "open",
        max_events: int = 2_000,
        max_markets: int = 10_000,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> list[KalshiMarket]:
        """Page through ordinary events and return their enriched markets."""
        if max_events <= 0 or max_markets <= 0:
            raise ValueError("event and market limits must be positive")
        cache_key = (status, max_events, max_markets)
        cached = self._event_market_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self.market_list_ttl_seconds:
            if on_progress:
                on_progress(len(cached[1]))
            return list(cached[1])
        all_markets: list[KalshiMarket] = []
        seen_tickers: set[str] = set()
        cursor: Optional[str] = None
        events_loaded = 0
        while events_loaded < max_events and len(all_markets) < max_markets:
            page_limit = min(200, max_events - events_loaded)
            markets, next_cursor = await self.list_event_markets(
                status=status,
                limit=page_limit,
                cursor=cursor,
            )
            events_loaded += page_limit
            for market in markets:
                if market.ticker in seen_tickers:
                    continue
                seen_tickers.add(market.ticker)
                all_markets.append(market)
                if len(all_markets) >= max_markets:
                    break
            if on_progress:
                on_progress(len(all_markets))
            if not next_cursor:
                break
            cursor = next_cursor
            await asyncio.sleep(0.2)
        self._event_market_cache[cache_key] = (time.monotonic(), list(all_markets))
        return all_markets

    async def get_event(self, event_ticker: str) -> Optional[KalshiEvent]:
        """
        Get information about an event.

        Args:
            event_ticker: Event ticker (e.g., "KXHIGHNY-25DEC08")

        Returns:
            KalshiEvent object or None if not found
        """
        data = await self._get(f"/events/{event_ticker}")
        if not data or "event" not in data:
            return None

        e = data["event"]
        return KalshiEvent(
            event_ticker=e.get("ticker", event_ticker),
            series_ticker=e.get("series_ticker", ""),
            title=e.get("title", ""),
            category=e.get("category", ""),
        )

    # =========================================================================
    # MARKETS ENDPOINTS
    # =========================================================================

    async def list_markets(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        limit: int = 1000,
        cursor: Optional[str] = None,
        mve_filter: str = "exclude",
    ) -> tuple[list[KalshiMarket], Optional[str]]:
        """
        List markets with optional filters.

        Args:
            status: Market status filter (open, closed, settled)
            series_ticker: Filter by series
            event_ticker: Filter by event
            limit: Maximum markets to return (max 1000)
            cursor: Pagination cursor
            mve_filter: Kalshi multivariate-event policy. Cross-venue matching
                defaults to ordinary single-event markets only.

        Returns:
            Tuple of (list of markets, next cursor or None)
        """
        if mve_filter not in {"exclude", "only"}:
            raise ValueError("mve_filter must be 'exclude' or 'only'")
        params = {
            "status": status,
            "limit": min(limit, 1000),
            "mve_filter": mve_filter,
        }
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor

        data = await self._get("/markets", params=params)
        if not data or "markets" not in data:
            return [], None

        markets = []
        for m in data["markets"]:
            market = self._parse_market(m)
            if market:
                markets.append(market)
                self._markets_cache[market.ticker] = market

        next_cursor = data.get("cursor")
        return markets, next_cursor

    async def list_all_markets(
        self,
        status: str = "open",
        max_markets: int = 10000,
        mve_filter: str = "exclude",
        on_progress: Optional[
            Callable[[int], None]
        ] = None,  # Callback for progress updates
    ) -> list[KalshiMarket]:
        """
        Fetch all markets with pagination.

        Args:
            status: Market status filter
            max_markets: Maximum total markets to fetch
            mve_filter: Kalshi multivariate-event policy.
            on_progress: Optional callback(loaded_count) for progress updates

        Returns:
            List of all markets
        """
        all_markets: list[KalshiMarket] = []
        cursor: Optional[str] = None

        while len(all_markets) < max_markets:
            markets, next_cursor = await self.list_markets(
                status=status,
                limit=1000,
                cursor=cursor,
                mve_filter=mve_filter,
            )

            if not markets:
                break

            all_markets.extend(markets)
            logger.info(f"Kalshi: {len(all_markets)} markets loaded...")

            # Report progress
            if on_progress:
                try:
                    on_progress(len(all_markets))
                except Exception as exc:
                    logger.debug("Kalshi progress callback failed: %s", exc)

            if not next_cursor:
                break
            cursor = next_cursor

            # Small delay to avoid rate limiting
            await asyncio.sleep(0.2)

        logger.info(f"Kalshi: {len(all_markets)} total markets loaded ✓")
        return all_markets[:max_markets]

    async def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """
        Get a specific market by ticker.

        Args:
            ticker: Market ticker

        Returns:
            KalshiMarket object or None if not found
        """
        # Check cache first
        if ticker in self._markets_cache:
            return self._markets_cache[ticker]

        data = await self._get(f"/markets/{ticker}")
        if not data or "market" not in data:
            return None

        market = self._parse_market(data["market"])
        if market:
            self._markets_cache[ticker] = market
        return market

    async def get_fee_schedule(self, ticker: str) -> KalshiFeeSchedule:
        """Read the current effective public fee metadata for one market.

        A complete event override takes precedence over its parent series. A
        partial override is rejected because guessing the missing half could
        understate costs.
        """
        if not ticker.strip():
            raise ValueError("ticker must be non-empty")
        market_payload = await self._get(f"/markets/{quote(ticker, safe='')}")
        market = (
            market_payload.get("market")
            if isinstance(market_payload, Mapping)
            else None
        )
        if not isinstance(market, Mapping):
            raise ValueError("Kalshi market fee metadata is unavailable")
        event_ticker = market.get("event_ticker")
        if not isinstance(event_ticker, str) or not event_ticker:
            raise ValueError("Kalshi fee metadata is missing event ticker")

        event_payload = await self._get(f"/events/{quote(event_ticker, safe='')}")
        event = (
            event_payload.get("event") if isinstance(event_payload, Mapping) else None
        )
        if not isinstance(event, Mapping):
            raise ValueError("Kalshi event fee metadata is unavailable")
        override_type = event.get("fee_type_override")
        override_multiplier = event.get("fee_multiplier_override")
        if (override_type is None) != (override_multiplier is None):
            raise ValueError("Kalshi event fee override is incomplete")
        if override_type is not None:
            return _fee_schedule(override_type, override_multiplier, source="event")

        series_ticker = event.get("series_ticker") or market.get("series_ticker")
        if not isinstance(series_ticker, str) or not series_ticker:
            raise ValueError("Kalshi fee metadata is missing series ticker")
        series_payload = await self._get(f"/series/{quote(series_ticker, safe='')}")
        series = (
            series_payload.get("series")
            if isinstance(series_payload, Mapping)
            else None
        )
        if not isinstance(series, Mapping):
            raise ValueError("Kalshi series fee metadata is unavailable")
        return _fee_schedule(
            series.get("fee_type"), series.get("fee_multiplier"), source="series"
        )

    async def list_historical_markets(
        self,
        max_markets: int = 1000,
        cursor: Optional[str] = None,
    ) -> tuple[list[KalshiMarket], Optional[str]]:
        """
        List markets archived to Kalshi historical data.
        """
        params: dict[str, Any] = {"limit": min(max_markets, 1000)}
        if cursor:
            params["cursor"] = cursor

        data = await self._get("/historical/markets", params=params)
        if not data or "markets" not in data:
            return [], None

        markets = []
        for item in data["markets"][:max_markets]:
            market = self._parse_market(item)
            if market:
                markets.append(market)
                self._markets_cache[market.ticker] = market

        return markets, data.get("cursor")

    def _parse_market(
        self,
        data: dict,
        *,
        event_title: str = "",
        event_category: str = "",
        event_series_ticker: str = "",
    ) -> Optional[KalshiMarket]:
        """Parse market data from API response."""
        try:
            # Prices come in cents, convert to dollars
            yes_price = (
                data.get("yes_price", 0) / 100.0 if data.get("yes_price") else 0.0
            )
            no_price = data.get("no_price", 0) / 100.0 if data.get("no_price") else 0.0

            # If no_price not given, derive from yes_price
            if no_price == 0 and yes_price > 0:
                no_price = 1.0 - yes_price

            # Parse close time
            close_time = None
            if data.get("close_time"):
                try:
                    close_time = datetime.fromisoformat(
                        data["close_time"].replace("Z", "+00:00")
                    )
                except (TypeError, ValueError) as exc:
                    logger.debug(
                        "Failed to parse Kalshi close_time %r: %s",
                        data.get("close_time"),
                        exc,
                    )
            expiration_time = None
            if data.get("expiration_time"):
                try:
                    expiration_time = datetime.fromisoformat(
                        data["expiration_time"].replace("Z", "+00:00")
                    )
                except (TypeError, ValueError) as exc:
                    logger.debug(
                        "Failed to parse Kalshi expiration_time %r: %s",
                        data.get("expiration_time"),
                        exc,
                    )

            volume = data.get("volume", data.get("volume_fp", 0))
            open_interest = data.get("open_interest", data.get("open_interest_fp", 0))

            return KalshiMarket(
                ticker=data.get("ticker", ""),
                event_ticker=data.get("event_ticker", ""),
                series_ticker=data.get("series_ticker", "") or event_series_ticker,
                title=data.get("title", ""),
                subtitle=data.get("subtitle", ""),
                event_title=event_title,
                yes_price=yes_price,
                no_price=no_price,
                status=data.get("status", ""),
                result=data.get("result"),
                volume=int(float(volume or 0)),
                open_interest=int(float(open_interest or 0)),
                close_time=close_time,
                expiration_time=expiration_time,
                category=data.get("category", "") or event_category,
                rules_primary=str(data.get("rules_primary") or ""),
                rules_secondary=str(data.get("rules_secondary") or ""),
                settlement_source=str(
                    data.get("settlement_source") or data.get("result_source") or ""
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to parse Kalshi market: {e}")
            return None

    # =========================================================================
    # ORDERBOOK ENDPOINTS
    # =========================================================================

    async def get_orderbook(self, ticker: str) -> Optional[KalshiOrderBook]:
        """
        Get order book for a market.

        Args:
            ticker: Market ticker

        Returns:
            KalshiOrderBook object or None if not found
        """
        data = await self._get(f"/markets/{ticker}/orderbook")
        if not data:
            return None

        # Only the current fixed-point schema is admissible. Silently mixing or
        # falling back to deprecated cent levels can manufacture incomplete or
        # duplicated executable depth.
        ob_fp = data.get("orderbook_fp")
        if not isinstance(ob_fp, Mapping):
            logger.warning(
                "Invalid Kalshi orderbook for %s: missing orderbook_fp", ticker
            )
            return None
        if "yes_dollars" not in ob_fp or "no_dollars" not in ob_fp:
            logger.warning(
                "Invalid Kalshi orderbook for %s: incomplete orderbook_fp sides",
                ticker,
            )
            return None

        try:
            yes_bids = _parse_orderbook_fp_levels(ob_fp["yes_dollars"], "yes_dollars")
            no_bids = _parse_orderbook_fp_levels(ob_fp["no_dollars"], "no_dollars")
        except ValueError as exc:
            logger.warning("Invalid Kalshi orderbook for %s: %s", ticker, exc)
            return None

        return KalshiOrderBook(
            ticker=ticker,
            yes_bids=yes_bids,
            no_bids=no_bids,
            timestamp=datetime.now(timezone.utc),
        )

    async def get_orderbook_unified(self, ticker: str) -> Optional[OrderBook]:
        """
        Get order book in unified format (compatible with Polymarket).

        Args:
            ticker: Market ticker

        Returns:
            OrderBook object or None if not found
        """
        kalshi_ob = await self.get_orderbook(ticker)
        if not kalshi_ob:
            return None
        return kalshi_ob.to_unified_orderbook()

    async def get_market_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
        include_latest_before_start: bool = False,
    ) -> list[dict]:
        """
        Fetch candlesticks for an active Kalshi market.

        Args:
            series_ticker: Series ticker for the market.
            ticker: Market ticker.
            start_ts: Unix start timestamp in seconds.
            end_ts: Unix end timestamp in seconds.
            period_interval: Candle length in minutes (1, 60, or 1440).
            include_latest_before_start: Include Kalshi's synthetic continuity candle.
        """
        params = {
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "period_interval": int(period_interval),
            "include_latest_before_start": str(include_latest_before_start).lower(),
        }
        data = await self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params=params,
        )
        return data.get("candlesticks", []) if isinstance(data, dict) else []

    async def get_historical_market_candlesticks(
        self,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> list[dict]:
        """
        Fetch candlesticks for a Kalshi market archived to historical data.
        """
        params = {
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "period_interval": int(period_interval),
        }
        data = await self._get(
            f"/historical/markets/{ticker}/candlesticks",
            params=params,
        )
        return data.get("candlesticks", []) if isinstance(data, dict) else []

    # =========================================================================
    # STREAMING (Polling-based for public API)
    # =========================================================================

    async def stream_orderbooks(
        self,
        tickers: list[str],
        batch_size: int = 100,
        rotation_delay: float = 2.0,
    ) -> AsyncIterator[tuple[str, OrderBook]]:
        """
        Stream order books for multiple markets using polling.

        Args:
            tickers: List of market tickers to stream
            batch_size: Number of markets to fetch per batch
            rotation_delay: Delay between batches in seconds

        Yields:
            Tuple of (ticker, OrderBook) for each update
        """
        logger.info(f"Starting Kalshi orderbook stream for {len(tickers)} markets")

        while True:
            for i in range(0, len(tickers), batch_size):
                batch = tickers[i : i + batch_size]
                logger.debug(
                    f"Fetching Kalshi orderbooks {i+1}-{min(i+batch_size, len(tickers))} of {len(tickers)}"
                )

                # Fetch orderbooks in parallel
                tasks = [self.get_orderbook_unified(ticker) for ticker in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for ticker, result in zip(batch, results):
                    if isinstance(result, BaseException):
                        logger.debug(
                            f"Failed to get Kalshi orderbook for {ticker}: {result}"
                        )
                        continue
                    if result:
                        yield (ticker, result)

                await asyncio.sleep(rotation_delay)

    # =========================================================================
    # CATEGORY/SEARCH HELPERS
    # =========================================================================

    async def get_markets_by_category(self, category: str) -> list[KalshiMarket]:
        """
        Get all open markets in a category.

        Common categories: elections, economics, crypto, tech, entertainment
        """
        # Kalshi API doesn't have a direct category filter, so we fetch all
        # and filter client-side
        all_markets = await self.list_all_markets(status="open")
        return [m for m in all_markets if m.category.lower() == category.lower()]

    async def search_markets(self, query: str) -> list[KalshiMarket]:
        """
        Search markets by title.

        Args:
            query: Search query string

        Returns:
            List of matching markets
        """
        all_markets = await self.list_all_markets(status="open")
        query_lower = query.lower()
        return [
            m
            for m in all_markets
            if query_lower in m.title.lower() or query_lower in m.subtitle.lower()
        ]
