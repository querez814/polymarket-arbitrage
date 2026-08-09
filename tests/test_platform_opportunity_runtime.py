import asyncio
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

import pytest

from kalshi_client.models import KalshiMarket, KalshiMilestone

from core.platform_opportunities import (
    PoliticalWatchPolicy,
    PlatformOpportunitySystem,
    ReplayObservationToken,
    VenueFeeSchedule,
)
from core.platform_opportunity_runtime import PlatformOpportunityWorker
from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from utils.platform_opportunity_store import PlatformOpportunityStore


@pytest.mark.asyncio
@pytest.mark.parametrize("background_operation", ["catalog", "acceptance"])
async def test_worker_processes_tokens_while_catalog_or_acceptance_runs(
    background_operation,
):
    """Slow catalog/report work cannot block the canonical decision path."""

    class System:
        def __init__(self):
            self.cohort_id = "cohort:parallel"
            self.started = Event()
            self.release = Event()
            self.processed = Event()

        def persist_replay_observation(self, _contract_id, _book, **_kwargs):
            return {"event": {"sequence": 1}}

        def observe_replay_token(self, _token):
            self.processed.set()
            return {"canonical": 1}

        def refresh_catalog(self, **_kwargs):
            self.started.set()
            assert self.release.wait(timeout=2)
            return {"refreshed": True}

        def discover_structural_relations(self, **_kwargs):
            return None

        def acceptance_report(self, _lane):
            self.started.set()
            assert self.release.wait(timeout=2)
            return {"accepted": False}

        def dashboard_summary(self):
            return {}

    system = System()
    worker = PlatformOpportunityWorker(system)
    await worker.start()
    if background_operation == "catalog":
        background = asyncio.create_task(
            worker.refresh_catalog(
                polymarket_markets=[],
                kalshi_markets=[],
                observed_at=datetime.now(timezone.utc),
            )
        )
    else:
        background = asyncio.create_task(worker.acceptance_report("test"))
    try:
        await asyncio.wait_for(asyncio.to_thread(system.started.wait), timeout=1)
        schedule = VenueFeeSchedule(
            "polymarket", "none", 0, 1, 0, datetime.now(timezone.utc), "test"
        )
        assert worker.submit_book(
            "polymarket:parallel",
            OrderBook(market_id="parallel"),
            observed_at=datetime.now(timezone.utc),
            fee_schedule=schedule,
        )
        await asyncio.wait_for(asyncio.to_thread(system.processed.wait), timeout=1)
    finally:
        system.release.set()
        await background
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_drains_queued_observations_before_shutdown():
    class System:
        def __init__(self):
            self.processed = []
            self.persisted = 0
            self.cohort_id = "cohort:test"

        def persist_replay_observation(self, contract_id, book, **kwargs):
            self.persisted += 1
            return {"event": {"sequence": self.persisted}}

        def observe_replay_token(self, token):
            self.processed.append(token.sequence)
            return {"canonical": token.sequence}

        def dashboard_summary(self):
            return {}

    system = System()
    observed = []
    worker = PlatformOpportunityWorker(
        system,
        queue_capacity=10,
        on_observation=lambda token, result: observed.append(
            (token.sequence, result["canonical"])
        ),
    )
    schedule = VenueFeeSchedule(
        "polymarket",
        "none",
        0,
        1,
        0,
        datetime.now(timezone.utc),
        "test",
    )
    await worker.start()
    for index in range(3):
        assert worker.submit_book(
            f"polymarket:{index}",
            OrderBook(market_id=str(index)),
            observed_at=datetime.now(timezone.utc),
            fee_schedule=schedule,
        )

    await worker.stop()

    assert system.processed == [1, 2, 3]
    assert observed == [(1, 1), (2, 2), (3, 3)]
    assert worker.processed == 3


