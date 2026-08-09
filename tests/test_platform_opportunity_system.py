from datetime import datetime, timedelta, timezone

from kalshi_client.models import KalshiMarket, KalshiMilestone
from polymarket_client.models import (
    Market,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)

from core.platform_opportunities import (
    AcceptancePolicy,
    CatalystReference,
    MonitoringPolicy,
    PoliticalWatchPolicy,
    PlatformOpportunitySystem,
    StructuralRelation,
    VenueFeeSchedule,
)
from utils.platform_opportunity_store import PlatformOpportunityStore

NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)


def _poly(
    market_id: str,
    question: str,
    *,
    event_id: str = "event-1",
    end_date: datetime | None = None,
    liquidity: float = 5_000,
    volume: float = 2_000,
) -> Market:
    return Market(
        market_id=market_id,
        condition_id=f"condition-{market_id}",
        question=question,
        event_id=event_id,
        event_title="August CPI release",
        end_date=end_date,
        liquidity=liquidity,
        volume_24h=volume,
        active=True,
        closed=False,
        resolution_source="BLS",
    )


def _kalshi(
    ticker: str,
    title: str,
    *,
    event_ticker: str = "KXCPI-26AUG",
    close_time: datetime | None = None,
    volume: int = 1_000,
    open_interest: int = 500,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker="KXCPI",
        title=title,
        event_title="August CPI release",
        close_time=close_time,
        volume=volume,
        open_interest=open_interest,
        status="open",
        settlement_source="BLS",
    )


def _book(market_id: str, *, bid: float, ask: float, bid_size: float, ask_size: float):
    yes = TokenOrderBook(TokenType.YES)
    yes.bids = OrderBookSide([PriceLevel(bid, bid_size)])
    yes.asks = OrderBookSide([PriceLevel(ask, ask_size)])
    no = TokenOrderBook(TokenType.NO)
    no.bids = OrderBookSide([PriceLevel(1 - ask, ask_size)])
    no.asks = OrderBookSide([PriceLevel(1 - bid, bid_size)])
    return OrderBook(market_id=market_id, yes=yes, no=no, timestamp=NOW)


def _zero_fee(venue: str = "polymarket") -> VenueFeeSchedule:
    return VenueFeeSchedule(
        venue=venue,
        fee_type="none",
        rate=0,
        exponent=1,
        multiplier=0,
        observed_at=NOW,
        source="test_authoritative_zero_fee",
    )


def test_catalog_is_platform_first_revisioned_and_hot_lane_is_bounded(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        monitoring_policy=MonitoringPolicy(
            lookahead=timedelta(days=7),
            max_hot_contracts=2,
            min_liquidity=100,
            min_volume=100,
        ),
    )
    close = NOW + timedelta(minutes=30)
    first = system.refresh_catalog(
        polymarket_markets=[
            _poly("p1", "Will CPI be above 3%?", end_date=close),
            _poly("p2", "Will CPI be above 4%?", end_date=close),
        ],
        kalshi_markets=[_kalshi("k1", "CPI above 3%", close_time=close)],
        observed_at=NOW,
    )
    second = system.refresh_catalog(
        polymarket_markets=[
            _poly("p1", "Will CPI be above 3%?", end_date=close),
            _poly("p2", "Will CPI be above 4%?", end_date=close),
        ],
        kalshi_markets=[_kalshi("k1", "CPI above 3%", close_time=close)],
        observed_at=NOW + timedelta(minutes=1),
    )

    assert first.catalog_contracts == 3
    assert first.revisions_written == 3
    assert second.revisions_written == 0
    assert len(second.monitoring.hot) == 2
    assert len(second.monitoring.budget_excluded) == 1
    assert second.monitoring.budget_excluded[0].reason == "hot_lane_capacity"
    assert store.catalog_counts() == {"current": 3, "revisions": 3}

    retired = system.refresh_catalog(
        polymarket_markets=[
            _poly("p1", "Will CPI be above 3%?", end_date=close),
        ],
        kalshi_markets=[],
        observed_at=NOW + timedelta(minutes=2),
    )
    assert retired.catalog_contracts == 1
    assert store.catalog_counts() == {"current": 1, "revisions": 3}


