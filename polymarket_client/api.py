"""
Polymarket API Client
======================

Abstracted client for Polymarket REST and WebSocket APIs.
Designed to be easily pluggable with real API implementations.
"""

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Optional
from urllib.parse import quote

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from polymarket_client.clob_bridge import (
    ClobTradingBridge,
    incremental_fill_trades,
    parse_open_order,
)
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
from utils.http_resilience import EndpointResilience


logger = logging.getLogger(__name__)


def _parse_api_datetime(value: Any) -> Optional[datetime]:
    """Parse Gamma's ISO timestamps into timezone-aware UTC datetimes."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class BasePolymarketClient(ABC):
    """Abstract base class for Polymarket client implementations."""
    
    @abstractmethod
    async def list_markets(self, filters: Optional[dict] = None) -> list[Market]:
        """Fetch list of available markets."""
        pass
    
    @abstractmethod
    async def get_market(self, market_id: str) -> Market:
        """Get details for a specific market."""
        pass
    
    @abstractmethod
    async def get_orderbook(self, market_id: str) -> OrderBook:
        """Fetch current order book for a market."""
        pass
    
    @abstractmethod
    def stream_orderbook(self, market_ids: list[str]) -> AsyncIterator[tuple[str, OrderBook]]:
        """Stream order book updates for multiple markets."""
        pass
    
    @abstractmethod
    async def get_positions(self) -> dict[str, dict[TokenType, Position]]:
        """Get all current positions."""
        pass
    
    @abstractmethod
    async def place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = ""
    ) -> Order:
        """Place a limit order."""
        pass
    
    @abstractmethod
    async def cancel_order(self, order_id: str) -> None:
        """Cancel an open order."""
        pass
    
    @abstractmethod
    async def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        """Get all open orders, optionally filtered by market."""
        pass
    
    @abstractmethod
    async def get_trades(self, market_id: Optional[str] = None, limit: int = 100) -> list[Trade]:
        """Get recent trades."""
        pass

    async def get_usdc_balance(self) -> Optional[float]:
        """Fetch available trading balance (live mode only)."""
        return None

    async def refresh_order(
        self,
        local_order: Order,
        *,
        fee_rate: float = 0.015,
    ) -> tuple[Order, list[Trade]]:
        """Fetch exchange order state and return incremental fill trades."""
        return local_order, []


class PolymarketClient(BasePolymarketClient):
    """
    Polymarket API client implementation.
    
    This implementation provides the structure for real API integration.
    Currently uses placeholder implementations that can be replaced with
    actual Polymarket CLOB API calls.
    """
    
    def __init__(
        self,
        rest_url: str = "https://clob.polymarket.com",
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        gamma_url: str = "https://gamma-api.polymarket.com",
        data_url: str = "https://data-api.polymarket.com",
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        private_key: Optional[str] = None,
        chain_id: int = 137,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        dry_run: bool = True,
    ):
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self.gamma_url = gamma_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.private_key = private_key
        self.chain_id = chain_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.dry_run = dry_run
        
        # HTTP client
        self._http_client: Optional[httpx.AsyncClient] = None
        
        # WebSocket connection
        self._ws_connection: Any = None
        self._ws_subscriptions: set[str] = set()
        
        # Live CLOB trading
        self._clob_bridge: Optional[ClobTradingBridge] = None
        
        # Simulated state for dry run
        self._simulated_orders: dict[str, Order] = {}
        self._simulated_positions: dict[str, dict[TokenType, Position]] = {}
        self._simulated_trades: list[Trade] = []
        
        # Cache for market data (avoids re-fetching)
        self._markets_cache: dict[str, Market] = {}
        self._token_index: dict[str, tuple[str, TokenType]] = {}
        self._unavailable_token_ids: set[str] = set()
        self._resilience: dict[str, EndpointResilience] = {}
        self._market_list_cache: dict[str, tuple[float, list[Market]]] = {}
        self.market_list_ttl_seconds = 300.0

    @property
    def request_metrics(self) -> dict[str, dict[str, float | int | bool]]:
        """Return read-only REST reliability metrics grouped by API base URL."""
        return {
            endpoint: resilience.metrics
            for endpoint, resilience in self._resilience.items()
        }
        
    async def __aenter__(self) -> "PolymarketClient":
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()
    
    async def connect(self) -> None:
        """Initialize HTTP client and optional live trading bridge."""
        self._http_client = httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._get_headers(),
        )
        if not self.dry_run:
            self._init_clob_bridge()
        logger.info(f"Polymarket client connected (dry_run={self.dry_run})")

    def _init_clob_bridge(self) -> None:
        """Initialize authenticated CLOB trading when running live."""
        if not self.private_key:
            raise RuntimeError("private_key is required for live trading")
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise RuntimeError("api_key, api_secret, and passphrase are required for live trading")

        self._clob_bridge = ClobTradingBridge(
            host=self.rest_url,
            chain_id=self.chain_id,
            private_key=self.private_key,
            api_key=self.api_key,
            api_secret=self.api_secret,
            passphrase=self.passphrase,
        )
        self._clob_bridge.connect()

    def _index_market_tokens(self, market: Market) -> None:
        identity = market.condition_id or market.market_id
        if market.yes_token_id:
            self._token_index[market.yes_token_id] = (identity, TokenType.YES)
        if market.no_token_id:
            self._token_index[market.no_token_id] = (identity, TokenType.NO)

    def resolve_token_id(self, market_id: str, token_type: TokenType) -> str:
        market = self._markets_cache.get(market_id)
        if market is None:
            market = next(
                (
                    candidate
                    for candidate in self._markets_cache.values()
                    if candidate.condition_id == market_id
                ),
                None,
            )
        if not market:
            raise ValueError(f"Unknown market_id {market_id}; load markets before placing orders")
        token_id = market.yes_token_id if token_type == TokenType.YES else market.no_token_id
        if not token_id:
            raise ValueError(f"Market {market_id} is missing {token_type.value} token id")
        return token_id

    async def get_usdc_balance(self) -> Optional[float]:
        """Fetch collateral balance from the CLOB (live mode only)."""
        if self.dry_run or not self._clob_bridge:
            return None
        return await self._clob_bridge.get_usdc_balance()

    async def refresh_order(
        self,
        local_order: Order,
        *,
        fee_rate: float = 0.015,
    ) -> tuple[Order, list[Trade]]:
        """Fetch exchange order state and return incremental fill trades."""
        if self.dry_run or not self._clob_bridge:
            return local_order, []

        payload = await self._clob_bridge.get_order(local_order.order_id)
        remote = parse_open_order(
            payload,
            market_id=local_order.market_id,
            token_type=local_order.token_type,
            strategy_tag=local_order.strategy_tag,
        )
        trades = incremental_fill_trades(
            remote,
            local_order.filled_size,
            fee_rate=fee_rate,
        )
        return remote, trades
    
    async def disconnect(self) -> None:
        """Close connections."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        if self._ws_connection:
            await self._ws_connection.close()
            self._ws_connection = None
        logger.info("Polymarket client disconnected")
    
    def _get_headers(self) -> dict[str, str]:
        """Get authentication headers."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            # TODO: Implement proper CLOB API authentication
            # Polymarket uses L1/L2 authentication with signatures
            headers["POLY_API_KEY"] = self.api_key
        return headers
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
        base_url: Optional[str] = None,
    ) -> Any:
        """Make an HTTP request with retry logic."""
        if not self._http_client:
            await self.connect()
        client = self._http_client
        if client is None:
            raise RuntimeError("Polymarket HTTP client failed to initialize")
        
        url = f"{base_url or self.rest_url}{endpoint}"
        resilience = self._resilience.setdefault(
            base_url or self.rest_url,
            EndpointResilience(base_delay=self.retry_delay),
        )
        for attempt in range(self.max_retries):
            try:
                resilience.before_request()
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                )
                response.raise_for_status()
                resilience.record(succeeded=True)
                return response.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                resilience.record(succeeded=False)
                log = logger.debug if status == 404 else logger.warning
                log("HTTP error %s on %s: %s", status, url, e)
                if (
                    status in EndpointResilience.RETRYABLE_STATUSES
                    and attempt < self.max_retries - 1
                ):
                    await asyncio.sleep(
                        resilience.retry_delay(
                            attempt, e.response.headers.get("Retry-After")
                        )
                    )
                    continue
                raise
            except httpx.RequestError as e:
                resilience.record(succeeded=False)
                logger.warning(f"Request error on {url}: {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(resilience.retry_delay(attempt))
                    continue
                raise
    
    async def list_markets(self, filters: Optional[dict] = None) -> list[Market]:
        """
        Fetch list of available markets from Gamma API.
        
        Endpoint: GET https://gamma-api.polymarket.com/markets
        
        Uses pagination to get ALL active markets across all categories!
        """
        try:
            cache_key = json.dumps(filters or {}, sort_keys=True, default=str)
            cached = self._market_list_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < self.market_list_ttl_seconds:
                return list(cached[1])
            params = filters.copy() if filters else {}
            params.setdefault("closed", "false")
            params.setdefault("order", "volume24hr")
            params.setdefault("ascending", "false")
            
            all_markets: list[Market] = []
            offset = 0
            requested_limit = int(params.pop("limit", 100) or 100)
            max_markets = int(params.pop("max_markets", requested_limit if "limit" in (filters or {}) else 5000) or 5000)
            page_size = max(1, min(requested_limit, 100))  # Gamma API max per request
            
            logger.info(f"Fetching up to {max_markets} markets from Polymarket...")
            
            # Paginate to get all markets
            while True:
                remaining = max_markets - len(all_markets)
                if remaining <= 0:
                    break
                
                params["limit"] = min(page_size, remaining)
                params["offset"] = offset

                try:
                    data = await self._request(
                        "GET", 
                        "/markets",
                        params=params,
                        base_url=self.gamma_url,
                    )
                except httpx.HTTPStatusError as e:
                    # Gamma sometimes rejects large offsets; keep markets we already fetched.
                    if e.response.status_code == 422 and all_markets:
                        logger.warning(
                            "Gamma pagination stopped at offset=%s with HTTP 422; returning %s markets collected so far",
                            offset,
                            len(all_markets),
                        )
                        break
                    raise
                
                if not data:
                    break
                
                batch_valid = 0
                for item in data:
                    market = self._parse_market(item)
                    if market and market.yes_token_id and market.no_token_id:
                        all_markets.append(market)
                        # Cache the market for later use
                        self._cache_market(market)
                        batch_valid += 1
                
                logger.info(f"Fetched batch: offset={offset}, got {len(data)} markets ({batch_valid} valid)")
                
                if len(data) < params["limit"]:
                    # No more pages
                    break
                    
                offset += params["limit"]
                
                # Rate limiting - don't hammer the API
                await asyncio.sleep(0.15)
                
                # Safety cap
                if len(all_markets) >= max_markets:
                    logger.info(f"Reached {max_markets} market cap")
                    break
            
            logger.info(f"=== TOTAL: {len(all_markets)} active markets with valid tokens ===")
            self._market_list_cache[cache_key] = (time.monotonic(), list(all_markets))
            return all_markets
            
        except Exception as e:
            logger.error(f"Failed to fetch markets from API: {e}")
            raise
    
    async def list_events(self, filters: Optional[dict] = None) -> list[dict]:
        """
        Fetch events (which contain markets) from Gamma API.
        
        Endpoint: GET https://gamma-api.polymarket.com/events
        
        Events are useful for getting grouped markets.
        """
        try:
            params = filters.copy() if filters else {}
            params.setdefault("closed", "false")
            params.setdefault("limit", 50)
            params.setdefault("order", "id")
            params.setdefault("ascending", "false")
            
            data = await self._request(
                "GET",
                "/events",
                params=params,
                base_url=self.gamma_url,
            )
            
            return data
            
        except Exception as e:
            logger.warning(f"Failed to fetch events: {e}")
            return []
    
    def _parse_market(self, data: dict) -> Optional[Market]:
        """Parse market data from Gamma API response."""
        try:
            market_id = str(data.get("id", ""))
            condition_id = data.get("conditionId", "")
            
            if not market_id:
                return None
            
            # Parse clobTokenIds - JSON string like '["tokenId1","tokenId2"]'
            clob_token_ids_raw = data.get("clobTokenIds", "")
            yes_token_id = ""
            no_token_id = ""
            
            if clob_token_ids_raw:
                try:
                    # It's a JSON array string
                    token_ids = json.loads(clob_token_ids_raw)
                    if isinstance(token_ids, list):
                        yes_token_id = str(token_ids[0]).strip() if len(token_ids) > 0 else ""
                        no_token_id = str(token_ids[1]).strip() if len(token_ids) > 1 else ""
                except (json.JSONDecodeError, TypeError):
                    # Fallback: try comma-separated
                    token_ids = clob_token_ids_raw.split(",")
                    yes_token_id = token_ids[0].strip() if len(token_ids) > 0 else ""
                    no_token_id = token_ids[1].strip() if len(token_ids) > 1 else ""
            
            # Parse outcomes - JSON string like '["Yes", "No"]'
            outcomes_str = data.get("outcomes", "")
            # Parse outcome prices - JSON string like '[0.65, 0.35]'
            outcome_prices_str = data.get("outcomePrices", "")
            
            raw_events = data.get("events")
            parent_event = (
                raw_events[0]
                if isinstance(raw_events, list)
                and raw_events
                and isinstance(raw_events[0], dict)
                else {}
            )
            return Market(
                market_id=market_id,
                condition_id=condition_id,
                question=data.get("question", "") or "",
                description=data.get("description", "") or "",
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                active=bool(data.get("active", True)),
                closed=bool(data.get("closed", False)),
                resolved=data.get("umaResolutionStatus") == "resolved",
                volume_24h=float(data.get("volume24hr") or data.get("volume24hrClob") or 0),
                liquidity=float(
                    data.get("liquidityNum")
                    or data.get("liquidityClob")
                    or data.get("liquidity")
                    or 0
                ),
                created_at=_parse_api_datetime(
                    data.get("createdAt") or data.get("created_at")
                ),
                end_date=_parse_api_datetime(
                    data.get("endDate")
                    or data.get("endDateIso")
                    or data.get("end_date")
                ),
                category=data.get("category", "") or "",
                event_id=str(
                    data.get("eventId")
                    or parent_event.get("id")
                    or parent_event.get("slug")
                    or ""
                ),
                event_title=str(parent_event.get("title") or ""),
                outcome_label=str(
                    data.get("groupItemTitle") or data.get("outcomeLabel") or ""
                ),
                negative_risk=bool(
                    data.get("negRisk")
                    or data.get("negativeRisk")
                    or parent_event.get("negRisk")
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to parse market: {e}")
            return None

    def _cache_market(self, market: Market) -> None:
        self._markets_cache[market.market_id] = market
        if market.condition_id:
            self._markets_cache[market.condition_id] = market
        self._index_market_tokens(market)
    
    def _get_placeholder_markets(self) -> list[Market]:
        """Get placeholder markets for testing."""
        return [
            Market(
                market_id="placeholder_market_1",
                condition_id="0x1234",
                question="Will BTC be above $100k by end of 2025?",
                description="Resolves YES if Bitcoin price exceeds $100,000",
                yes_token_id="yes_token_1",
                no_token_id="no_token_1",
                active=True,
                volume_24h=50000.0,
                liquidity=100000.0,
            ),
            Market(
                market_id="placeholder_market_2",
                condition_id="0x5678",
                question="Will ETH 2.0 be fully deployed by Q2 2025?",
                description="Resolves YES if Ethereum completes all upgrades",
                yes_token_id="yes_token_2",
                no_token_id="no_token_2",
                active=True,
                volume_24h=25000.0,
                liquidity=50000.0,
            ),
        ]
    
    async def get_market(self, market_id: str) -> Market:
        """
        Get details for a specific market.
        
        Can fetch by ID or by slug:
        - GET /markets/{id} - by numeric ID
        - GET /markets/slug/{slug} - by slug
        """
        try:
            # Try fetching by ID first
            data = await self._request(
                "GET",
                f"/markets/{market_id}",
                base_url=self.gamma_url,
            )
            market = self._parse_market(data)
            if market:
                self._cache_market(market)
                return market
            raise ValueError("Failed to parse market")
        except Exception as e:
            logger.warning(f"Failed to fetch market {market_id}: {e}")
            if self.dry_run:
                return Market(
                    market_id=market_id,
                    condition_id=market_id,
                    question=f"Market {market_id}",
                    active=True,
                )
            raise

    async def get_fee_rate_bps(self, token_id: str) -> int:
        """Read the current public per-token taker base fee in basis points."""
        if not isinstance(token_id, str) or not token_id.strip():
            raise ValueError("token_id must be non-empty")
        payload = await self._request(
            "GET",
            f"/fee-rate/{quote(token_id, safe='')}",
            base_url=self.rest_url,
        )
        value = payload.get("base_fee") if isinstance(payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Polymarket base_fee must be integer basis points")
        if not 0 <= value <= 10_000:
            raise ValueError("Polymarket base_fee exceeds safety bounds")
        return value

    async def get_clob_market_info(self, condition_id: str) -> Mapping[str, Any]:
        """Read current V2 CLOB market metadata, including its fee curve."""
        if not isinstance(condition_id, str) or not condition_id.strip():
            raise ValueError("condition_id must be non-empty")
        payload = await self._request(
            "GET",
            f"/clob-markets/{quote(condition_id, safe='')}",
            base_url=self.rest_url,
        )
        if not isinstance(payload, Mapping):
            raise ValueError("Polymarket CLOB market metadata must be an object")
        return payload
    
    async def get_market_by_slug(self, slug: str) -> Market:
        """
        Get market by its slug.
        
        Endpoint: GET /markets/slug/{slug}
        
        The slug can be extracted from Polymarket URLs:
        https://polymarket.com/event/some-event-slug
        """
        try:
            data = await self._request(
                "GET",
                f"/markets/slug/{slug}",
                base_url=self.gamma_url,
            )
            market = self._parse_market(data)
            if market:
                self._cache_market(market)
                return market
            raise ValueError("Failed to parse market")
        except Exception as e:
            logger.error(f"Failed to fetch market by slug {slug}: {e}")
            raise
    
    async def get_event_by_slug(self, slug: str) -> dict:
        """
        Get event by its slug.
        
        Endpoint: GET /events/slug/{slug}
        
        Events contain multiple related markets.
        """
        try:
            data = await self._request(
                "GET",
                f"/events/slug/{slug}",
                base_url=self.gamma_url,
            )
            return data
        except Exception as e:
            logger.error(f"Failed to fetch event by slug {slug}: {e}")
            raise
    
    async def get_orderbook(self, market_id: str) -> OrderBook:
        """
        Fetch current order book for a market.
        
        Uses Polymarket CLOB API:
        GET https://clob.polymarket.com/book?token_id={token_id}
        """
        # Get market to find token IDs
        market = await self.get_market(market_id)
        
        if not market.yes_token_id or not market.no_token_id:
            logger.warning(f"No token IDs for market {market_id}")
            return OrderBook(market_id=market_id, timestamp=datetime.now(timezone.utc))
        
        # Fetch REAL order books from CLOB API
        yes_book = await self._fetch_token_orderbook(market.yes_token_id, TokenType.YES)
        no_book = await self._fetch_token_orderbook(market.no_token_id, TokenType.NO)
        
        return OrderBook(
            market_id=market_id,
            yes=yes_book,
            no=no_book,
            timestamp=datetime.now(timezone.utc),
        )

    async def get_prices_history(
        self,
        token_id: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        fidelity: Optional[int] = None,
        interval: Optional[str] = None,
    ) -> list[dict]:
        """
        Fetch historical price data for a CLOB token.

        Args:
            token_id: Polymarket CLOB token ID.
            start_ts: Optional Unix start timestamp in seconds.
            end_ts: Optional Unix end timestamp in seconds.
            fidelity: Optional resolution in minutes.
            interval: Optional API interval such as "1h", "1d", "1w", "6h", or "max".

        Returns:
            List of history points shaped like {"t": unix_seconds, "p": price}.
        """
        params: dict[str, Any] = {"market": token_id}
        if start_ts is not None:
            params["startTs"] = int(start_ts)
        if end_ts is not None:
            params["endTs"] = int(end_ts)
        if fidelity is not None:
            params["fidelity"] = int(fidelity)
        if interval:
            params["interval"] = interval

        data = await self._request(
            "GET",
            "/prices-history",
            params=params,
            base_url=self.rest_url,
        )
        return data.get("history", []) if isinstance(data, dict) else []
    
    async def _fetch_token_orderbook(self, token_id: str, token_type: TokenType) -> TokenOrderBook:
        """Fetch order book for a single token from CLOB API."""
        if token_id in self._unavailable_token_ids:
            return TokenOrderBook(token_type=token_type)
        try:
            data = await self._request(
                "GET",
                "/book",
                params={"token_id": token_id},
                base_url=self.rest_url,
            )
            
            # Parse bids and asks
            bids = []
            asks = []
            
            for bid in data.get("bids", [])[:10]:
                bids.append(PriceLevel(
                    price=float(bid.get("price", 0)),
                    size=float(bid.get("size", 0)),
                ))
            
            for ask in data.get("asks", [])[:10]:
                asks.append(PriceLevel(
                    price=float(ask.get("price", 0)),
                    size=float(ask.get("size", 0)),
                ))
            
            return TokenOrderBook(
                token_type=token_type,
                bids=OrderBookSide(levels=bids),
                asks=OrderBookSide(levels=asks),
            )
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self._unavailable_token_ids.add(token_id)
                logger.info(
                    "Suppressing stale Polymarket token with no orderbook: %s",
                    token_id,
                )
            else:
                logger.warning(f"Failed to fetch orderbook for token {token_id}: {e}")
            return TokenOrderBook(token_type=token_type)
        except Exception as e:
            logger.warning(f"Failed to fetch orderbook for token {token_id}: {e}")
            # Return empty book
            return TokenOrderBook(
                token_type=token_type,
                bids=OrderBookSide(levels=[]),
                asks=OrderBookSide(levels=[]),
            )
    
    def _generate_simulated_orderbook(self, market_id: str) -> OrderBook:
        """Generate a simulated order book for testing."""
        import random
        
        # Simulate realistic prices with occasional mispricings
        yes_mid = 0.50 + random.uniform(-0.30, 0.30)
        
        # 20% chance of significant mispricing (arb opportunity!)
        if random.random() < 0.20:
            inefficiency = random.uniform(-0.08, 0.08)  # Bigger mispricing
        else:
            inefficiency = random.uniform(-0.02, 0.02)  # Normal slight inefficiency
        
        no_mid = 1.0 - yes_mid + inefficiency
        
        spread = random.uniform(0.02, 0.06)
        
        def generate_levels(mid: float, is_bid: bool, count: int = 5) -> list[PriceLevel]:
            levels = []
            for i in range(count):
                offset = (i + 1) * 0.01
                if is_bid:
                    price = max(0.01, mid - spread/2 - offset)
                else:
                    price = min(0.99, mid + spread/2 + offset)
                size = random.uniform(100, 1000)
                levels.append(PriceLevel(price=round(price, 2), size=round(size, 2)))
            return levels
        
        yes_book = TokenOrderBook(
            token_type=TokenType.YES,
            bids=OrderBookSide(levels=generate_levels(yes_mid, is_bid=True)),
            asks=OrderBookSide(levels=generate_levels(yes_mid, is_bid=False)),
        )
        
        no_book = TokenOrderBook(
            token_type=TokenType.NO,
            bids=OrderBookSide(levels=generate_levels(no_mid, is_bid=True)),
            asks=OrderBookSide(levels=generate_levels(no_mid, is_bid=False)),
        )
        
        return OrderBook(
            market_id=market_id,
            yes=yes_book,
            no=no_book,
            timestamp=datetime.now(timezone.utc),
        )
    
    async def stream_orderbook(self, market_ids: list[str], use_simulation: bool = False) -> AsyncIterator[tuple[str, OrderBook]]:
        """
        Stream order book updates.
        
        If use_simulation=True, generates simulated data with opportunities.
        Otherwise fetches REAL data from Polymarket CLOB API.
        """
        if use_simulation:
            async for item in self._stream_simulated_orderbooks(market_ids):
                yield item
            return
        
        logger.info(f"Starting REAL orderbook stream for {len(market_ids)} markets")
        
        # We already have token IDs in the cached markets - use them directly!
        # Build token map from cached market data (no extra API calls needed)
        market_tokens: dict[str, tuple[str, str]] = {}
        
        for market_id in market_ids:
            if market_id in self._markets_cache:
                market = self._markets_cache[market_id]
                if (
                    market.active
                    and not market.closed
                    and market.yes_token_id
                    and market.no_token_id
                    and market.yes_token_id not in self._unavailable_token_ids
                    and market.no_token_id not in self._unavailable_token_ids
                ):
                    market_tokens[market_id] = (market.yes_token_id, market.no_token_id)
        
        logger.info(f"Have token IDs for {len(market_tokens)} markets (from cache)")
        
        if not market_tokens:
            logger.warning("No markets with valid token IDs found!")
            return
        
        # Settings for processing large market counts
        active_batch_size = 500  # Process 500 markets per rotation
        markets_per_request_batch = 20  # Fetch 20 at a time within the active batch
        request_delay = 0.05  # 50ms between API calls
        batch_delay = 0.3  # 300ms between request batches
        rotation_delay = 2.0  # 2 seconds before rotating to next 500
        
        market_list = list(market_tokens.keys())
        total_markets = len(market_list)
        current_offset = 0
        
        logger.info(f"Will rotate through {total_markets} markets, {active_batch_size} at a time")
        
        try:
            while True:
                # Get current batch of 500 markets
                end_offset = min(current_offset + active_batch_size, total_markets)
                active_markets = market_list[current_offset:end_offset]
                
                logger.info(f"Processing markets {current_offset+1}-{end_offset} of {total_markets}")
                
                # Process this batch
                for i in range(0, len(active_markets), markets_per_request_batch):
                    request_batch = active_markets[i:i + markets_per_request_batch]
                    
                    for market_id in request_batch:
                        try:
                            yes_token, no_token = market_tokens[market_id]
                            
                            # Fetch REAL order books from CLOB API
                            yes_book = await self._fetch_token_orderbook(yes_token, TokenType.YES)
                            no_book = await self._fetch_token_orderbook(no_token, TokenType.NO)
                            
                            orderbook = OrderBook(
                                market_id=market_id,
                                yes=yes_book,
                                no=no_book,
                                timestamp=datetime.now(timezone.utc),
                            )
                            
                            yield (market_id, orderbook)
                            await asyncio.sleep(request_delay)
                            
                        except Exception as e:
                            # Silently skip errors - don't spam logs
                            continue
                    
                    await asyncio.sleep(batch_delay)
                
                # Move to next batch of 500
                current_offset = end_offset
                if current_offset >= total_markets:
                    current_offset = 0  # Start over from beginning
                    logger.info("Completed full market cycle, starting over...")
                
                await asyncio.sleep(rotation_delay)
                
        except asyncio.CancelledError:
            logger.info("Orderbook stream cancelled")
            raise
        except Exception as e:
            logger.error(f"Orderbook stream error: {e}")
            raise
    
    async def _stream_simulated_orderbooks(self, market_ids: list[str]) -> AsyncIterator[tuple[str, OrderBook]]:
        """Generate simulated order books with occasional arbitrage opportunities."""
        import random
        
        logger.info(f"Starting SIMULATED orderbook stream for {len(market_ids)} markets")
        
        # Use subset for faster updates
        active_markets = market_ids[:100] if len(market_ids) > 100 else market_ids
        
        try:
            while True:
                # Update 10-20 random markets per cycle
                batch = random.sample(active_markets, min(15, len(active_markets)))
                
                for market_id in batch:
                    orderbook = self._generate_simulated_orderbook(market_id)
                    yield (market_id, orderbook)
                    await asyncio.sleep(0.02)  # Fast updates
                
                await asyncio.sleep(0.5)  # Brief pause between cycles
                
        except asyncio.CancelledError:
            logger.info("Simulated orderbook stream cancelled")
            raise

    async def _connect_websocket(self, market_ids: list[str]) -> None:
        """
        Connect to Polymarket WebSocket.
        
        TODO: Implement actual WebSocket connection and subscription.
        """
        try:
            self._ws_connection = await websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10,
            )
            
            # Subscribe to markets
            for market_id in market_ids:
                subscribe_msg = json.dumps({
                    "type": "subscribe",
                    "market": market_id,
                    "channel": "book",
                })
                await self._ws_connection.send(subscribe_msg)
                self._ws_subscriptions.add(market_id)
            
            logger.info(f"WebSocket connected, subscribed to {len(market_ids)} markets")
            
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise
    
    async def get_positions(self) -> dict[str, dict[TokenType, Position]]:
        """
        Get all current positions.
        
        TODO: Implement with actual Polymarket API.
        """
        if self.dry_run:
            return self._simulated_positions.copy()
        
        try:
            if not self._clob_bridge or not self._http_client:
                raise RuntimeError("live Polymarket clients are not initialized")
            address = await self._clob_bridge.get_profile_address()
            data: list[dict[str, Any]] = []
            offset = 0
            while True:
                response = await self._http_client.get(
                    f"{self.data_url}/positions",
                    params={
                        "user": address,
                        "sizeThreshold": 0,
                        "limit": 500,
                        "offset": offset,
                    },
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list):
                    raise ValueError("Polymarket positions response must be an array")
                data.extend(page)
                if len(page) < 500:
                    break
                offset += len(page)
                if offset > 10_000:
                    raise RuntimeError("Polymarket positions exceeded API pagination limit")
            positions: dict[str, dict[TokenType, Position]] = {}
            for item in data:
                market_id = str(item["conditionId"])
                token_type = (
                    TokenType.YES
                    if str(item["outcome"]).strip().lower() == "yes"
                    else TokenType.NO
                )
                positions.setdefault(market_id, {})[token_type] = Position(
                    market_id=market_id,
                    token_type=token_type,
                    size=float(item["size"]),
                    avg_entry_price=float(item.get("avgPrice", 0)),
                    realized_pnl=float(item.get("realizedPnl", 0)),
                )
            return positions
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            raise RuntimeError("Failed to fetch authoritative live positions") from e
    
    async def place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = ""
    ) -> Order:
        """Place a limit order."""
        if self.dry_run:
            order_id = f"order_{uuid.uuid4().hex[:12]}"
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
            logger.info(f"[DRY RUN] Placing order: {order}")
            self._simulated_orders[order_id] = order
            return order

        if not self._clob_bridge:
            raise RuntimeError("Live trading bridge is not initialized")

        token_id = self.resolve_token_id(market_id, token_type)
        try:
            response = await self._clob_bridge.place_limit_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )
            order_id = str(
                response.get("orderID")
                or response.get("id")
                or response.get("order_id")
                or ""
            )
            if not order_id:
                raise RuntimeError(f"Order placement returned no order id: {response}")

            payload = await self._clob_bridge.get_order(order_id)
            order = parse_open_order(
                payload,
                market_id=market_id,
                token_type=token_type,
                strategy_tag=strategy_tag,
            )
            logger.info(f"Order placed: {order.order_id}")
            return order
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            rejected = Order(
                order_id=f"rejected_{uuid.uuid4().hex[:8]}",
                market_id=market_id,
                token_type=token_type,
                side=side,
                price=price,
                size=size,
                status=OrderStatus.REJECTED,
                strategy_tag=strategy_tag,
            )
            raise RuntimeError(f"Order rejected: {e}") from e
    
    async def cancel_order(self, order_id: str) -> None:
        """Cancel an open order."""
        if self.dry_run:
            if order_id in self._simulated_orders:
                self._simulated_orders[order_id].status = OrderStatus.CANCELLED
                logger.info(f"[DRY RUN] Cancelled order: {order_id}")
            return

        if not self._clob_bridge:
            raise RuntimeError("Live trading bridge is not initialized")

        try:
            await self._clob_bridge.cancel_order(order_id)
            logger.info(f"Order cancelled: {order_id}")
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise
    
    async def cancel_all_orders(self, market_id: Optional[str] = None) -> int:
        """Cancel all open orders, optionally for a specific market."""
        orders = await self.get_open_orders(market_id)
        cancelled = 0
        
        for order in orders:
            try:
                await self.cancel_order(order.order_id)
                cancelled += 1
            except Exception as e:
                logger.warning(f"Failed to cancel order {order.order_id}: {e}")
        
        return cancelled
    
    async def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        """Get all open orders."""
        if self.dry_run:
            simulated_orders = [
                o for o in self._simulated_orders.values()
                if o.is_open and (market_id is None or o.market_id == market_id)
            ]
            return simulated_orders

        if not self._clob_bridge:
            raise RuntimeError("Live trading bridge is not initialized")

        try:
            raw_orders = await self._clob_bridge.get_open_orders()
            orders: list[Order] = []
            for item in raw_orders:
                asset_id = str(item.get("asset_id") or "")
                mapped_market_id, token_type = self._token_index.get(asset_id, ("", TokenType.YES))
                resolved_market_id = mapped_market_id or str(item.get("market") or "")
                if market_id and resolved_market_id != market_id:
                    continue
                orders.append(
                    parse_open_order(
                        item,
                        market_id=resolved_market_id,
                        token_type=token_type,
                    )
                )
            return orders
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            raise RuntimeError("Failed to fetch authoritative live open orders") from e
    
    async def get_trades(self, market_id: Optional[str] = None, limit: int = 100) -> list[Trade]:
        """Get recent trades."""
        if self.dry_run:
            simulated_trades = self._simulated_trades[-limit:]
            if market_id:
                simulated_trades = [
                    trade for trade in simulated_trades if trade.market_id == market_id
                ]
            return simulated_trades
        
        try:
            params: dict[str, Any] = {"limit": limit}
            if market_id:
                params["market_id"] = market_id
            
            data = await self._request("GET", "/trades", params=params)
            
            trades: list[Trade] = []
            for item in data:
                trades.append(Trade(
                    trade_id=item["trade_id"],
                    order_id=item["order_id"],
                    market_id=item["market_id"],
                    token_type=TokenType.YES if item["outcome"] == "Yes" else TokenType.NO,
                    side=OrderSide(item["side"]),
                    price=float(item["price"]),
                    size=float(item["size"]),
                    fee=float(item.get("fee", 0)),
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                ))
            return trades
        except Exception as e:
            logger.warning(f"Failed to fetch trades: {e}")
            return []
    
    def simulate_fill(self, order_id: str, fill_size: Optional[float] = None) -> Optional[Trade]:
        """
        Simulate an order fill (for dry run mode).
        Returns the generated trade if successful.
        """
        if order_id not in self._simulated_orders:
            return None
        
        order = self._simulated_orders[order_id]
        if not order.is_open:
            return None
        
        fill_size = fill_size or order.remaining_size
        fill_size = min(fill_size, order.remaining_size)
        
        # Create trade with realistic Polymarket fees
        # Taker fee is ~1.5% (150 bps), maker is 0%
        # Assume taker for simulation (conservative)
        notional = fill_size * order.price
        fee_rate = 0.015  # 1.5% taker fee
        fee = notional * fee_rate
        
        trade = Trade(
            trade_id=f"trade_{uuid.uuid4().hex[:12]}",
            order_id=order_id,
            market_id=order.market_id,
            token_type=order.token_type,
            side=order.side,
            price=order.price,
            size=fill_size,
            fee=fee,  # Realistic 1.5% fee
            is_simulated=True,
            simulation_label="hypothetical_paper_fill",
        )
        
        # Update order
        order.filled_size += fill_size
        order.updated_at = datetime.now(timezone.utc)
        if order.remaining_size <= 0:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        
        # Update position
        self._update_simulated_position(trade)
        self._simulated_trades.append(trade)
        
        logger.info(f"[DRY RUN] Hypothetical paper fill: {trade}")
        return trade
    
    def _update_simulated_position(self, trade: Trade) -> None:
        """Update simulated position after a trade."""
        market_id = trade.market_id
        token_type = trade.token_type
        
        if market_id not in self._simulated_positions:
            self._simulated_positions[market_id] = {}
        
        if token_type not in self._simulated_positions[market_id]:
            self._simulated_positions[market_id][token_type] = Position(
                market_id=market_id,
                token_type=token_type,
                size=0,
                avg_entry_price=0,
            )
        
        pos = self._simulated_positions[market_id][token_type]
        
        # Update position
        if trade.side == OrderSide.BUY:
            new_size = pos.size + trade.size
            if new_size > 0:
                pos.avg_entry_price = (
                    (pos.avg_entry_price * pos.size + trade.price * trade.size) / new_size
                )
            pos.size = new_size
        else:
            # SELL reduces position
            if pos.size > 0:
                realized = (trade.price - pos.avg_entry_price) * trade.size
                pos.realized_pnl += realized
            pos.size -= trade.size
