from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.cross_platform_arb import MarketPair
from core.event_calendar import ScheduledEvent
from core.event_contracts import (
    EventContractDiscovery,
    EventContractDiscoveryResult,
    EventCoverage,
    EventPairLink,
)
from utils.paper_trade_store import PaperTradeStore


@pytest.mark.asyncio
async def test_event_discovery_reserves_verification_for_matching_family_and_period():
    event = ScheduledEvent(
        event_id="bls:cpi-july-2026",
        source_id="bls",
        external_id="cpi-july-2026",
        title="Consumer Price Index for July 2026",
        description="Scheduled national CPI release.",
        event_type="cpi",
        scheduled_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        reference_period="July 2026",
        status="scheduled",
        source_url="https://www.bls.gov/schedule/news_release/bls.ics",
    )
    july_poly = SimpleNamespace(
        market_id="poly-july-core-cpi",
        question="Will core CPI inflation for July 2026 be above 0.3%?",
        description="Resolves from the BLS CPI release.",
        event_title="July 2026 core CPI",
    )
    august_poly = SimpleNamespace(
        market_id="poly-august-core-cpi",
        question="Will core CPI inflation for August 2026 be above 0.3%?",
        description="Resolves from the BLS CPI release.",
        event_title="August 2026 core CPI",
    )
    politics_poly = SimpleNamespace(
        market_id="poly-election",
        question="Who will win the 2028 election?",
        description="",
        event_title="",
    )
    july_kalshi = SimpleNamespace(
        ticker="KX-CORECPI-26JUL-T03",
        matching_text="Will core CPI for July 2026 be above 0.3%?",
        title="Core CPI July 2026",
        subtitle="",
        event_title="Core CPI July 2026",
        rules_primary="Resolves from BLS CPI.",
        rules_secondary="",
    )
    fed_kalshi = SimpleNamespace(
        ticker="KXFED-26SEP",
        matching_text="Will the Fed cut rates in September 2026?",
        title="Fed decision",
        subtitle="",
        event_title="September Fed decision",
        rules_primary="",
        rules_secondary="",
    )
    pair = MarketPair(
        polymarket_id=july_poly.market_id,
        kalshi_ticker=july_kalshi.ticker,
        polymarket_question=july_poly.question,
        kalshi_title=july_kalshi.matching_text,
        similarity_score=0.97,
        semantic_relation="equivalent",
        verification_confidence=0.99,
        auto_approved=True,
    )

    class Matcher:
        def __init__(self):
            self.calls = []

        async def find_matches(self, polymarket, kalshi):
            self.calls.append((list(polymarket), list(kalshi)))
            return [pair]

    matcher = Matcher()
    discovery = EventContractDiscovery(matcher=matcher)

    result = await discovery.discover(
        events=[event],
        polymarket_markets=[july_poly, august_poly, politics_poly],
        kalshi_markets=[july_kalshi, fed_kalshi],
    )

    assert [[market.market_id for market in call[0]] for call in matcher.calls] == [
        ["poly-july-core-cpi"]
    ]
    assert [[market.ticker for market in call[1]] for call in matcher.calls] == [
        ["KX-CORECPI-26JUL-T03"]
    ]
    assert result.pairs == (pair,)
    assert len(result.links) == 1
    assert result.links[0].event_id == event.event_id
    assert result.links[0].pair_id == pair.pair_id
    assert result.coverage[0].status == "verified_pairs"


@pytest.mark.asyncio
async def test_event_discovery_rejects_low_confidence_equivalent_match():
    event = ScheduledEvent(
        event_id="bls:cpi-july-2026",
        source_id="bls",
        external_id="cpi-july-2026",
        title="Consumer Price Index for July 2026",
        description="",
        event_type="cpi",
        scheduled_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        reference_period="July 2026",
        status="scheduled",
        source_url="https://www.bls.gov/schedule/news_release/bls.ics",
    )
    poly = SimpleNamespace(
        market_id="poly-cpi",
        question="Will CPI for July 2026 exceed 3%?",
        event_title="July 2026 CPI",
        description="",
        resolution_source="BLS",
    )
    kalshi = SimpleNamespace(
        ticker="kx-cpi",
        matching_text="Will CPI for July 2026 exceed 3%?",
        title="July 2026 CPI",
        subtitle="",
        event_title="",
        rules_primary="BLS",
        rules_secondary="",
        settlement_source="BLS",
    )
    low_confidence = MarketPair(
        polymarket_id=poly.market_id,
        kalshi_ticker=kalshi.ticker,
        polymarket_question=poly.question,
        kalshi_title=kalshi.title,
        similarity_score=0.97,
        semantic_relation="equivalent",
        verification_confidence=0.70,
        auto_approved=False,
    )

    class Matcher:
        async def find_matches(self, *_):
            return [low_confidence]

    result = await EventContractDiscovery(matcher=Matcher()).discover(
        events=[event],
        polymarket_markets=[poly],
        kalshi_markets=[kalshi],
    )

    assert result.pairs == ()
    assert result.links == ()
    assert result.coverage[0].status == "no_verified_pairs"