def test_partial_catalog_replaces_ordinary_eligibility_without_losing_history(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        monitoring_policy=MonitoringPolicy(
            lookahead=timedelta(days=7),
            max_hot_contracts=2,
            min_liquidity=100,
            min_volume=100,
        ),
    )
    close = NOW + timedelta(minutes=30)
    system.refresh_catalog(
        polymarket_markets=[
            _poly("old-high-volume", "Will CPI be above 3%?", end_date=close),
            _poly("old-low-volume", "Will CPI be above 4%?", end_date=close),
        ],
        kalshi_markets=[],
        observed_at=NOW,
    )

    partial = system.refresh_catalog(
        polymarket_markets=[
            _poly("fresh", "Will CPI be above 5%?", end_date=close, volume=101),
        ],
        kalshi_markets=[],
        snapshot_complete=False,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert partial.catalog_contracts == 1
    assert [item.contract_id for item in partial.monitoring.hot] == ["polymarket:fresh"]
    assert store.catalog_counts() == {"current": 1, "revisions": 3}


def test_partial_catalog_rehydrates_active_political_lock_after_restart(tmp_path):
    path = tmp_path / "opportunities.db"
    occurrence = NOW + timedelta(hours=2)
    original = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path),
        political_watch_policy=PoliticalWatchPolicy(max_events=1),
    )
    original.refresh_catalog(
        polymarket_markets=[
            _poly(
                "election",
                "Will the President win the election?",
                event_id="election-2026",
                end_date=occurrence,
            )
        ],
        kalshi_markets=[],
        observed_at=NOW,
    )
    original.store.close()

    resumed_store = PlatformOpportunityStore(path)
    resumed = PlatformOpportunitySystem(
        store=resumed_store,
        political_watch_policy=PoliticalWatchPolicy(max_events=1),
    )
    partial = resumed.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[],
        snapshot_complete=False,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert partial.catalog_contracts == 1
    assert [item.contract_id for item in partial.monitoring.warm] == [
        "polymarket:election"
    ]
    assert resumed_store.catalog_counts() == {"current": 0, "revisions": 1}


def test_selected_political_event_survives_refresh_volume_displacement_until_cooldown(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=2,
        cooldown_after=timedelta(hours=2),
    )
    system = PlatformOpportunitySystem(store=store, political_watch_policy=policy)
    close = NOW + timedelta(minutes=30)

    first = system.refresh_catalog(
        polymarket_markets=[
            _poly(
                "election",
                "Will the President win the election?",
                event_id="election-2026",
                end_date=close,
                volume=100,
            ),
            _poly(
                "approval",
                "Will presidential approval exceed 50%?",
                event_id="approval-2026",
                end_date=close,
                volume=50,
            ),
        ],
        kalshi_markets=[],
        observed_at=NOW,
    )
    assert [item.contract_id for item in first.monitoring.hot] == ["polymarket:election"]

    displaced = system.refresh_catalog(
        polymarket_markets=[
            _poly(
                "election",
                "Will the President win the election?",
                event_id="election-2026",
                end_date=close,
                volume=1,
            ),
            _poly(
                "approval",
                "Will presidential approval exceed 50%?",
                event_id="approval-2026",
                end_date=close,
                volume=100_000,
            ),
            _poly(
                "mve",
                "MVE: President election and BTC above $100k",
                event_id="combo",
                end_date=close,
                volume=1_000_000,
            ),
        ],
        kalshi_markets=[],
        observed_at=NOW + timedelta(minutes=1),
    )

    assert [item.contract_id for item in displaced.monitoring.hot] == ["polymarket:election"]
    locks = store.active_political_event_locks(now=NOW + timedelta(minutes=1))
    assert [lock["event_id"] for lock in locks] == ["election-2026"]


