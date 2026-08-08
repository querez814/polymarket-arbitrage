from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.cross_platform_arb import MarketPair
from core.event_contracts import EventPairLink
from core.event_lane import EventLanePolicy, EventLaneScheduler
from utils.paper_trade_store import PaperTradeStore


def test_event_lane_moves_pair_through_clock_driven_states():
    event_at = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    now = [event_at - timedelta(days=4)]
    pair = MarketPair(
        polymarket_id="poly-cpi",
        kalshi_ticker="kx-cpi",
        polymarket_question="Core CPI above 0.3%?",
        kalshi_title="Core CPI above 0.3%?",
        similarity_score=0.98,
    )
    link = EventPairLink(
        event_id="bls:cpi-july-2026",
        event_type="cpi",
        scheduled_at=event_at,
        pair=pair,
    )
    scheduler = EventLaneScheduler(
        policy=EventLanePolicy(),
        clock=lambda: now[0],
    )

    expected = [
        (event_at - timedelta(days=4), "scheduled", 300.0),
        (event_at - timedelta(hours=12), "warm", 60.0),
        (event_at - timedelta(minutes=30), "hot", 2.0),
        (event_at - timedelta(minutes=3), "burst", 1.0),
        (event_at + timedelta(minutes=10), "burst", 1.0),
        (event_at + timedelta(hours=2), "cooldown", 10.0),
    ]
    observed = []
    for moment, state, interval in expected:
        now[0] = moment
        schedule = scheduler.schedule([link])
        observed.append((schedule.pairs[0].state, schedule.pairs[0].interval_seconds))

    assert observed == [(state, interval) for _, state, interval in expected]

    now[0] = event_at + timedelta(days=2)
    assert scheduler.schedule([link]).pairs == ()


def test_store_records_lane_transitions_without_per_scan_duplicates(tmp_path):
    from core.event_calendar import (
        CalendarSnapshot,
        CalendarSourceHealth,
        ScheduledEvent,
    )

    event_at = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    now = [event_at - timedelta(minutes=30)]
    pair = MarketPair(
        polymarket_id="poly-cpi",
        kalshi_ticker="kx-cpi",
        polymarket_question="Core CPI above 0.3%?",
        kalshi_title="Core CPI above 0.3%?",
        similarity_score=0.98,
    )
    link = EventPairLink(
        event_id="bls:cpi-july-2026",
        event_type="cpi",
        scheduled_at=event_at,
        pair=pair,
    )
    scheduler = EventLaneScheduler(
        policy=EventLanePolicy(),
        clock=lambda: now[0],
    )
    event = ScheduledEvent(
        event_id=link.event_id,
        source_id="bls",
        external_id="cpi-july-2026",
        title="Consumer Price Index for July 2026",
        description="",
        event_type="cpi",
        scheduled_at=event_at,
        reference_period="July 2026",
        status="scheduled",
        source_url="https://www.bls.gov/schedule/news_release/bls.ics",
    )
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        store.start_run(starting_equity=1000, pnl_source="projected_locked_paper")
        store.record_event_calendar_snapshot(
            CalendarSnapshot(
                status="complete",
                generated_at=now[0],
                window_start=now[0],
                window_end=event_at + timedelta(days=1),
                events=(event,),
                sources=(
                    CalendarSourceHealth(
                        source_id="bls",
                        source_url=event.source_url,
                        status="fresh",
                        checked_at=now[0],
                        last_success_at=now[0],
                        event_count=1,
                        error="",
                    ),
                ),
            )
        )
        hot = scheduler.schedule([link])
        store.record_event_lane_snapshot(hot)
        store.record_event_lane_snapshot(hot)
        store.record_event_operational_failure(
            event_id=link.event_id,
            pair_id=link.pair_id,
            lane_state="hot",
            reason_code="paired_snapshot_timeout",
            observed_at=now[0],
        )
        now[0] = event_at - timedelta(minutes=3)
        store.record_event_lane_snapshot(scheduler.schedule([link]))
        transitions = store.recent_event_lane_transitions(limit=10)
        scorecard = store.event_week_scorecard()
    finally:
        store.close()

    assert [row["lane_state"] for row in reversed(transitions)] == ["hot", "burst"]
    assert all(row["event_id"] == link.event_id for row in transitions)
    assert scorecard["operational_failure_count"] == 1
    assert scorecard["operational_failures"] == {"paired_snapshot_timeout": 1}