@pytest.mark.asyncio
async def test_worker_occurrence_lane_does_not_starve_another_occurrence():
    """A slow sealed occurrence may not hold another route behind one worker lock."""

    class System:
        def __init__(self):
            self.cohort_id = "cohort:lanes"
            self.persisted = 0
            self.slow_started = Event()
            self.release_slow = Event()
            self.fast_processed = Event()

        def persist_replay_observation(self, _contract_id, _book, **_kwargs):
            self.persisted += 1
            return {"event": {"sequence": self.persisted}}

        def political_replay_route(self, token):
            return SimpleNamespace(lane_key=f"occurrence:{token.sequence}")

        def observe_replay_token(self, token):
            if token.sequence == 1:
                self.slow_started.set()
                assert self.release_slow.wait(timeout=2)
            else:
                self.fast_processed.set()
            return {"canonical": token.sequence}

        def dashboard_summary(self):
            return {}

    system = System()
    worker = PlatformOpportunityWorker(system, max_event_lanes=2)
    schedule = VenueFeeSchedule(
        "polymarket", "none", 0, 1, 0, datetime.now(timezone.utc), "test"
    )
    await worker.start()
    try:
        assert worker.submit_book(
            "polymarket:slow",
            OrderBook(market_id="slow"),
            observed_at=datetime.now(timezone.utc),
            fee_schedule=schedule,
        )
        await asyncio.wait_for(asyncio.to_thread(system.slow_started.wait), timeout=1)
        assert worker.submit_book(
            "polymarket:fast",
            OrderBook(market_id="fast"),
            observed_at=datetime.now(timezone.utc),
            fee_schedule=schedule,
        )
        await asyncio.wait_for(asyncio.to_thread(system.fast_processed.wait), timeout=1)
    finally:
        system.release_slow.set()
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_persists_before_queueing_and_records_queue_drop(tmp_path):
    """A saturated queue cannot discard a completed read without durable evidence."""
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(tmp_path / "db"))
    worker = PlatformOpportunityWorker(system, queue_capacity=1)
    observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, observed_at, "test")

    assert worker.submit_book(
        "polymarket:first",
        OrderBook(market_id="first"),
        observed_at=observed_at,
        fee_schedule=schedule,
    )
    assert not worker.submit_book(
        "polymarket:dropped",
        OrderBook(market_id="dropped"),
        observed_at=observed_at,
        fee_schedule=schedule,
    )

    assert [
        event["contract_id"]
        for event in system.store.replay_observation_events(cohort_id=system.cohort_id)
    ] == ["polymarket:first", "polymarket:dropped"]
    assert system.store.observation_failure_telemetry(cohort_id=system.cohort_id) == {
        "polymarket:dropped": {
            "queue_drop": {
                "failure_count": 1,
                "last_failed_at": "2026-08-09T00:00:00+00:00",
            }
        }
    }


@pytest.mark.asyncio
async def test_worker_restart_drains_persisted_token_without_a_queue_notification(
    tmp_path,
):
    """A completed read survives a process boundary until its hook completes."""
    path = tmp_path / "opportunities.db"
    observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, observed_at, "test")
    first = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    # Do not start the first worker: this models a crash after durable replay
    # persistence but before an in-memory queue notification can be consumed.
    first_worker = PlatformOpportunityWorker(first)
    assert first_worker.submit_book(
        "polymarket:restart-token",
        OrderBook(market_id="restart-token"),
        observed_at=observed_at,
        fee_schedule=schedule,
    )
    assert first.store.unprocessed_replay_observation_sequences(
        cohort_id=first.cohort_id
    ) == [1]
    first.store.close()

    resumed = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    delivered = []
    worker = PlatformOpportunityWorker(
        resumed,
        on_observation=lambda token, _result: delivered.append(token.sequence),
    )
    await worker.start()
    await worker.stop()

    assert delivered == [1]
    # The worker persisted a terminal no-signal envelope before acknowledging
    # the token.  A later restart can advance this token without rebuilding
    # scorer feature history merely to rediscover that absence.
    decision = resumed.political_scored_decision(
        ReplayObservationToken(resumed.cohort_id, 1)
    )
    assert decision["outcome"] == "no_signal"
    assert decision["signal"] == {}
    assert (
        resumed.store.unprocessed_replay_observation_sequences(
            cohort_id=resumed.cohort_id
        )
        == []
    )
    resumed.store.close()

    # A second restart sees the durable receipt and must not rescore or
    # re-deliver the completed token.
    restarted = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    duplicate_delivery = []
    restarted_worker = PlatformOpportunityWorker(
        restarted,
        on_observation=lambda token, _result: duplicate_delivery.append(token.sequence),
    )
    await restarted_worker.start()
    await restarted_worker.stop()
    assert duplicate_delivery == []