def test_locked_political_event_is_sampled_at_warm_hot_and_cooldown_cadences(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=1,
        cooldown_after=timedelta(hours=2),
    )
    system = PlatformOpportunitySystem(store=store, political_watch_policy=policy)
    occurrence = NOW + timedelta(hours=2)
    market = _poly(
        "election",
        "Will the President win the election?",
        event_id="election-2026",
        end_date=occurrence,
    )

    warm = system.refresh_catalog(
        polymarket_markets=[market], kalshi_markets=[], observed_at=NOW
    ).monitoring
    assert [
        (item.reason, item.cadence, item.interval_seconds) for item in warm.warm
    ] == [("political_event_lock", "warm", 60.0)]

    hot = system.refresh_catalog(
        polymarket_markets=[market],
        kalshi_markets=[],
        observed_at=occurrence - timedelta(minutes=30),
    ).monitoring
    assert [
        (item.reason, item.cadence, item.interval_seconds) for item in hot.hot
    ] == [("political_event_lock", "hot", 2.0)]

    cooldown = system.refresh_catalog(
        polymarket_markets=[market],
        kalshi_markets=[],
        observed_at=occurrence + timedelta(minutes=1),
    ).monitoring
    assert [
        (item.reason, item.cadence, item.interval_seconds)
        for item in cooldown.hot
    ] == [("political_event_lock", "cooldown", 10.0)]

    dashboard = system.dashboard_summary()
    assert dashboard["political_event_locks"][0]["event_id"] == "election-2026"
    assert dashboard["political_event_locks"][0]["contract_ids"] == [
        "polymarket:election"
    ]
    assert dashboard["political_event_locks"][0]["sampled_contract_ids"] == [
        "polymarket:election"
    ]
    assert dashboard["sampled_contract_ids"] == ["polymarket:election"]
    assert dashboard["research_pnl"] == {
        "authority": "shadow_research_only",
        "actual_exit": {"marks": 0, "scored_marks": 0, "capacity_pnl": 0.0},
        "horizons": {},
    }


def test_fuzzy_calendar_title_does_not_schedule_unrelated_contract(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    close = NOW + timedelta(days=10)
    release = NOW + timedelta(minutes=30)

    refresh = system.refresh_catalog(
        polymarket_markets=[
            _poly(
                "bnb-price",
                "Will BNB price be above $700 in 2026?",
                event_id="crypto-price-2026",
                end_date=close,
            )
        ],
        kalshi_markets=[],
        catalyst_references=[
            CatalystReference(
                reference_id="bls-ppi-2026-08",
                title="PPI price index release 2026",
                scheduled_at=release,
                source="https://www.bls.gov/schedule/",
                authoritative=True,
            )
        ],
        observed_at=NOW,
    )

    assert refresh.catalog_contracts == 1
    contract = system.contracts[0]
    assert contract.catalyst_at == close
    assert contract.catalyst_evidence == "exact_venue_metadata"
    assert contract.catalyst_sources == ("polymarket.end_date",)
    assert refresh.monitoring.hot == ()


def test_kalshi_expected_expiration_drives_catalyst_not_later_legal_close(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    expected = NOW + timedelta(hours=2)
    legal_close = NOW + timedelta(days=30)

    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            KalshiMarket(
                ticker="KX-CPI",
                event_ticker="KXE-CPI",
                series_ticker="KXS-CPI",
                title="CPI release",
                expiration_time=expected,
                close_time=legal_close,
                volume=1000,
                open_interest=500,
            )
        ],
        observed_at=NOW,
    )

    contract = system.contracts[0]
    assert contract.close_time == legal_close
    assert contract.catalyst_at == expected
    assert contract.catalyst_sources == ("kalshi.expiration_time",)


def test_kalshi_exact_milestone_sets_occurrence_not_settlement_deadline(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(max_events=1),
    )
    occurrence = NOW + timedelta(hours=2)
    settlement_deadline = NOW + timedelta(days=30)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXTRUMPMENTION-26AUG10-A",
                "Will Trump mention immigration?",
                event_ticker="KXTRUMPMENTION-26AUG10",
                close_time=settlement_deadline,
            )
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="milestone-mention",
                title="Trump remarks",
                category="Politics",
                milestone_type="political_speech",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=45),
                related_event_tickers=("KXTRUMPMENTION-26AUG10",),
                primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
                source_id="kalshi",
            )
        ],
        observed_at=NOW,
    )

    contract = system.contracts[0]
    assert contract.close_time == settlement_deadline
    assert contract.occurrence_at == occurrence
    assert contract.occurrence_evidence == "exact_venue_milestone"
    assert contract.occurrence_sources == (
        "kalshi.milestone:milestone-mention:start_date",
    )
    assert contract.catalyst_at == occurrence
    assert [lock["event_id"] for lock in system.store.active_political_event_locks(now=NOW)] == [
        "KXTRUMPMENTION-26AUG10"
    ]


