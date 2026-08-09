from datetime import datetime, timezone

import pytest

from kalshi_client.models import KalshiMarket, KalshiMilestone

from core.platform_opportunities import (
    PoliticalWatchPolicy,
    PlatformOpportunitySystem,
    VenueFeeSchedule,
)
from core.platform_opportunity_runtime import PlatformOpportunityWorker
from polymarket_client.models import OrderBook
from utils.platform_opportunity_store import PlatformOpportunityStore


@pytest.mark.asyncio
async def test_worker_drains_queued_observations_before_shutdown():
    class System:
        def __init__(self):
            self.processed = []

        def set_fee_schedule(self, contract_id, schedule):
            pass

        def observe_book(self, contract_id, book, *, observed_at):
            self.processed.append(contract_id)

        def dashboard_summary(self):
            return {}

    system = System()
    worker = PlatformOpportunityWorker(system, queue_capacity=10)
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

    assert system.processed == ["polymarket:0", "polymarket:1", "polymarket:2"]
    assert worker.processed == 3


@pytest.mark.asyncio
async def test_worker_persists_exact_kalshi_milestone_window_in_political_lock(tmp_path):
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
async def test_worker_persists_successful_empty_depth_observation_across_restart(tmp_path):
    """An assigned contract is not an observation until its book read completes."""
    path = tmp_path / "opportunities.db"
    system = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    worker = PlatformOpportunityWorker(system)
    observed_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    schedule = VenueFeeSchedule(
        "polymarket", "none", 0, 1, 0, observed_at, "test"
    )

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
    schedule = VenueFeeSchedule(
        "polymarket", "none", 0, 1, 0, received_at, "test"
    )

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
    schedule = VenueFeeSchedule(
        "polymarket", "none", 0, 1, 0, received_at, "test"
    )

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
        "venue_timestamp": None,
        "timestamp_provenance": "local_request_receipt",
    }
    assert system.store.replay_book_state(event["state_hash"])["yes"]["bids"] == []
    assert system.store.replay_fee_schedule(event["fee_hash"])["venue"] == "polymarket"


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
