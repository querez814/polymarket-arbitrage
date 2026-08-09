"""Asynchronous boundary around the shadow opportunity research system."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from kalshi_client.models import KalshiMarket, KalshiMilestone
from polymarket_client.models import Market, OrderBook

from core.platform_opportunities import (
    CatalogRefresh,
    CatalystReference,
    PlatformOpportunitySystem,
    VenueFeeSchedule,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookEnvelope:
    contract_id: str
    book: OrderBook
    observed_at: datetime
    fee_schedule: VenueFeeSchedule


class PlatformOpportunityWorker:
    """Bounded, non-blocking producer with off-hot-path persistence/scoring."""

    def __init__(
        self,
        system: PlatformOpportunitySystem,
        *,
        queue_capacity: int = 10_000,
        on_update: Callable[[dict], None] | None = None,
    ):
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        self.system = system
        self._queue: asyncio.Queue[BookEnvelope | None] = asyncio.Queue(queue_capacity)
        self._operation_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False
        self._on_update = on_update
        self.processed = 0
        self.dropped = 0
        self.failures = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run(), name="platform_opportunity_shadow_worker"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            await self._queue.put(None)
            await self._task
            self._task = None

    def submit_book(
        self,
        contract_id: str,
        book: OrderBook,
        *,
        observed_at: datetime,
        fee_schedule: VenueFeeSchedule,
    ) -> bool:
        """Never wait in a feed/execution callback; drop visibly if saturated."""
        try:
            self._queue.put_nowait(
                BookEnvelope(contract_id, book, observed_at, fee_schedule)
            )
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    async def refresh_catalog(
        self,
        *,
        polymarket_markets: Sequence[Market],
        kalshi_markets: Sequence[KalshiMarket],
        kalshi_milestones: Sequence[KalshiMilestone] = (),
        catalyst_references: Sequence[CatalystReference] = (),
        snapshot_complete: bool = True,
        observed_at: datetime,
    ) -> CatalogRefresh:
        async with self._operation_lock:
            result = await self._run_sync(
                self.system.refresh_catalog,
                polymarket_markets=polymarket_markets,
                kalshi_markets=kalshi_markets,
                kalshi_milestones=kalshi_milestones,
                catalyst_references=catalyst_references,
                snapshot_complete=snapshot_complete,
                observed_at=observed_at,
            )
            await self._run_sync(
                self.system.discover_structural_relations, observed_at=observed_at
            )
        self._publish()
        return result

    async def acceptance_report(self, lane: str):
        """Expensive clustered bootstrap always stays outside the scanner."""
        async with self._operation_lock:
            return await self._run_sync(self.system.acceptance_report, lane)

    async def _run(self) -> None:
        while True:
            envelope = await self._queue.get()
            if envelope is None:
                self._queue.task_done()
                break
            try:
                async with self._operation_lock:
                    self.system.set_fee_schedule(
                        envelope.contract_id, envelope.fee_schedule
                    )
                    await self._run_sync(
                        self.system.observe_book,
                        envelope.contract_id,
                        envelope.book,
                        observed_at=envelope.observed_at,
                    )
                self.processed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1
                logger.exception(
                    "Shadow opportunity observation failed | contract=%s",
                    envelope.contract_id,
                )
            finally:
                self._queue.task_done()
                self._publish()

    @staticmethod
    async def _run_sync(function, /, *args, **kwargs):
        """Cancellation waits for the underlying thread before resources close."""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _publish(self) -> None:
        if self._on_update is None:
            return
        payload = self.system.dashboard_summary()
        payload["worker"] = {
            "queued": self._queue.qsize(),
            "processed": self.processed,
            "dropped": self.dropped,
            "failures": self.failures,
        }
        self._on_update(payload)