def test_kalshi_ambiguous_distinct_primary_milestones_fail_closed(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    first = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXSPEECH-26-A",
                "Will the President mention immigration?",
                event_ticker="KXSPEECH-26",
                close_time=NOW + timedelta(days=30),
            )
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="speech-first",
                title="President remarks",
                category="Politics",
                milestone_type="political_speech",
                start_time=first,
                end_time=first + timedelta(minutes=30),
                related_event_tickers=("KXSPEECH-26",),
                primary_event_tickers=("KXSPEECH-26",),
            ),
            KalshiMilestone(
                milestone_id="speech-rescheduled",
                title="President remarks",
                category="Politics",
                milestone_type="political_speech",
                start_time=first + timedelta(hours=1),
                end_time=first + timedelta(hours=1, minutes=30),
                related_event_tickers=("KXSPEECH-26",),
                primary_event_tickers=("KXSPEECH-26",),
            ),
        ],
        observed_at=NOW,
    )

    contract = system.contracts[0]
    assert contract.occurrence_at is None
    assert contract.occurrence_evidence == "unknown"


def test_kalshi_duplicate_primary_milestone_windows_deduplicate(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    occurrence = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXSPEECH-26-A",
                "Will the President mention immigration?",
                event_ticker="KXSPEECH-26",
            )
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="speech-z",
                title="President remarks",
                category="Politics",
                milestone_type="political_speech",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=30),
                related_event_tickers=("KXSPEECH-26",),
                primary_event_tickers=("KXSPEECH-26",),
            ),
            KalshiMilestone(
                milestone_id="speech-a",
                title="President remarks duplicate",
                category="Politics",
                milestone_type="political_speech",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=30),
                related_event_tickers=("KXSPEECH-26",),
                primary_event_tickers=("KXSPEECH-26",),
            ),
        ],
        observed_at=NOW,
    )

    contract = system.contracts[0]
    assert contract.occurrence_at == occurrence
    assert contract.occurrence_sources == (
        "kalshi.milestone:speech-a:start_date",
    )


def test_kalshi_unrelated_milestone_cannot_schedule_contract(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    expiration = NOW + timedelta(days=7)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXBNB-26-A",
                "Will BNB be above $700?",
                event_ticker="KXBNB-26",
                close_time=expiration,
            )
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="ppi",
                title="PPI release",
                category="Economics",
                milestone_type="economic_release",
                start_time=NOW + timedelta(minutes=30),
                end_time=None,
                related_event_tickers=("KXPPI-26AUG",),
                primary_event_tickers=("KXPPI-26AUG",),
            )
        ],
        observed_at=NOW,
    )

    contract = system.contracts[0]
    assert contract.occurrence_at is None
    assert contract.catalyst_at == expiration


def test_only_machine_checkable_structure_authorizes_relative_value(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[
            _poly("p3", "Will August CPI be above 3%?", end_date=close),
            _poly("p4", "Will August CPI be above 4%?", end_date=close),
            _poly("vague", "Will inflation surprise markets?", end_date=close),
        ],
        kalshi_markets=[],
        observed_at=NOW,
    )

    relations = system.discover_structural_relations(observed_at=NOW)

    assert len(relations) == 1
    relation = relations[0]
    assert relation.relation_type == "ordered_thresholds"
    assert relation.invariant == "P(above 4) <= P(above 3)"
    assert set(relation.contract_ids) == {"polymarket:p3", "polymarket:p4"}
    assert "vague" not in relation.relation_id


def test_structural_relation_is_retained_when_sampling_capacity_excludes_a_leg(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        monitoring_policy=MonitoringPolicy(
            lookahead=timedelta(days=7),
            max_hot_contracts=1,
            min_liquidity=100,
            min_volume=100,
        ),
    )
    close = NOW + timedelta(minutes=30)
    refresh = system.refresh_catalog(
        polymarket_markets=[
            _poly("p3", "Will August CPI be above 3%?", end_date=close),
            _poly("p4", "Will August CPI be above 4%?", end_date=close),
        ],
        kalshi_markets=[],
        observed_at=NOW,
    )

    assert len(refresh.monitoring.hot) == 1
    assert len(refresh.monitoring.budget_excluded) == 1
    assert refresh.monitoring.budget_excluded[0].reason == (
        "structural_relation_sampling_capacity"
    )
    relations = system.discover_structural_relations(observed_at=NOW)
    assert len(relations) == 1
    assert set(relations[0].contract_ids) == {"polymarket:p3", "polymarket:p4"}


