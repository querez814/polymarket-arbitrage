"""Asynchronous boundary around the shadow opportunity research system."""

from __future__ import annotations

import asyncio
import logging
import time
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
        max_event_lanes: int = 4,
        on_update: Callable[[dict], None] | None = None,
        on_observation: Callable[[ReplayObservationToken, object], None] | None = None,
    ):
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if max_event_lanes <= 0:
            raise ValueError("max_event_lanes must be positive")
        self.system = system
        self._queue: asyncio.Queue[ReplayObservationToken | None] = asyncio.Queue(
            queue_capacity
        )
        self._task: asyncio.Task | None = None
        self._event_queues: dict[str, asyncio.Queue[ReplayObservationToken | None]] = {}
        self._event_tasks: dict[str, asyncio.Task] = {}
        # These values intentionally describe only the in-process wake-up
        # queues.  Durable backlog/restart coverage remains the store's
        # responsibility, so the dashboard never mistakes this for a replay
        # completeness measurement.
        self._event_metrics: dict[str, dict[str, object]] = {}
        self._max_event_lanes = max_event_lanes
        self._running = False
        # Lanes may score canonical observations concurrently, but a paper
        # transition changes the shared portfolio.  Keep that short hand-off
        # deterministic by admitting completed scores in durable replay order.
        # ``None`` is used until startup recovery or the first persisted token
        # establishes the next sequence for a new cohort.
        self._global_completion_condition = asyncio.Condition()
        self._next_global_completion_sequence: int | None = None
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
        for route_key, queue in self._event_queues.items():
            self._start_event_task(route_key, queue)
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
            if sequences:
                self._note_global_completion_sequence(sequences[0])
            for sequence in sequences:
                await self._enqueue(
                    ReplayObservationToken(cohort_id=cohort_id, sequence=sequence)
                )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            await self._queue.put(None)
            await self._task
            self._task = None
        for queue in self._event_queues.values():
            await queue.put(None)
        if self._event_tasks:
            await asyncio.gather(*self._event_tasks.values())
        self._event_queues.clear()
        self._event_tasks.clear()

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
            self._note_global_completion_sequence(token.sequence)
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
            self._enqueue_nowait(token)
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
        return await self._run_sync(self.system.acceptance_report, lane)

    async def _run(self) -> None:
        await self._run_queue(self._queue)

    async def _run_event_lane(
        self,
        route_key: str,
        queue: asyncio.Queue[ReplayObservationToken | None],
    ) -> None:
        """Drain one sealed occurrence in strict local sequence order."""
        await self._run_queue(queue, route_key=route_key)

    async def _run_queue(
        self,
        queue: asyncio.Queue[ReplayObservationToken | None],
        *,
        route_key: str | None = None,
    ) -> None:
        while True:
            token = await queue.get()
            if token is None:
                queue.task_done()
                break
            if route_key is not None:
                self._lane_metric(route_key)["queued_at"].pop(token.sequence, None)
            started = time.perf_counter()
            succeeded = False
            try:
                result = await self._run_sync(self.system.observe_replay_token, token)
                await self._complete_observation_in_global_order(token, result)
                self.processed += 1
                succeeded = True
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1
                if route_key is not None:
                    self._lane_metric(route_key)["failures"] += 1
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
                if route_key is not None:
                    metric = self._lane_metric(route_key)
                    elapsed = time.perf_counter() - started
                    metric["latest_service_seconds"] = elapsed
                    metric["total_service_seconds"] += elapsed
                    metric["last_completed_at"] = datetime.now(timezone.utc)
                    if succeeded:
                        metric["completed"] += 1
                queue.task_done()
                self._publish()

    def _route_key(self, token: ReplayObservationToken) -> str | None:
        """Read only sealed replay provenance; catalog state cannot route work."""
        route_for = getattr(self.system, "political_replay_route", None)
        if route_for is None:
            return None
        route = route_for(token)
        return None if route is None else route.lane_key

    def _event_queue_for(
        self, token: ReplayObservationToken
    ) -> tuple[str | None, asyncio.Queue[ReplayObservationToken | None]]:
        route_key = self._route_key(token)
        if route_key is None:
            return None, self._queue
        queue = self._event_queues.get(route_key)
        if queue is not None:
            return route_key, queue
        if len(self._event_queues) >= self._max_event_lanes:
            raise asyncio.QueueFull("political event-lane capacity exhausted")
        queue = asyncio.Queue(self._queue.maxsize)
        self._event_queues[route_key] = queue
        if self._running:
            self._start_event_task(route_key, queue)
        return route_key, queue

    def _start_event_task(
        self, route_key: str, queue: asyncio.Queue[ReplayObservationToken | None]
    ) -> None:
        if route_key not in self._event_tasks:
            self._event_tasks[route_key] = asyncio.create_task(
                self._run_event_lane(route_key, queue),
                name=f"platform_opportunity_{route_key}",
            )

    def _enqueue_nowait(self, token: ReplayObservationToken) -> None:
        self._note_global_completion_sequence(token.sequence)
        route_key, queue = self._event_queue_for(token)
        try:
            queue.put_nowait(token)
        except asyncio.QueueFull:
            if route_key is not None:
                self._lane_metric(route_key)["dropped"] += 1
            raise
        self._record_lane_assignment(route_key, token)

    async def _enqueue(self, token: ReplayObservationToken) -> None:
        self._note_global_completion_sequence(token.sequence)
        route_key, queue = self._event_queue_for(token)
        await queue.put(token)
        self._record_lane_assignment(route_key, token)

    def _note_global_completion_sequence(self, sequence: int) -> None:
        """Remember the earliest known unfinished replay sequence.

        This is deliberately called at persistence/admission time, rather than
        after scoring finishes: a fast later lane must not establish itself as
        the global allocator's next decision while an earlier lane is slow.
        """
        if (
            self._next_global_completion_sequence is None
            or sequence < self._next_global_completion_sequence
        ):
            self._next_global_completion_sequence = sequence

    async def _complete_observation_in_global_order(
        self, token: ReplayObservationToken, result: object
    ) -> None:
        """Commit the shared-paper hand-off and receipt in replay order.

        Feature/scoring work has already completed before entering this
        coordinator.  A slow event therefore cannot stop another event from
        scoring; it can only defer the short, transactional portfolio decision
        which must remain deterministic across worker completion schedules.
        """
        async with self._global_completion_condition:
            if self._next_global_completion_sequence is None:
                self._next_global_completion_sequence = token.sequence
            while token.sequence != self._next_global_completion_sequence:
                await self._global_completion_condition.wait()
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
            self._next_global_completion_sequence += 1
            self._global_completion_condition.notify_all()

    def _lane_metric(self, route_key: str) -> dict[str, object]:
        return self._event_metrics.setdefault(
            route_key,
            {
                "assigned": 0,
                "completed": 0,
                "dropped": 0,
                "failures": 0,
                "total_service_seconds": 0.0,
                "latest_service_seconds": None,
                "first_assigned_at": None,
                "last_assigned_at": None,
                "last_completed_at": None,
                "queued_at": {},
            },
        )

    def _record_lane_assignment(
        self, route_key: str | None, token: ReplayObservationToken
    ) -> None:
        if route_key is None:
            return
        now = datetime.now(timezone.utc)
        metric = self._lane_metric(route_key)
        metric["assigned"] += 1
        metric["first_assigned_at"] = metric["first_assigned_at"] or now
        metric["last_assigned_at"] = now
        metric["queued_at"][token.sequence] = now

    def _event_lane_telemetry(self) -> dict[str, dict[str, object]]:
        now = datetime.now(timezone.utc)
        telemetry: dict[str, dict[str, object]] = {}
        for route_key, metric in self._event_metrics.items():
            queued_at = metric["queued_at"]
            assert isinstance(queued_at, dict)
            oldest = min(queued_at.values(), default=None)
            telemetry[route_key] = {
                "queue_depth": len(queued_at),
                "oldest_queue_age_seconds": (
                    None if oldest is None else (now - oldest).total_seconds()
                ),
                "assigned": metric["assigned"],
                "completed": metric["completed"],
                "dropped": metric["dropped"],
                "failures": metric["failures"],
                "total_service_seconds": metric["total_service_seconds"],
                "latest_service_seconds": metric["latest_service_seconds"],
                "first_assigned_at": self._iso(metric["first_assigned_at"]),
                "last_assigned_at": self._iso(metric["last_assigned_at"]),
                "last_completed_at": self._iso(metric["last_completed_at"]),
            }
        return telemetry

    @staticmethod
    def _iso(value: object) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

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
            "event_lanes": self._event_lane_telemetry(),
        }
        self._on_update(payload)
