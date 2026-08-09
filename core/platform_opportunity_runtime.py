"""Asynchronous boundary around the shadow opportunity research system."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from kalshi_client.models import KalshiMarket, KalshiMilestone
from polymarket_client.models import Market, OrderBook

from core.platform_opportunities import (
    CatalogRefresh,
    CatalystReference,
    PlatformOpportunitySystem,
    ReplayObservationToken,
    VenueFeeSchedule,
)

logger = logging.getLogger(__name__)


class PlatformOpportunityWorker:
    """Bounded, non-blocking producer with off-hot-path persistence/scoring."""

    def __init__(
        self,
        system: PlatformOpportunitySystem,
        *,
        queue_capacity: int = 10_000,
        on_update: Callable[[dict], None] | None = None,
        on_observation: Callable[[ReplayObservationToken, object], None] | None = None,
    ):
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        self.system = system
        self._queue: asyncio.Queue[ReplayObservationToken | None] = asyncio.Queue(
            queue_capacity
        )
        self._operation_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False
        self._on_update = on_update
        # This hook receives only the sealed replay token and the result scored
        # from its canonical evidence.  It is the narrow runtime hand-off used
        # by experimental paper; raw adapter books never reappear here.
        self._on_observation = on_observation
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
        # Queue delivery is only a wake-up optimization.  On every process
        # start, reload durable tokens which never received a completion
        # receipt (including queue drops and a crash after persistence) in
        # canonical sequence order.  This keeps replay evidence from being
        # silently stranded in the previous process's memory queue.
        store = getattr(self.system, "store", None)
        cohort_id = getattr(self.system, "cohort_id", None)
        if store is not None and isinstance(cohort_id, str):
            sequences = await self._run_sync(
                store.unprocessed_replay_observation_sequences,
                cohort_id=cohort_id,
            )
            for sequence in sequences:
                await self._queue.put(
                    ReplayObservationToken(cohort_id=cohort_id, sequence=sequence)
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
        """Persist completed evidence before token-only queue admission.

        The raw adapter book is intentionally consumed here and never crosses
        the asynchronous decision boundary.  A queue drop therefore leaves a
        canonical observation plus durable coverage-gap evidence behind.
        """
        failure_at = received_at or observed_at
        try:
            replay_evidence = self.system.persist_replay_observation(
                contract_id,
                book,
                observed_at=observed_at,
                request_started_at=request_started_at,
                received_at=received_at,
                fee_schedule=fee_schedule,
                book_received_at=book_received_at,
                fee_request_started_at=fee_request_started_at,
                fee_received_at=fee_received_at,
            )
            token = ReplayObservationToken(
                cohort_id=self.system.cohort_id,
                sequence=int(replay_evidence["event"]["sequence"]),
            )
        except Exception:
            self.failures += 1
            self.system.record_observation_failure(
                contract_id, reason_code="processing_failed", failed_at=failure_at
            )
            logger.exception(
                "Shadow opportunity replay persistence failed | contract=%s",
                contract_id,
            )
            self._publish()
            return False
        try:
            self._queue.put_nowait(token)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            self.system.record_observation_failure(
                contract_id, reason_code="queue_drop", failed_at=failure_at
            )
            self._publish()
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
            token = await self._queue.get()
            if token is None:
                self._queue.task_done()
                break
            try:
                async with self._operation_lock:
                    result = await self._run_sync(
                        self.system.observe_replay_token, token
                    )
                    if self._on_observation is not None:
                        await self._run_sync(self._on_observation, token, result)
                    store = getattr(self.system, "store", None)
                    if store is not None:
                        await self._run_sync(
                            store.record_replay_processing_receipt,
                            cohort_id=token.cohort_id,
                            sequence=token.sequence,
                            completed_at=datetime.now(timezone.utc),
                        )
                self.processed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1
                store = getattr(self.system, "store", None)
                if store is None:
                    logger.exception(
                        "Shadow opportunity observation failed | cohort=%s sequence=%s",
                        token.cohort_id,
                        token.sequence,
                    )
                    continue
                event = await self._run_sync(
                    store.replay_observation_event,
                    cohort_id=token.cohort_id,
                    sequence=token.sequence,
                )
                failed_at = datetime.fromisoformat(str(event["received_at"]))
                await self._run_sync(
                    self.system.record_observation_failure,
                    str(event["contract_id"]),
                    reason_code="processing_failed",
                    failed_at=failed_at,
                )
                logger.exception(
                    "Shadow opportunity observation failed | cohort=%s sequence=%s",
                    token.cohort_id,
                    token.sequence,
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