def test_directional_intent_is_immutable_and_scored_only_from_later_books(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())

    assert (
        system.observe_book(
            "polymarket:p1",
            _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
            observed_at=NOW,
        ).intents
        == ()
    )
    emitted = system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert len(emitted.intents) == 1
    intent = emitted.intents[0]
    assert intent.lane == "directional_reaction"
    assert intent.direction == "yes"
    assert intent.entry_price == 0.53
    assert intent.model_version == "microstructure-baseline-v1"
    assert store.mark_rows(lane="directional_reaction", horizon_seconds=30) == []

    scored = system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.57, ask=0.59, bid_size=200, ask_size=100),
        observed_at=NOW + timedelta(seconds=36),
    )

    marks = [mark for mark in scored.marks if mark.horizon_seconds == 30]
    assert {mark.capacity_fraction for mark in marks} == {0.05, 0.1, 0.2, 1.0}
    assert all(mark.net_return is not None and mark.net_return > 0 for mark in marks)
    assert len(store.intent_rows(lane="directional_reaction")) == 1

    exited = system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.54, ask=0.56, bid_size=25, ask_size=250),
        observed_at=NOW + timedelta(seconds=40),
    )
    strategy_marks = [mark for mark in exited.marks if mark.horizon_seconds == -1]
    assert strategy_marks
    assert {mark.reason for mark in strategy_marks} == {"signal_reversal"}
    research_pnl = store.research_mark_summary()
    assert research_pnl["authority"] == "shadow_research_only"
    assert research_pnl["actual_exit"]["marks"] == 1
    assert research_pnl["actual_exit"]["scored_marks"] == 1
    assert research_pnl["horizons"]["30"]["marks"] == 1
    assert research_pnl["horizons"]["30"]["scored_marks"] == 1


def test_shadow_intent_fails_closed_without_authoritative_fee_metadata(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    close = NOW + timedelta(minutes=30)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    result = system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert result.intents == ()


def test_zero_momentum_imbalance_does_not_emit_directional_no_intent(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())

    system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    result = system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=20, ask_size=300),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert result.intents == ()
    assert system.store.intent_rows(lane="directional_reaction") == []


def test_directional_intent_uses_signed_composite_not_momentum_alone(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())

    system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    emitted = system.observe_book(
        "polymarket:p1",
        # Mid-price momentum is negative (-0.2¢), but the strongly positive
        # imbalance produces a positive qualifying composite (+0.0127).
        _book("p1", bid=0.508, ask=0.528, bid_size=1_000, ask_size=10),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert len(emitted.intents) == 1
    assert emitted.intents[0].direction == "yes"


def test_zero_exit_capacity_records_insufficient_depth_marks_without_crashing(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())
    system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    emitted = system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=NOW + timedelta(seconds=5),
    )
    assert len(emitted.intents) == 1

    scored = system.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.57, ask=0.59, bid_size=0, ask_size=250),
        observed_at=NOW + timedelta(seconds=36),
    )

    marks = [mark for mark in scored.marks if mark.horizon_seconds == 30]
    assert len(marks) == 4
    assert {mark.reason for mark in marks} == {"insufficient_later_executable_depth"}
    assert all(mark.max_notional == 0 for mark in marks)
    assert all(mark.net_return is None for mark in marks)


def test_relative_value_lane_uses_structural_residual_not_semantic_similarity(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[
            _poly("p3", "Will August CPI be above 3%?", end_date=close),
            _poly("p4", "Will August CPI be above 4%?", end_date=close),
        ],
        kalshi_markets=[],
        observed_at=NOW,
    )
    relation = system.discover_structural_relations(observed_at=NOW)[0]
    system.set_fee_schedule("polymarket:p3", _zero_fee())
    system.set_fee_schedule("polymarket:p4", _zero_fee())

    # Establish the observed structural residual, then compress it sharply.
    system.observe_book(
        "polymarket:p3",
        _book("p3", bid=0.59, ask=0.61, bid_size=200, ask_size=200),
        observed_at=NOW,
    )
    system.observe_book(
        "polymarket:p4",
        _book("p4", bid=0.49, ask=0.51, bid_size=200, ask_size=200),
        observed_at=NOW,
    )
    system.observe_book(
        "polymarket:p3",
        _book("p3", bid=0.60, ask=0.62, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=2),
    )
    system.observe_book(
        "polymarket:p4",
        _book("p4", bid=0.50, ask=0.52, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=2),
    )
    system.observe_book(
        "polymarket:p3",
        _book("p3", bid=0.55, ask=0.57, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=4),
    )
    emitted = system.observe_book(
        "polymarket:p4",
        _book("p4", bid=0.52, ask=0.54, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=4),
    )

    relative = [intent for intent in emitted.intents if intent.lane == "relative_value"]
    assert len(relative) == 1
    assert relative[0].relation_id == relation.relation_id
    assert relative[0].direction == "long_restrictive_yes_long_broad_no"
    assert relative[0].model_version == "structural-residual-baseline-v1"


