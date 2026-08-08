"""
Data Feed Module
=================

Maintains real-time in-memory state of order books and positions
for all monitored markets.
"""

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from polymarket_client.api import PolymarketClient
from polymarket_client.models import (
    Market,
    MarketState,
    OrderBook,
    Position,
    TokenType,
)

logger = logging.getLogger(__name__)


class DataFeed:
    """
    Real-time data feed manager.

    Subscribes to order book updates via WebSocket and periodically
    refreshes positions via REST API. Provides a unified view of
    market state for the trading engine.
    """

    def __init__(
        self,
        client: PolymarketClient,
        market_ids: list[str],
        position_refresh_interval: float = 5.0,
        on_update: Optional[Callable[[str, MarketState], None]] = None,
        config=None,
    ):
        self.client = client
        self.market_ids = list(market_ids)
        self._discover_markets = not bool(market_ids)
        self.position_refresh_interval = position_refresh_interval
        self.on_update = on_update
        self.config = config

        # In-memory state
        self._markets: dict[str, Market] = {}
        self._order_books: dict[str, OrderBook] = {}
        self._positions: dict[str, dict[TokenType, Position]] = {}
        self._market_states: dict[str, MarketState] = {}

        # Tasks
        self._orderbook_task: Optional[asyncio.Task] = None
        self._position_task: Optional[asyncio.Task] = None
        self._market_resync_task: Optional[asyncio.Task] = None
        self._priority_orderbook_task: Optional[asyncio.Task] = None
        self._running = False
        self._priority_market_ids: set[str] = set()
        self._priority_market_groups: dict[str, set[str]] = {}

        # Statistics
        self._update_count = 0
        self._last_update: dict[str, datetime] = {}

    async def start(self) -> None:
        """
        Start the data feed.

        Connects to order book streams and starts position refresh loop.
        """
        if self._running:
            logger.warning("DataFeed already running")
            return

        self._running = True
        logger.info(f"Starting DataFeed for {len(self.market_ids)} markets")

        # Fetch initial market info
        await self._fetch_markets()

        # Fetch initial positions
        await self._refresh_positions()

        # Start streaming order books
        self._orderbook_task = asyncio.create_task(
            self._stream_orderbooks(), name="orderbook_stream"
        )

        # Start position refresh loop
        self._position_task = asyncio.create_task(
            self._position_refresh_loop(), name="position_refresh"
        )
        self._market_resync_task = asyncio.create_task(
            self._market_resync_loop(),
            name="market_resync",
        )

        logger.info("DataFeed started successfully")

    async def stop(self) -> None:
        """Stop the data feed."""
        self._running = False

        if self._orderbook_task:
            self._orderbook_task.cancel()
            try:
                await self._orderbook_task
            except asyncio.CancelledError:
                pass

        if self._position_task:
            self._position_task.cancel()
            try:
                await self._position_task
            except asyncio.CancelledError:
                pass

        for task in (self._market_resync_task, self._priority_orderbook_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("DataFeed stopped")

    async def _fetch_markets(self) -> None:
        """Fetch market information for all monitored markets."""
        try:
            if self._discover_markets:
                # Discover markets if none specified - list_markets returns full Market objects!
                markets = await self.client.list_markets({"active": True})

                min_volume = float(
                    getattr(
                        getattr(self.config, "mode", None),
                        "semantic_min_polymarket_volume_24h",
                        0.0,
                    )
                    or 0.0
                )
                eligible = [
                    market for market in markets if market.volume_24h >= min_volume
                ]
                self._replace_market_snapshot(eligible)
                logger.info(
                    "Polymarket discovery filtered by 24h volume | "
                    "raw=%s eligible=%s filtered=%s min_volume_24h=%.2f",
                    len(markets),
                    len(eligible),
                    len(markets) - len(eligible),
                    min_volume,
                )
            else:
                # Only fetch if specific market_ids were provided
                markets = []
                for market_id in self.market_ids:
                    market = await self.client.get_market(market_id)
                    if market is not None:
                        markets.append(market)
                self._replace_market_snapshot(markets)

        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            raise

    def _replace_market_snapshot(self, markets: list[Market]) -> None:
        """Atomically replace discovery state and prune closed-market data."""
        replacement = {market.market_id: market for market in markets}
        removed = set(self._markets) - set(replacement)
        self._markets = replacement
        self.market_ids = list(replacement)
        for stale in removed:
            self._order_books.pop(stale, None)
            self._positions.pop(stale, None)
            self._market_states.pop(stale, None)
            self._last_update.pop(stale, None)
        self._priority_market_ids.intersection_update(replacement)
        for group in self._priority_market_groups.values():
            group.intersection_update(replacement)

    async def resync_markets(self, *, restart_stream: bool = True) -> None:
        """Refresh the active snapshot and restart its snapshot-based stream."""
        previous = set(self.market_ids)
        await self._fetch_markets()
        current = set(self.market_ids)
        logger.info(
            "Polymarket market resync complete | active=%s added=%s pruned=%s",
            len(current),
            len(current - previous),
            len(previous - current),
        )
        if restart_stream and self._running:
            if self._orderbook_task:
                self._orderbook_task.cancel()
                try:
                    await self._orderbook_task
                except asyncio.CancelledError:
                    pass
            self._orderbook_task = asyncio.create_task(
                self._stream_orderbooks(),
                name="orderbook_stream",
            )

    async def _market_resync_loop(self) -> None:
        interval = float(
            getattr(
                getattr(self.config, "mode", None),
                "polymarket_market_resync_seconds",
                1800.0,
            )
            or 1800.0
        )
        while self._running:
            try:
                await asyncio.sleep(interval)
                if self._running:
                    await self.resync_markets()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Polymarket market resync failed")

    def set_priority_markets(self, market_ids: list[str]) -> None:
        """Set the backwards-compatible default priority group."""
        self.set_priority_market_group("default", market_ids)

    def set_priority_market_group(self, source: str, market_ids: list[str]) -> None:
        """Merge independently owned priority sets without last-writer loss."""
        if not source.strip():
            raise ValueError("priority market group source must be non-empty")
        group = set(market_ids).intersection(self._markets)
        if group:
            self._priority_market_groups[source] = group
        else:
            self._priority_market_groups.pop(source, None)
        priority = set().union(*self._priority_market_groups.values()) if self._priority_market_groups else set()
        if priority == self._priority_market_ids:
            return
        self._priority_market_ids = priority
        logger.info(
            "Polymarket priority orderbook lane updated | markets=%s interval=%.2fs",
            len(priority),
            self._priority_refresh_interval,
        )
        if (
            self._running
            and priority
            and (
                self._priority_orderbook_task is None
                or self._priority_orderbook_task.done()
            )
        ):
            self._priority_orderbook_task = asyncio.create_task(
                self._priority_orderbook_loop(),
                name="priority_orderbook_stream",
            )

    @property
    def _priority_refresh_interval(self) -> float:
        return float(
            getattr(
                getattr(self.config, "mode", None),
                "polymarket_priority_refresh_seconds",
                2.0,
            )
            or 2.0
        )

    async def _priority_orderbook_loop(self) -> None:
        semaphore = asyncio.Semaphore(8)
        while self._running:
            started = asyncio.get_running_loop().time()

            async def refresh(market_id: str) -> None:
                async with semaphore:
                    orderbook = await self.client.get_orderbook(market_id)
                    self._record_orderbook(market_id, orderbook)

            try:
                market_ids = sorted(self._priority_market_ids)
                if market_ids:
                    results = await asyncio.gather(
                        *(refresh(market_id) for market_id in market_ids),
                        return_exceptions=True,
                    )
                    failures = sum(
                        isinstance(result, BaseException) for result in results
                    )
                    if failures:
                        logger.warning(
                            "Polymarket priority lane refresh failures=%s/%s",
                            failures,
                            len(market_ids),
                        )
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(
                    max(0.05, self._priority_refresh_interval - elapsed)
                )
            except asyncio.CancelledError:
                raise

    async def _stream_orderbooks(self) -> None:
        """Stream order book updates."""
        broad_stream_enabled = getattr(
            getattr(self.config, "mode", None),
            "polymarket_broad_orderbook_stream_enabled",
            True,
        )
        if broad_stream_enabled is not True:
            logger.info(
                "Polymarket broad orderbook stream disabled; "
                "verified pair snapshots own cross-platform reads"
            )
            while self._running:
                await asyncio.sleep(60)
            return

        # Use simulation for demo/screenshots, real data for production
        # Check config.mode.data_mode (set in config.yaml)
        use_simulation = getattr(self.config, "use_simulation", False)

        while self._running:
            try:
                async for market_id, orderbook in self.client.stream_orderbook(
                    self.market_ids, use_simulation=use_simulation
                ):
                    if not self._running:
                        break

                    self._record_orderbook(market_id, orderbook)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Order book stream error: {e}")
                if self._running:
                    await asyncio.sleep(1)  # Brief delay before reconnecting

    def _record_orderbook(self, market_id: str, orderbook: OrderBook) -> None:
        self._order_books[market_id] = orderbook
        self._last_update[market_id] = datetime.utcnow()
        self._update_count += 1
        self._update_market_state(market_id)

    async def _position_refresh_loop(self) -> None:
        """Periodically refresh positions."""
        while self._running:
            try:
                await asyncio.sleep(self.position_refresh_interval)
                await self._refresh_positions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Position refresh error: {e}")

    async def _refresh_positions(self) -> None:
        """Fetch current positions from API."""
        try:
            self._positions = await self.client.get_positions()
            logger.debug(f"Refreshed positions for {len(self._positions)} markets")

            # Update market states with new positions
            for market_id in self._positions:
                if market_id in self._order_books:
                    self._update_market_state(market_id)

        except Exception as e:
            logger.warning(f"Failed to refresh positions: {e}")

    def _update_market_state(self, market_id: str) -> None:
        """Update the complete market state for a market."""
        if market_id not in self._markets:
            return

        state = MarketState(
            market=self._markets.get(
                market_id,
                Market(market_id=market_id, condition_id=market_id, question=""),
            ),
            order_book=self._order_books.get(market_id, OrderBook(market_id=market_id)),
            positions=self._positions.get(market_id, {}),
            open_orders=[],  # Will be populated by execution engine
            timestamp=datetime.utcnow(),
        )

        self._market_states[market_id] = state

        # Notify callback if set
        if self.on_update:
            try:
                self.on_update(market_id, state)
            except Exception as e:
                logger.error(f"Update callback error for {market_id}: {e}")

    def get_market_state(self, market_id: str) -> Optional[MarketState]:
        """
        Get the latest state snapshot for a market.

        Returns None if the market hasn't been loaded yet.
        """
        return self._market_states.get(market_id)

    def get_all_market_states(self) -> dict[str, MarketState]:
        """Get all current market states."""
        return self._market_states.copy()

    def get_order_book(self, market_id: str) -> Optional[OrderBook]:
        """Get the latest order book for a market."""
        return self._order_books.get(market_id)

    def get_position(self, market_id: str, token_type: TokenType) -> Optional[Position]:
        """Get position for a specific market and token."""
        market_positions = self._positions.get(market_id, {})
        return market_positions.get(token_type)

    def get_positions(self, market_id: str) -> dict[TokenType, Position]:
        """Get all positions for a market."""
        return self._positions.get(market_id, {})

    def get_market(self, market_id: str) -> Optional[Market]:
        """Get market information."""
        return self._markets.get(market_id)

    @property
    def update_count(self) -> int:
        """Get total number of order book updates received."""
        return self._update_count

    @property
    def is_running(self) -> bool:
        """Check if the data feed is running."""
        return self._running

    def get_staleness(self, market_id: str) -> Optional[float]:
        """
        Get time since last update for a market (in seconds).
        Returns None if never updated.
        """
        if market_id not in self._last_update:
            return None
        return (datetime.utcnow() - self._last_update[market_id]).total_seconds()

    async def wait_for_data(self, timeout: float = 10.0) -> bool:
        """
        Wait until data is available for all markets.

        Returns True if data is available, False on timeout.
        """
        start = datetime.utcnow()
        while (datetime.utcnow() - start).total_seconds() < timeout:
            if all(m in self._order_books for m in self.market_ids):
                return True
            await asyncio.sleep(0.1)
        return False