@pytest.mark.asyncio
async def test_fomc_event_requires_contracts_for_the_scheduled_meeting_month():
    event = ScheduledEvent(
        event_id="federal_reserve:fomc-september-2026",
        source_id="federal_reserve",
        external_id="fomc-september-2026",
        title="FOMC Meeting",
        description="",
        event_type="fomc",
        scheduled_at=datetime(2026, 9, 16, 18, tzinfo=timezone.utc),
        reference_period="",
        status="scheduled",
        source_url="https://www.federalreserve.gov/json/calendar.json",
    )
    september_poly = SimpleNamespace(
        market_id="poly-sep",
        question="Will the Fed cut rates in September 2026?",
        event_title="September 2026 FOMC",
        description="",
        resolution_source="Federal Reserve",
    )
    december_poly = SimpleNamespace(
        market_id="poly-dec",
        question="Will the Fed cut rates in December 2026?",
        event_title="December 2026 FOMC",
        description="",
        resolution_source="Federal Reserve",
    )
    september_kalshi = SimpleNamespace(
        ticker="KXFED-26SEP",
        matching_text="Will the Fed cut rates in September 2026?",
        title="September Fed decision",
        subtitle="",
        event_title="September 2026 FOMC",
        rules_primary="",
        rules_secondary="",
        settlement_source="Federal Reserve",
    )

    class Matcher:
        def __init__(self):
            self.polymarket = []

        async def find_matches(self, polymarket, _kalshi):
            self.polymarket = list(polymarket)
            return []

    matcher = Matcher()
    await EventContractDiscovery(matcher=matcher).discover(
        events=[event],
        polymarket_markets=[september_poly, december_poly],
        kalshi_markets=[september_kalshi],
    )

    assert [market.market_id for market in matcher.polymarket] == ["poly-sep"]


@pytest.mark.asyncio
async def test_event_discovery_enforces_aggregate_matcher_call_budget():
    events = [
        ScheduledEvent(
            event_id=f"bls:cpi-{month.casefold()}-2026",
            source_id="bls",
            external_id=f"cpi-{month.casefold()}-2026",
            title=f"Consumer Price Index for {month} 2026",
            description="",
            event_type="cpi",
            scheduled_at=datetime(2026, index, 12, 12, 30, tzinfo=timezone.utc),
            reference_period=f"{month} 2026",
            status="scheduled",
            source_url="https://www.bls.gov/schedule/news_release/bls.ics",
        )
        for index, month in ((8, "July"), (9, "August"))
    ]
    polymarket = [
        SimpleNamespace(
            market_id=f"poly-{month.casefold()}",
            question=f"Will CPI for {month} 2026 exceed 3%?",
            event_title="",
            description="",
            resolution_source="BLS",
        )
        for month in ("July", "August")
    ]
    kalshi = [
        SimpleNamespace(
            ticker=f"kx-{month.casefold()}",
            matching_text=f"Will CPI for {month} 2026 exceed 3%?",
            title=f"{month} 2026 CPI",
            subtitle="",
            event_title="",
            rules_primary="BLS",
            rules_secondary="",
            settlement_source="BLS",
        )
        for month in ("July", "August")
    ]

    class Matcher:
        def __init__(self):
            self.calls = 0

        async def find_matches(self, *_):
            self.calls += 1
            return []

    matcher = Matcher()
    result = await EventContractDiscovery(
        matcher=matcher,
        max_matcher_calls_per_cycle=1,
    ).discover(
        events=events,
        polymarket_markets=polymarket,
        kalshi_markets=kalshi,
    )

    assert matcher.calls == 1
    assert [row.status for row in result.coverage] == [
        "no_verified_pairs",
        "budget_exhausted",
    ]


def test_store_persists_verified_event_pair_link_for_restart(tmp_path):
    event = ScheduledEvent(
        event_id="bls:cpi-july-2026",
        source_id="bls",
        external_id="cpi-july-2026",
        title="Consumer Price Index for July 2026",
        description="",
        event_type="cpi",
        scheduled_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        reference_period="July 2026",
        status="scheduled",
        source_url="https://www.bls.gov/schedule/news_release/bls.ics",
    )
    pair = MarketPair(
        polymarket_id="poly-cpi",
        kalshi_ticker="kx-cpi",
        polymarket_question="Core CPI above 0.3%?",
        kalshi_title="Core CPI above 0.3%?",
        similarity_score=0.97,
        semantic_relation="equivalent",
        verification_confidence=0.99,
        verification_reasons=("same metric", "same threshold"),
        auto_approved=True,
    )
    result = EventContractDiscoveryResult(
        pairs=(pair,),
        links=(
            EventPairLink(
                event_id=event.event_id,
                event_type=event.event_type,
                scheduled_at=event.scheduled_at,
                pair=pair,
            ),
        ),
        coverage=(
            EventCoverage(
                event_id=event.event_id,
                event_type=event.event_type,
                status="verified_pairs",
                polymarket_candidates=1,
                kalshi_candidates=1,
                verified_pairs=1,
            ),
        ),
    )
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        # Calendar persistence owns the event identity referenced by pair links.
        from datetime import timedelta
        from core.event_calendar import CalendarSnapshot, CalendarSourceHealth

        now = datetime(2026, 8, 8, 15, tzinfo=timezone.utc)
        store.record_event_calendar_snapshot(
            CalendarSnapshot(
                status="complete",
                generated_at=now,
                window_start=now,
                window_end=now + timedelta(days=7),
                events=(event,),
                sources=(
                    CalendarSourceHealth(
                        source_id="bls",
                        source_url=event.source_url,
                        status="fresh",
                        checked_at=now,
                        last_success_at=now,
                        event_count=1,
                        error="",
                    ),
                ),
            )
        )
        store.record_event_contract_discovery(result, observed_at=now)
        rows = store.active_event_pair_links(
            start=now,
            end=now + timedelta(days=7),
        )
    finally:
        store.close()

    assert len(rows) == 1
    assert rows[0]["event_id"] == event.event_id
    assert rows[0]["pair_id"] == pair.pair_id
    assert rows[0]["semantic_relation"] == "equivalent"
    assert rows[0]["verification_reasons"] == ["same metric", "same threshold"]
