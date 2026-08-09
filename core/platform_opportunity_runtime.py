"""Asynchronous boundary around the shadow opportunity research system."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Sequence

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
    request_started_at: datetime | None
    received_at: datetime | None
    fee_schedule: VenueFeeSchedule
    book_received_at: datetime | None = None
    fee_request_started_at: datetime | None = None
    fee_received_at: datetime | None = None


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
        request_started_at: datetime | None = None,
        received_at: datetime | None = None,
        fee_schedule: VenueFeeSchedule,
        book_received_at: datetime | None = None,
        fee_request_started_at: datetime | None = None,
        fee_received_at: datetime | None = None,
    ) -> bool:
        """Never wait in a feed/execution callback; drop visibly if saturated."""
        try:
            self._queue.put_nowait(
                BookEnvelope(
                    contract_id,
                    book,
                    observed_at,
                    request_started_at,
                    received_at,
                    fee_schedule,
                    book_received_at,
                    fee_request_started_at,
                    fee_received_at,
                )
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
        venue_coverage: Mapping[str, Mapping[str, object]] | None = None,
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
                venue_coverage=venue_coverage,
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
                    persist_replay = getattr(
                        self.system, "persist_replay_observation", None
                    )
                    scored_book = envelope.book
                    if persist_replay is not None:
                        replay_evidence = await self._run_sync(
                            persist_replay,
                            envelope.contract_id,
                            envelope.book,
                            observed_at=envelope.observed_at,
                            request_started_at=envelope.request_started_at,
                            received_at=envelope.received_at,
                            fee_schedule=envelope.fee_schedule,
                            book_received_at=envelope.book_received_at,
                            fee_request_started_at=envelope.fee_request_started_at,
                            fee_received_at=envelope.fee_received_at,
                        )
                        replay_book = getattr(
                            self.system, "replay_book_for_state_hash", None
                        )
                        if replay_book is not None:
                            scored_book = await self._run_sync(
                                replay_book,
                                replay_evidence["state_hash"],
                                market_id=envelope.book.market_id,
                            )
                    await self._run_sync(
                        self.system.observe_book,
                        envelope.contract_id,
                        scored_book,
                        observed_at=envelope.observed_at,
                    )
                    record = getattr(self.system, "record_successful_observation", None)
                    if record is not None and persist_replay is None:
                        await self._run_sync(
                            record,
                            envelope.contract_id,
                            observed_at=envelope.observed_at,
                            request_started_at=envelope.request_started_at,
                            received_at=envelope.received_at,
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