@pytest.mark.asyncio
async def test_worker_records_durable_processing_gap_for_persisted_token(tmp_path):
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(tmp_path / "db"))
    worker = PlatformOpportunityWorker(system)
    observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, observed_at, "test")

    def fail_decision(_token):
        raise RuntimeError("deliberate decision failure")

    system.observe_replay_token = fail_decision
    await worker.start()
    assert worker.submit_book(
        "polymarket:decision-failure",
        OrderBook(market_id="decision-failure"),
        observed_at=observed_at,
        fee_schedule=schedule,
    )
    await worker.stop()

    assert worker.failures == 1
    assert system.store.observation_failure_telemetry(cohort_id=system.cohort_id) == {
        "polymarket:decision-failure": {
            "processing_failed": {
                "failure_count": 1,
                "last_failed_at": "2026-08-09T00:00:00+00:00",
            }
        }
    }


@pytest.mark.asyncio
async def test_worker_persists_exact_kalshi_milestone_window_in_political_lock(
    tmp_path,
):
    """The real dashboard worker must carry exact milestone evidence to the store."""
    start = datetime(2026, 8, 10, 22, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 10, 23, 15, tzinfo=timezone.utc)
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(
            max_events=1,
            reviewed_pinned_event_ids=("kalshi:KXTRUMPMENTION-26AUG10",),
        ),
    )
    worker = PlatformOpportunityWorker(system)

    await worker.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            KalshiMarket(
                ticker="KXTRUMPMENTION-26AUG10-T1",
                event_ticker="KXTRUMPMENTION-26AUG10",
                series_ticker="KXTRUMPMENTION",
                title="Will Trump mention tariffs?",
                event_title="Trump remarks",
                category="Politics",
                volume=1_000,
                open_interest=500,
            )
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="trump-remarks",
                title="Trump remarks",
                category="Politics",
                milestone_type="speech",
                start_time=start,
                end_time=end,
                related_event_tickers=("KXTRUMPMENTION-26AUG10",),
                primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
                source_id="official-schedule",
            )
        ],
        observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    [lock] = system.store.active_political_event_locks(
        now=datetime(2026, 8, 9, tzinfo=timezone.utc)
    )
    assert lock["event_start_at"] == "2026-08-10T22:30:00+00:00"
    assert lock["event_end_at"] == "2026-08-10T23:15:00+00:00"


@pytest.mark.asyncio
async def test_worker_persists_successful_empty_depth_observation_across_restart(
    tmp_path,
):
    """An assigned contract is not an observation until its book read completes."""
    path = tmp_path / "opportunities.db"
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    worker = PlatformOpportunityWorker(system)
    observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, observed_at, "test")

    await worker.start()
    assert worker.submit_book(
        "polymarket:empty-depth",
        OrderBook(market_id="empty-depth"),
        observed_at=observed_at,
        fee_schedule=schedule,
    )
    await worker.stop()

    assert system.store.observation_telemetry(cohort_id=system.cohort_id) == {
        "polymarket:empty-depth": {
            "observation_count": 1,
            "first_observed_at": "2026-08-09T00:00:00+00:00",
            "last_observed_at": "2026-08-09T00:00:00+00:00",
            "last_request_started_at": None,
            "last_received_at": None,
        }
    }
    system.store.close()

    resumed = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    assert resumed.store.observation_telemetry(cohort_id=resumed.cohort_id) == {
        "polymarket:empty-depth": {
            "observation_count": 1,
            "first_observed_at": "2026-08-09T00:00:00+00:00",
            "last_observed_at": "2026-08-09T00:00:00+00:00",
            "last_request_started_at": None,
            "last_received_at": None,
        }
    }