def test_acceptance_is_per_lane_and_fails_closed_before_preregistered_sample(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        acceptance_policy=AcceptancePolicy(
            min_event_clusters=50,
            min_intents=200,
            max_drawdown=10,
        ),
    )

    report = system.acceptance_report("directional_reaction")

    assert report.lane == "directional_reaction"
    assert report.passed is False
    assert "fewer_than_50_event_clusters" in report.reasons
    assert "fewer_than_200_intents" in report.reasons
    assert report.authority == "shadow_only"


def test_restart_restores_open_intent_marks_and_cooldown(tmp_path):
    path = tmp_path / "opportunities.db"
    restart_now = datetime.now(timezone.utc)
    close = restart_now + timedelta(hours=2)
    first_store = PlatformOpportunityStore(path)
    first = PlatformOpportunitySystem(store=first_store)
    first.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=restart_now,
    )
    first.set_fee_schedule("polymarket:p1", _zero_fee())
    first.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=restart_now,
    )
    emitted = first.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=restart_now + timedelta(seconds=5),
    )
    assert len(emitted.intents) == 1
    first_store.close()

    second_store = PlatformOpportunityStore(path)
    second = PlatformOpportunitySystem(store=second_store)
    second.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=restart_now + timedelta(seconds=10),
    )
    second.set_fee_schedule("polymarket:p1", _zero_fee())
    scored = second.observe_book(
        "polymarket:p1",
        _book("p1", bid=0.48, ask=0.50, bid_size=20, ask_size=300),
        observed_at=restart_now + timedelta(seconds=40),
    )

    assert any(mark.horizon_seconds == -1 for mark in scored.marks)
    assert len(second_store.intent_rows(cohort_id=second.cohort_id)) == 1


def test_acceptance_never_mixes_model_or_cost_cohorts(tmp_path):
    path = tmp_path / "opportunities.db"
    original = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path), slippage_per_contract=0.002
    )
    changed = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path), slippage_per_contract=0.003
    )

    assert original.cohort_id != changed.cohort_id
    assert changed.acceptance_report("directional_reaction").intents == 0


def test_dashboard_summary_and_research_pnl_do_not_mix_cohorts(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    close = NOW + timedelta(hours=2)
    first = PlatformOpportunitySystem(store=store, experiment_id="first")
    second = PlatformOpportunitySystem(store=store, experiment_id="second")
    assert first.cohort_id != second.cohort_id

    for system, market_id in ((first, "first"), (second, "second")):
        contract_id = f"polymarket:{market_id}"
        system.refresh_catalog(
            polymarket_markets=[
                _poly(market_id, "Will CPI be above 3%?", end_date=close)
            ],
            kalshi_markets=[],
            observed_at=NOW,
        )
        system.set_fee_schedule(contract_id, _zero_fee())
        system.observe_book(
            contract_id,
            _book(market_id, bid=0.49, ask=0.51, bid_size=100, ask_size=100),
            observed_at=NOW,
        )
        assert len(
            system.observe_book(
                contract_id,
                _book(market_id, bid=0.51, ask=0.53, bid_size=300, ask_size=50),
                observed_at=NOW + timedelta(seconds=5),
            ).intents
        ) == 1

    first.observe_book(
        "polymarket:first",
        _book("first", bid=0.57, ask=0.59, bid_size=200, ask_size=100),
        observed_at=NOW + timedelta(seconds=36),
    )

    first_dashboard = first.dashboard_summary()
    second_dashboard = second.dashboard_summary()
    assert first_dashboard["intents"] == {"directional_reaction": 1}
    assert second_dashboard["intents"] == {"directional_reaction": 1}
    assert first_dashboard["marks"] == 4
    assert second_dashboard["marks"] == 0
    assert first_dashboard["research_pnl"]["horizons"]["30"]["marks"] == 1
    assert second_dashboard["research_pnl"]["horizons"] == {}