@pytest.mark.asyncio
async def test_worker_persists_local_book_request_and_receipt_times(tmp_path):
    """Request timing is durable evidence, not an adapter timestamp substitute."""
    path = tmp_path / "opportunities.db"
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    worker = PlatformOpportunityWorker(system)
    request_started_at = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    received_at = request_started_at.replace(second=1)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, received_at, "test")

    await worker.start()
    assert worker.submit_book(
        "polymarket:timed-book",
        OrderBook(market_id="timed-book"),
        observed_at=received_at,
        request_started_at=request_started_at,
        received_at=received_at,
        fee_schedule=schedule,
    )
    await worker.stop()

    assert system.store.observation_telemetry(cohort_id=system.cohort_id) == {
        "polymarket:timed-book": {
            "observation_count": 1,
            "first_observed_at": "2026-08-09T12:00:01+00:00",
            "last_observed_at": "2026-08-09T12:00:01+00:00",
            "last_request_started_at": "2026-08-09T12:00:00+00:00",
            "last_received_at": "2026-08-09T12:00:01+00:00",
        }
    }


@pytest.mark.asyncio
async def test_worker_persists_replay_event_before_scoring_an_observation(tmp_path):
    """A successful observation has a durable causal replay event first."""
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(tmp_path / "db"))
    worker = PlatformOpportunityWorker(system)
    request_started_at = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    received_at = request_started_at.replace(second=1)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, received_at, "test")

    await worker.start()
    assert worker.submit_book(
        "polymarket:replayable",
        OrderBook(market_id="replayable"),
        observed_at=received_at,
        request_started_at=request_started_at,
        received_at=received_at,
        fee_schedule=schedule,
    )
    await worker.stop()

    [event] = system.store.replay_observation_events(cohort_id=system.cohort_id)
    assert event == {
        "sequence": 1,
        "contract_id": "polymarket:replayable",
        "kind": "change",
        "lock_phase": "unclassified",
        "state_hash": event["state_hash"],
        "fee_hash": event["fee_hash"],
        "request_started_at": "2026-08-09T12:00:00+00:00",
        "received_at": "2026-08-09T12:00:01+00:00",
        "book_received_at": None,
        "book_latency_ms": 1000,
        "fee_request_started_at": None,
        "fee_received_at": None,
        "fee_latency_ms": None,
        "reviewed_lock_event_id": None,
        "reviewed_milestone_id": None,
        "reviewed_event_start_at": None,
        "reviewed_event_end_at": None,
        "reviewed_lock_selected_at": None,
        "venue_timestamp": None,
        "timestamp_provenance": "local_request_receipt",
    }
    assert system.store.replay_book_state(event["state_hash"])["yes"]["bids"] == []
    assert system.store.replay_fee_schedule(event["fee_hash"])["venue"] == "polymarket"


@pytest.mark.asyncio
async def test_worker_persists_independent_book_and_fee_read_timing(tmp_path):
    """Replay retains real fee provenance and later combined evidence receipt."""
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(tmp_path / "db"))
    worker = PlatformOpportunityWorker(system)
    book_request = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    book_receipt = book_request.replace(second=1)
    fee_request = book_request.replace(second=2)
    fee_receipt = book_request.replace(second=4)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, fee_receipt, "test")

    await worker.start()
    assert worker.submit_book(
        "polymarket:timed-evidence",
        OrderBook(market_id="timed-evidence"),
        observed_at=fee_receipt,
        request_started_at=book_request,
        received_at=fee_receipt,
        book_received_at=book_receipt,
        fee_request_started_at=fee_request,
        fee_received_at=fee_receipt,
        fee_schedule=schedule,
    )
    await worker.stop()

    [event] = system.store.replay_observation_events(cohort_id=system.cohort_id)
    assert event["received_at"] == "2026-08-09T12:00:04+00:00"
    assert event["book_received_at"] == "2026-08-09T12:00:01+00:00"
    assert event["book_latency_ms"] == 1000
    assert event["fee_request_started_at"] == "2026-08-09T12:00:02+00:00"
    assert event["fee_received_at"] == "2026-08-09T12:00:04+00:00"
    assert event["fee_latency_ms"] == 2000
    fee = system.store.replay_fee_schedule(event["fee_hash"])
    assert fee["fetched_at"] == "2026-08-09T12:00:04+00:00"


@pytest.mark.asyncio
async def test_worker_scores_only_the_persisted_bounded_replay_book(tmp_path):
    """Raw depth beyond the retained 50 levels cannot influence decisions."""
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(tmp_path / "db"))
    worker = PlatformOpportunityWorker(system)
    observed_at = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    schedule = VenueFeeSchedule("polymarket", "none", 0, 1, 0, observed_at, "test")
    # The final ask is real raw depth but lies beyond the persisted replay cap.
    # Were scoring to retain the adapter object, entry/exit walking could see it.
    yes = TokenOrderBook(TokenType.YES)
    yes.bids = OrderBookSide([PriceLevel(0.49, 100)])
    yes.asks = OrderBookSide(
        [PriceLevel(0.51 + index * 0.001, 1) for index in range(51)]
    )
    no = TokenOrderBook(TokenType.NO)
    no.bids = OrderBookSide([PriceLevel(0.49, 100)])
    no.asks = OrderBookSide([PriceLevel(0.51, 100)])
    raw_book = OrderBook(market_id="bounded", yes=yes, no=no)

    await worker.start()
    assert worker.submit_book(
        "polymarket:bounded",
        raw_book,
        observed_at=observed_at,
        request_started_at=observed_at,
        received_at=observed_at,
        fee_schedule=schedule,
    )
    await worker.stop()

    scored_book, _ = system._latest_books["polymarket:bounded"]
    assert scored_book is not raw_book
    assert len(raw_book.yes.asks.levels) == 51
    assert len(scored_book.yes.asks.levels) == 50
    assert [price for price, _ in system._levels(scored_book, "yes", entry=True)] == [
        0.51 + index * 0.001 for index in range(50)
    ]


def test_dashboard_exposes_durable_observation_time_bounds_and_provenance(tmp_path):
    """Local request/receipt timing remains visible after many observations."""
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(tmp_path / "db"))
    first = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    system.record_successful_observation(
        "polymarket:timed-book",
        observed_at=first.replace(second=1),
        request_started_at=first,
        received_at=first.replace(second=1),
    )
    system.record_successful_observation(
        "polymarket:timed-book",
        observed_at=first.replace(minute=1, second=1),
        request_started_at=first.replace(minute=1),
        received_at=first.replace(minute=1, second=1),
    )

    evidence = system.dashboard_summary()["observation_evidence"]

    assert evidence == {
        "first_durable_evidence_at": "2026-08-09T12:00:01+00:00",
        "last_durable_evidence_at": "2026-08-09T12:01:01+00:00",
        "event_count": 2,
        "timestamp_source": "local_request_receipt",
    }


def test_observation_failures_are_reason_coded_and_survive_restart(tmp_path):
    path = tmp_path / "opportunities.db"
    observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    system._sampled_contract_ids = {"polymarket:target"}
    system.record_observation_failure(
        "polymarket:target",
        reason_code="book_read_failed",
        failed_at=observed_at,
    )
    system.record_observation_failure(
        "polymarket:target",
        reason_code="fee_metadata_failed",
        failed_at=observed_at,
    )
    system.record_observation_failure(
        "polymarket:target",
        reason_code="book_read_failed",
        failed_at=observed_at,
    )

    failures = system.dashboard_summary()["observation_failures"]
    assert failures["polymarket:target"]["book_read_failed"]["failure_count"] == 2
    assert failures["polymarket:target"]["fee_metadata_failed"]["failure_count"] == 1
    assert system.store.observation_telemetry(cohort_id=system.cohort_id) == {}
    system.store.close()

    resumed = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    resumed._sampled_contract_ids = {"polymarket:target"}
    assert resumed.dashboard_summary()["observation_failures"] == failures
