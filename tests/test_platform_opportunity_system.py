import json
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

import pytest

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
    PoliticalEventLock,
    PlatformOpportunitySystem,
    ReplayOccurrenceRoute,
    ReplayObservationToken,
    LaneAuthority,
    StructuralRelation,
    VenueFeeSchedule,
    _normalized_milestone_metadata,
)
from core.political_experimental_paper import PoliticalExperimentalPaperLedger
from core.political_sizing_scenarios import required_political_sizing_scenarios
from core.political_sizing_evidence import sealed_political_sizing_evidence
from core.political_sizing_report import (
    PoliticalSizingDepthLevel,
    PoliticalSizingExitEvidence,
    PoliticalSizingOpportunity,
    PoliticalSizingReportBundle,
    evaluate_required_political_sizing_scenarios,
    evaluate_political_sizing_scenario,
)
from utils.platform_opportunity_store import (
    PlatformOpportunityStore,
    ReplayEvidenceCapacityError,
    ReplayEvidenceIntegrityError,
)

NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)


def test_required_political_sizing_scenarios_are_immutable_and_evidence_scoped():
    """Seven approved paper-only policies share evidence but not policy identity."""
    scenarios = required_political_sizing_scenarios()

    assert [scenario.name for scenario in scenarios] == [
        "control_p25_t100",
        "cf_p50_t100",
        "cf_p50_t200",
        "cf_p100_t400",
        "cf_p150_t600",
        "cf_p200_t800",
        "cf_liquidity_ceiling",
    ]
    assert all(not scenario.deployable for scenario in scenarios)
    assert all(
        scenario.canonical_policy()["read_only_not_realized"] is True
        for scenario in scenarios
    )
    assert scenarios[0].scenario_id(evidence_cohort_id="evidence:a") != scenarios[
        0
    ].scenario_id(evidence_cohort_id="evidence:b")
    assert scenarios[0].scenario_id(evidence_cohort_id="evidence:a") != scenarios[
        1
    ].scenario_id(evidence_cohort_id="evidence:a")
    assert scenarios[-1].position_cap is None
    assert scenarios[-1].total_reserved_cap is None


def test_read_only_sizing_fans_identical_sealed_evidence_without_store_mutation(
    tmp_path,
):
    """Each policy allocates the same evidence and cannot create a paper row."""
    store = PlatformOpportunityStore(tmp_path / "sizing.db")
    before = store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    evidence = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:sealed",
        signal_id="signal:sealed",
        entry_replay_sequence=7,
        entry_replay_hash="hash:sealed",
        event_id="event-a",
        milestone_id="milestone-a",
        contract_id="contract-a",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
    )
    reports = [
        evaluate_political_sizing_scenario(
            scenario=scenario,
            opportunities=(evidence,),
            starting_cash_micros=1_000_000_000,
        )
        for scenario in required_political_sizing_scenarios()
    ]

    assert all(report.evidence_cohort_id == "evidence:sealed" for report in reports)
    assert all(
        report.allocations[0].signal_id == "signal:sealed"
        and report.allocations[0].entry_replay_hash == "hash:sealed"
        for report in reports
    )
    assert reports[0].allocations[0].executable_quantity == 10
    assert reports[-1].allocations[0].executable_quantity == 10
    assert reports[0].realized_pnl_micros is None
    assert reports[0].open_unrealized_pnl_micros is None
    assert reports[0].open_valuation_complete is False
    assert reports[0].maximum_drawdown_micros is None
    assert reports[0].peak_capital_used_micros == 4_270_000
    assert reports[0].capital_utilization_ratio == Decimal("0.0427")
    assert (
        store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        == before
    )


def test_required_sizing_fanout_keeps_control_and_counterfactuals_separate():
    """The seven required reports consume one sealed stream, never seven signals."""
    entry = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:fanout",
        signal_id="signal:fanout",
        entry_replay_sequence=1,
        entry_replay_hash="hash:entry",
        event_id="event-a",
        milestone_id="milestone-a",
        contract_id="contract-a",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
    )
    exit_evidence = PoliticalSizingExitEvidence(
        evidence_cohort_id="evidence:fanout",
        contract_id="contract-a",
        exit_replay_sequence=3,
        exit_replay_hash="hash:exit",
        trigger="event_boundary",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.55", Decimal("100")),),
    )

    bundle = evaluate_required_political_sizing_scenarios(
        opportunities=(entry,),
        exit_evidence=(exit_evidence,),
        starting_cash_micros=1_000_000_000,
    )

    assert bundle.read_only_not_realized is True
    assert bundle.control.scenario_name == "control_p25_t100"
    assert [report.scenario_name for report in bundle.counterfactuals] == [
        "cf_p50_t100",
        "cf_p50_t200",
        "cf_p100_t400",
        "cf_p150_t600",
        "cf_p200_t800",
        "cf_liquidity_ceiling",
    ]
    assert len(bundle.reports) == 7
    assert all(
        report.evidence_cohort_id == "evidence:fanout"
        and report.allocations[0].signal_id == "signal:fanout"
        and report.allocations[0].entry_replay_hash == "hash:entry"
        and report.exits[0].exit_replay_hash == "hash:exit"
        for report in bundle.reports
    )
    assert entry.entry_replay_hash == "hash:entry"
    assert exit_evidence.exit_replay_hash == "hash:exit"

    payload = bundle.dashboard_payload()

    assert payload["label"] == "counterfactual_sizing"
    assert payload["read_only_not_realized"] is True
    assert payload["control"]["scenario_name"] == "control_p25_t100"
    assert len(payload["counterfactuals"]) == 6
    assert "aggregate_pnl_micros" not in payload
    assert payload["control"]["realized_pnl_micros"] == 950_000
    assert payload["control"]["event_summaries"] == [
        {
            "event_id": "event-a",
            "allocation_count": 1,
            "requested_quantity": 10,
            "executable_quantity": 10,
            "capital_used_micros": 4_270_000,
            "capital_rejected_micros": 0,
            "unused_eligible_quantity": 0,
            "saturation_reasons": [],
        }
    ]
    assert payload["control"]["risk_group_summaries"] == [
        {
            "risk_group_id": "milestone-a",
            "allocation_count": 1,
            "requested_quantity": 10,
            "executable_quantity": 10,
            "capital_used_micros": 4_270_000,
            "capital_rejected_micros": 0,
            "unused_eligible_quantity": 0,
            "saturation_reasons": [],
        }
    ]
    assert payload["counterfactuals"][0]["allocations"][0] == {
        "signal_id": "signal:fanout",
        "entry_replay_sequence": 1,
        "entry_replay_hash": "hash:entry",
        "event_id": "event-a",
        "milestone_id": "milestone-a",
        "contract_id": "contract-a",
        "base_lane": "event_live",
        "risk_group_id": "milestone-a",
        "requested_quantity": 10,
        "executable_quantity": 10,
        "capital_used_micros": 4_270_000,
        "capital_rejected_micros": 0,
        "unused_eligible_quantity": 0,
        "saturation_reason": None,
    }


def test_read_only_sizing_enforces_occurrence_overlap_without_changing_evidence():
    """One scenario cannot count two open entries from the same occurrence."""
    first = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:sealed",
        signal_id="signal:first",
        entry_replay_sequence=1,
        entry_replay_hash="hash:first",
        event_id="event-a",
        milestone_id="milestone-a",
        contract_id="contract-a",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
    )
    second = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:sealed",
        signal_id="signal:second",
        entry_replay_sequence=2,
        entry_replay_hash="hash:second",
        event_id="event-a",
        milestone_id="milestone-a",
        contract_id="contract-b",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
    )

    report = evaluate_political_sizing_scenario(
        scenario=required_political_sizing_scenarios()[0],
        opportunities=(second, first),
        starting_cash_micros=1_000_000_000,
    )

    assert [item.signal_id for item in report.allocations] == [
        "signal:first",
        "signal:second",
    ]
    assert report.allocations[1].saturation_reason == "occurrence_overlap"


def test_read_only_sizing_enforces_typed_correlated_risk_group_caps():
    """Two Trump occurrences cannot consume all slots in one scenario group."""
    first = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:risk-group",
        signal_id="signal:first",
        entry_replay_sequence=1,
        entry_replay_hash="hash:first",
        event_id="trump-says",
        milestone_id="milestone-says",
        contract_id="contract-says",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
        risk_group_id="trump-aug-10",
    )
    second = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:risk-group",
        signal_id="signal:second",
        entry_replay_sequence=2,
        entry_replay_hash="hash:second",
        event_id="trump-mentions",
        milestone_id="milestone-mentions",
        contract_id="contract-mentions",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
        risk_group_id="trump-aug-10",
    )
    third = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:risk-group",
        signal_id="signal:third",
        entry_replay_sequence=3,
        entry_replay_hash="hash:third",
        event_id="trump-approval",
        milestone_id="milestone-approval",
        contract_id="contract-approval",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
        risk_group_id="trump-aug-10",
    )

    report = evaluate_political_sizing_scenario(
        scenario=required_political_sizing_scenarios()[0],
        opportunities=(first, second, third),
        starting_cash_micros=1_000_000_000,
    )

    assert [item.executable_quantity for item in report.allocations] == [10, 10, 0]
    assert report.allocations[-1].saturation_reason == "risk_group_max_open_positions"
    payload = PoliticalSizingReportBundle(
        evidence_cohort_id="evidence:risk-group",
        read_only_not_realized=True,
        control=report,
        counterfactuals=(),
    ).dashboard_payload()
    assert payload["control"]["risk_group_summaries"] == [
        {
            "risk_group_id": "trump-aug-10",
            "allocation_count": 3,
            "requested_quantity": 30,
            "executable_quantity": 20,
            "capital_used_micros": 8_540_000,
            "capital_rejected_micros": 4_270_000,
            "unused_eligible_quantity": 10,
            "saturation_reasons": ["risk_group_max_open_positions"],
        }
    ]


def test_read_only_sizing_enforces_correlated_risk_group_reserve_cap():
    """A group reserve cap limits size even when portfolio capacity remains."""
    first = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:risk-reserve",
        signal_id="signal:first",
        entry_replay_sequence=1,
        entry_replay_hash="hash:first",
        event_id="trump-says",
        milestone_id="milestone-says",
        contract_id="contract-says",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
        risk_group_id="trump-aug-10",
    )
    second = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:risk-reserve",
        signal_id="signal:second",
        entry_replay_sequence=2,
        entry_replay_hash="hash:second",
        event_id="trump-mentions",
        milestone_id="milestone-mentions",
        contract_id="contract-mentions",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
        risk_group_id="trump-aug-10",
    )
    scenario = required_political_sizing_scenarios()[0]
    scenario = type(scenario)(
        name="test_risk_reserve",
        position_cap=Decimal("25"),
        total_reserved_cap=Decimal("100"),
        max_open_positions=4,
        risk_label="counterfactual",
        risk_group_reserved_cap=Decimal("5"),
        max_open_positions_per_risk_group=4,
    )

    report = evaluate_political_sizing_scenario(
        scenario=scenario,
        opportunities=(first, second),
        starting_cash_micros=1_000_000_000,
    )

    assert [item.executable_quantity for item in report.allocations] == [10, 1]
    assert report.allocations[-1].saturation_reason == (
        "capital_or_position_or_risk_group_cap"
    )
    assert second.entry_replay_hash == "hash:second"


def test_read_only_sizing_applies_sealed_exit_evidence_without_store_mutation(tmp_path):
    """Every scenario uses the same exit replay and releases only its own capital."""
    store = PlatformOpportunityStore(tmp_path / "sizing-exit.db")
    before = store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    entry = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:sealed",
        signal_id="signal:sealed",
        entry_replay_sequence=1,
        entry_replay_hash="hash:entry",
        event_id="event-a",
        milestone_id="milestone-a",
        contract_id="contract-a",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("100")),),
    )
    exit_evidence = PoliticalSizingExitEvidence(
        evidence_cohort_id="evidence:sealed",
        contract_id="contract-a",
        exit_replay_sequence=3,
        exit_replay_hash="hash:exit",
        trigger="event_boundary",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.55", Decimal("100")),),
    )

    reports = [
        evaluate_political_sizing_scenario(
            scenario=scenario,
            opportunities=(entry,),
            exit_evidence=(exit_evidence,),
            starting_cash_micros=1_000_000_000,
        )
        for scenario in required_political_sizing_scenarios()
    ]

    assert all(report.capital_used_micros == 0 for report in reports)
    assert all(report.realized_pnl_micros == 950_000 for report in reports)
    assert all(report.open_valuation_complete for report in reports)
    assert all(report.open_unrealized_pnl_micros is None for report in reports)
    assert all(report.maximum_drawdown_micros == 0 for report in reports)
    assert all(
        report.exits[0].exit_replay_hash == "hash:exit"
        and report.exits[0].trigger == "event_boundary"
        and report.exits[0].executable_quantity == 10
        for report in reports
    )
    assert (
        store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        == before
    )


def test_read_only_sizing_reports_cap_rejection_and_unused_eligible_depth():
    """A cap-limited scenario distinguishes unallocated depth from used capital."""
    opportunity = PoliticalSizingOpportunity(
        evidence_cohort_id="evidence:sealed",
        signal_id="signal:cap-limited",
        entry_replay_sequence=1,
        entry_replay_hash="hash:cap-limited",
        event_id="event-a",
        milestone_id="milestone-a",
        contract_id="contract-a",
        base_lane="event_live",
        side="yes",
        fee_schedule=_authoritative_kalshi_fee(),
        levels=(PoliticalSizingDepthLevel("0.40", Decimal("1000")),),
    )

    report = evaluate_political_sizing_scenario(
        scenario=required_political_sizing_scenarios()[0],
        opportunities=(opportunity,),
        starting_cash_micros=1_000_000_000,
    )

    allocation = report.allocations[0]
    assert allocation.requested_quantity == 100
    assert 0 < allocation.executable_quantity < allocation.requested_quantity
    assert report.unused_eligible_quantity == allocation.unused_eligible_quantity
    assert report.capital_rejected_micros > 0
    assert report.peak_capital_used_micros == report.capital_used_micros


def _authoritative_kalshi_fee() -> dict[str, str | int]:
    return {
        "schema_version": 1,
        "venue": "kalshi",
        "fee_type": "kalshi_quadratic",
        "rate": "0.07",
        "exponent": "1",
        "multiplier": "1",
        "observed_at": NOW.isoformat(),
        "fetched_at": NOW.isoformat(),
        "source": "kalshi_public_metadata",
    }


def _lane_authorities(*lanes: str) -> dict[str, LaneAuthority]:
    return {lane: "forward_only_unvalidated" for lane in lanes}


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


def test_political_experimental_paper_account_is_idempotent_and_cent_exact(tmp_path):
    """The political ledger starts once and records a durable after-state."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    cohort_id = "political-v2-test"

    first = store.initialize_political_experimental_paper_account(
        cohort_id=cohort_id,
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW,
    )
    second = store.initialize_political_experimental_paper_account(
        cohort_id=cohort_id,
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW + timedelta(seconds=1),
    )

    assert (
        first
        == second
        == {
            "starting_cash_micros": 1_000_000_000,
            "cash_micros": 1_000_000_000,
            "reserved_micros": 0,
            "realized_pnl_micros": 0,
            "open_positions": 0,
            "valuation_complete": True,
        }
    )
    assert store.political_experimental_paper_events(cohort_id=cohort_id) == [
        {
            "sequence": 1,
            "event_type": "account_initialized",
            "occurred_at": NOW.isoformat(),
            "cash_micros": 1_000_000_000,
            "reserved_micros": 0,
            "realized_pnl_micros": 0,
            "payload": {"starting_cash_micros": 1_000_000_000},
        }
    ]


def test_political_paper_ordinary_account_trade_economics_match_binding_fixture(
    tmp_path,
):
    """Political paper uses per-fill Kalshi fees and conservative cent balances."""
    ledger = PoliticalExperimentalPaperLedger(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        cohort_id="political-v2-economics",
    )

    fee = _authoritative_kalshi_fee()
    entry = ledger.entry_economics(quantity=10, displayed_ask="0.40", fee_schedule=fee)
    assert entry.effective_price == "0.41"
    assert entry.raw_fee == "0.16933"
    assert entry.rounded_trade_fee == "0.1694"
    assert entry.balance_change_micros == -4_270_000

    first_exit = ledger.exit_economics(
        quantity=4, displayed_bid="0.55", fee_schedule=fee
    )
    assert first_exit.effective_price == "0.54"
    assert first_exit.raw_fee == "0.069552"
    assert first_exit.rounded_trade_fee == "0.0696"
    assert first_exit.balance_change_micros == 2_090_000
    assert (
        first_exit.proportional_basis_micros(
            total_basis_micros=4_270_000, total_quantity=10
        )
        == 1_708_000
    )

    final_exit = ledger.exit_economics(
        quantity=6, displayed_bid="0.55", fee_schedule=fee
    )
    assert final_exit.balance_change_micros == 3_130_000
    assert (
        first_exit.balance_change_micros
        + final_exit.balance_change_micros
        - entry.debit_micros
        == 950_000
    )


def test_political_paper_accepts_authoritative_zero_multiplier_fee_schedule(tmp_path):
    """A captured quadratic schedule may legitimately charge zero fees."""
    ledger = PoliticalExperimentalPaperLedger(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        cohort_id="political-v2-zero-fee",
    )

    entry = ledger.entry_economics(
        quantity=1,
        displayed_ask="0.40",
        fee_schedule={**_authoritative_kalshi_fee(), "multiplier": "0"},
    )

    assert entry.raw_fee == "0"
    assert entry.rounded_trade_fee == "0"
    assert entry.balance_change_micros == -410_000


@pytest.mark.parametrize(
    "mutation, error",
    (
        ({"fee_type": "none"}, "fee type"),
        ({"rate": "0"}, "terms"),
        ({"fetched_at": None}, "timing"),
    ),
)
def test_political_paper_economics_rejects_missing_or_unsupported_replay_fee(
    tmp_path, mutation, error
):
    """The binding rate comes from replay evidence; it is never a fallback."""
    ledger = PoliticalExperimentalPaperLedger(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        cohort_id="political-v2-fee-validation",
    )
    with pytest.raises(ValueError, match=error):
        ledger.entry_economics(
            quantity=1,
            displayed_ask="0.40",
            fee_schedule={**_authoritative_kalshi_fee(), **mutation},
        )


def test_political_paper_account_initialization_serializes_connections_and_rejects_drift(
    tmp_path,
):
    """Separate workers create one account/event and cannot change its policy."""
    path = tmp_path / "opportunities.db"
    stores = (PlatformOpportunityStore(path), PlatformOpportunityStore(path))

    def initialize(index: int) -> dict[str, int | bool]:
        return stores[index].initialize_political_experimental_paper_account(
            cohort_id="political-v2-race",
            starting_cash_micros=1_000_000_000,
            initialized_at=NOW + timedelta(seconds=index),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            accounts = list(executor.map(initialize, (0, 1)))
    finally:
        for store in stores:
            store.close()

    assert accounts[0] == accounts[1]
    store = PlatformOpportunityStore(path)
    assert [
        event["event_type"]
        for event in store.political_experimental_paper_events(
            cohort_id="political-v2-race"
        )
    ] == ["account_initialized"]
    with pytest.raises(ValueError, match="policy drift"):
        store.initialize_political_experimental_paper_account(
            cohort_id="political-v2-race",
            starting_cash_micros=999_000_000,
            initialized_at=NOW,
        )


def test_kalshi_event_index_only_retires_absent_rows_after_cursor_exhaustion(tmp_path):
    """A bounded or failed source page cannot erase previously active inventory."""
    path = tmp_path / "opportunities.db"
    started = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    store = PlatformOpportunityStore(path)

    store.begin_kalshi_event_index_refresh(refresh_id="pass-1", started_at=started)
    assert (
        store.record_kalshi_event_index_page(
            refresh_id="pass-1",
            events=(
                {"event_ticker": "KXALPHA-26", "title": "raw alpha"},
                {"event_ticker": "KXBETA-26", "category": "Elections"},
            ),
            next_cursor=None,
            observed_at=started,
        )
        == 2
    )
    assert (
        store.complete_kalshi_event_index_refresh(
            refresh_id="pass-1", completed_at=started
        )
        == 0
    )

    store.begin_kalshi_event_index_refresh(
        refresh_id="pass-2", started_at=started + timedelta(minutes=1)
    )
    store.record_kalshi_event_index_page(
        refresh_id="pass-2",
        events=({"event_ticker": "KXALPHA-26", "title": "raw alpha v2"},),
        next_cursor="next-page",
        observed_at=started + timedelta(minutes=1),
    )
    store.fail_kalshi_event_index_refresh(refresh_id="pass-2", reason="429 backoff")
    assert [
        row["event_ticker"] for row in store.kalshi_event_index_rows(active_only=True)
    ] == [
        "KXALPHA-26",
        "KXBETA-26",
    ]
    assert store.kalshi_event_index_state()["next_cursor"] == "next-page"
    store.close()

    restarted = PlatformOpportunityStore(path)
    assert restarted.kalshi_event_index_state()["last_failure"] == "429 backoff"
    assert [
        row["event_ticker"]
        for row in restarted.kalshi_event_index_rows(active_only=True)
    ] == [
        "KXALPHA-26",
        "KXBETA-26",
    ]

    restarted.begin_kalshi_event_index_refresh(
        refresh_id="pass-3", started_at=started + timedelta(minutes=2)
    )
    restarted.record_kalshi_event_index_page(
        refresh_id="pass-3",
        events=({"event_ticker": "KXALPHA-26", "title": "raw alpha v3"},),
        next_cursor=None,
        observed_at=started + timedelta(minutes=2),
    )
    assert (
        restarted.complete_kalshi_event_index_refresh(
            refresh_id="pass-3", completed_at=started + timedelta(minutes=3)
        )
        == 1
    )
    rows = restarted.kalshi_event_index_rows()
    assert [(row["event_ticker"], row["active"]) for row in rows] == [
        ("KXALPHA-26", True),
        ("KXBETA-26", False),
    ]
    assert rows[0]["payload"] == {
        "event_ticker": "KXALPHA-26",
        "title": "raw alpha v3",
    }


def test_kalshi_event_index_rejects_duplicate_or_incomplete_refresh_pages(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    store.begin_kalshi_event_index_refresh(refresh_id="pass", started_at=now)

    with pytest.raises(ValueError, match="duplicate event_ticker"):
        store.record_kalshi_event_index_page(
            refresh_id="pass",
            events=(
                {"event_ticker": "KXDUP-26"},
                {"event_ticker": "KXDUP-26"},
            ),
            next_cursor=None,
            observed_at=now,
        )
    store.record_kalshi_event_index_page(
        refresh_id="pass",
        events=({"event_ticker": "KXONE-26"},),
        next_cursor="more",
        observed_at=now,
    )
    with pytest.raises(RuntimeError, match="not cursor-exhausted"):
        store.complete_kalshi_event_index_refresh(refresh_id="pass", completed_at=now)


def test_kalshi_event_probe_state_survives_restart_and_caps_failure_backoff(tmp_path):
    """Rotation can prefer unprobed rows without retrying one failed source forever."""
    path = tmp_path / "opportunities.db"
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    store = PlatformOpportunityStore(path)

    first = store.record_kalshi_event_probe_failure(
        event_ticker="KXPROBE-26",
        attempted_at=now,
        reason="429 backoff",
        base_backoff_seconds=30,
        max_backoff_seconds=90,
    )
    second = store.record_kalshi_event_probe_failure(
        event_ticker="KXPROBE-26",
        attempted_at=now + timedelta(seconds=31),
        reason="temporary response",
        base_backoff_seconds=30,
        max_backoff_seconds=90,
    )
    third = store.record_kalshi_event_probe_failure(
        event_ticker="KXPROBE-26",
        attempted_at=now + timedelta(minutes=2),
        reason="temporary response",
        base_backoff_seconds=30,
        max_backoff_seconds=90,
    )
    assert [
        first["backoff_seconds"],
        second["backoff_seconds"],
        third["backoff_seconds"],
    ] == [
        30,
        60,
        90,
    ]
    assert third["consecutive_failures"] == 3
    store.close()

    restarted = PlatformOpportunityStore(path)
    assert restarted.kalshi_event_probe_state("KXPROBE-26") == {
        "event_ticker": "KXPROBE-26",
        "last_attempt_at": (now + timedelta(minutes=2)).isoformat(),
        "last_success_at": None,
        "last_failure_at": (now + timedelta(minutes=2)).isoformat(),
        "consecutive_failures": 3,
        "next_eligible_at": (now + timedelta(minutes=2, seconds=90)).isoformat(),
        "last_failure": "temporary response",
    }
    success = restarted.record_kalshi_event_probe_success(
        event_ticker="KXPROBE-26", attempted_at=now + timedelta(minutes=4)
    )
    assert success["consecutive_failures"] == 0
    assert success["next_eligible_at"] == (now + timedelta(minutes=4)).isoformat()
    assert success["last_failure"] is None


def test_kalshi_event_rotation_prefers_unprobed_currently_political_rows_across_restart(
    tmp_path,
):
    """Raw index history is classified at selection time and rotates durably."""
    path = tmp_path / "opportunities.db"
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    store = PlatformOpportunityStore(path)
    store.begin_kalshi_event_index_refresh(refresh_id="full-pass", started_at=now)
    store.record_kalshi_event_index_page(
        refresh_id="full-pass",
        events=tuple(
            {
                "event_ticker": f"KXROTATE-{index:02d}",
                "title": f"Election fixture {index}",
                "category": "Elections",
            }
            for index in range(50)
        )
        + (
            {
                "event_ticker": "KXMENTION-NONPOLITICAL",
                "title": "Celebrity mentions fixture",
                "series_ticker": "KXMENTION",
                "category": "Mentions",
            },
            {
                "event_ticker": "KXMENTION-POLITICAL",
                "title": "Trump mentions fixture",
                "series_ticker": "KXMENTION",
                "category": "Mentions",
            },
        ),
        next_cursor=None,
        observed_at=now,
    )
    store.complete_kalshi_event_index_refresh(refresh_id="full-pass", completed_at=now)
    system = PlatformOpportunitySystem(
        store=store,
        political_watch_policy=PoliticalWatchPolicy(
            max_events=4,
            reviewed_pinned_event_ids=("kalshi:KXROTATE-00",),
        ),
    )
    coverage = system.kalshi_event_rotation_coverage(now=now + timedelta(minutes=5))
    assert coverage["eligible_event_tickers"] == 50
    assert coverage["unprobed_event_tickers"] == 50
    assert coverage["oldest_unprobed_age_seconds"] == 300.0
    assert coverage["full_pass_progress"] == {
        "completed_event_tickers": 0,
        "eligible_event_tickers": 50,
        "fraction": 0.0,
        "complete": False,
    }

    first = system.select_kalshi_event_rotation(now=now, limit=24)
    assert "KXROTATE-00" not in first
    assert "KXMENTION-NONPOLITICAL" not in first
    assert "KXMENTION-POLITICAL" in first
    assert len(first) == 24
    for ticker in first:
        store.record_kalshi_event_probe_success(event_ticker=ticker, attempted_at=now)
    store.close()

    restarted = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path),
        political_watch_policy=PoliticalWatchPolicy(
            max_events=4,
            reviewed_pinned_event_ids=("kalshi:KXROTATE-00",),
        ),
    )
    second = restarted.select_kalshi_event_rotation(
        now=now + timedelta(minutes=1), limit=24
    )
    assert len(second) == 24
    assert set(first).isdisjoint(second)
    assert set(first + second).issubset(
        {f"KXROTATE-{index:02d}" for index in range(1, 50)} | {"KXMENTION-POLITICAL"}
    )


def test_kalshi_event_rotation_skips_backing_off_rows_and_retained_locks(tmp_path):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    store.begin_kalshi_event_index_refresh(refresh_id="pass", started_at=now)
    store.record_kalshi_event_index_page(
        refresh_id="pass",
        events=(
            {"event_ticker": "KXBACKOFF", "category": "Politics"},
            {"event_ticker": "KXRETAINED", "category": "Politics"},
            {"event_ticker": "KXREADY", "category": "Politics"},
        ),
        next_cursor=None,
        observed_at=now,
    )
    store.complete_kalshi_event_index_refresh(refresh_id="pass", completed_at=now)
    store.record_kalshi_event_probe_failure(
        event_ticker="KXBACKOFF",
        attempted_at=now,
        reason="429",
        base_backoff_seconds=60,
        max_backoff_seconds=60,
    )
    system = PlatformOpportunitySystem(
        store=store,
        political_watch_policy=PoliticalWatchPolicy(max_events=4),
    )
    system._political_locks = {
        "KXRETAINED": PoliticalEventLock(
            event_id="KXRETAINED",
            event_title="Retained",
            occurrence_at=now,
            event_start_at=now,
            event_end_at=now + timedelta(hours=1),
            locked_until=now + timedelta(hours=2),
            selected_at=now,
            contract_ids=(),
        )
    }

    assert system.select_kalshi_event_rotation(now=now, limit=24) == ("KXREADY",)


def test_political_paper_account_binds_a_canonical_immutable_policy(tmp_path):
    """A restart can reuse a cohort only with the identical paper policy."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    policy = {
        "max_open_positions": 4,
        "entry_depth_fraction": "0.10",
        "exit_rule": {"maximum_hold_seconds": 600},
    }
    store.initialize_political_experimental_paper_account(
        cohort_id="political-v2-policy",
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW,
        policy=policy,
    )

    bound = store.political_experimental_paper_policy(cohort_id="political-v2-policy")
    assert bound["policy"] == {
        **policy,
        "schema_version": 1,
        "starting_cash_micros": 1_000_000_000,
    }
    assert len(bound["policy_hash"]) == 64

    store.initialize_political_experimental_paper_account(
        cohort_id="political-v2-policy",
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW + timedelta(seconds=1),
        policy=dict(reversed(list(policy.items()))),
    )
    with pytest.raises(ValueError, match="immutable policy differs"):
        store.initialize_political_experimental_paper_account(
            cohort_id="political-v2-policy",
            starting_cash_micros=1_000_000_000,
            initialized_at=NOW,
            policy={**policy, "max_open_positions": 3},
        )


def test_political_paper_entry_resolution_uses_only_immutable_policy_limits(
    tmp_path,
):
    """A resolver cannot accept caller-selected capital or depth limits."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:policy")
    ledger.initialize(
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW,
        policy={
            "max_total_reserved_micros": 100_000_000,
            "max_position_reserved_micros": 25_000_000,
            "max_open_positions": 4,
            "entry_depth_fraction": "0.10",
        },
    )

    assert ledger._entry_limits() == (
        100_000_000,
        25_000_000,
        4,
        Decimal("0.10"),
    )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        ledger._resolve_pending_signal(
            signal_id="signal:policy",
            replay_sequence=1,
            max_position_reserved_micros=1,
        )
    assert not hasattr(store, "resolve_political_experimental_pending_signal")
    assert not hasattr(ledger, "resolve_pending_signal")


def test_political_paper_exit_uses_only_immutable_policy_limits(tmp_path):
    """An exit caller cannot shorten its hold or widen its displayed depth."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:policy")
    ledger.initialize(
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW,
        policy={
            "max_total_reserved_micros": 100_000_000,
            "max_position_reserved_micros": 25_000_000,
            "max_open_positions": 4,
            "entry_depth_fraction": "0.10",
            "minimum_hold_seconds": "2",
        },
    )

    assert ledger._exit_limits() == (Decimal("2"), Decimal("0.10"))
    assert not hasattr(ledger, "exit_position")
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        ledger._exit_position(
            position_id="position:policy",
            replay_sequence=1,
            minimum_hold_seconds=0,
        )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        ledger._exit_position(
            position_id="position:policy",
            replay_sequence=1,
            displayed_depth_fraction=Decimal("1"),
        )


def test_political_pending_signal_is_durable_causal_and_restart_idempotent(tmp_path):
    """A qualified signal survives restart but is never itself a fill attempt."""
    path = tmp_path / "opportunities.db"
    ledger = PoliticalExperimentalPaperLedger(
        store=PlatformOpportunityStore(path), cohort_id="political-v2-test"
    )
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    replay = ledger.store.record_replay_observation(
        cohort_id="political-v2-test",
        contract_id="kalshi:KXTEST-26AUG",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.51, 10]], "asks": [[0.52, 10]]},
            "no": {"bids": [[0.48, 10]], "asks": [[0.49, 10]]},
        },
        fee_schedule={"schema_version": 1, "venue": "kalshi", "fee_type": "none"},
        lock_phase="hot",
        observed_at=NOW + timedelta(milliseconds=100),
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    signal = {
        "signal_id": "signal:causal-1",
        "replay_sequence": replay["event"]["sequence"],
        "event_id": "event-1",
        "milestone_id": "milestone-1",
        "contract_id": "kalshi:KXTEST-26AUG",
        "side": "yes",
        "base_lane": "hot_pre_event",
        "phase": "hot",
        "signal_request_started_at": NOW,
        "signal_received_at": NOW + timedelta(milliseconds=100),
        "expires_at": NOW + timedelta(seconds=10),
        "model_version": "depth-imbalance-reaction-experimental-v1",
        "config_hash": "config-hash",
        "state_hash": replay["state_hash"],
        "fee_hash": replay["fee_hash"],
        "features": {"mid": 0.52, "imbalance": 0.4},
    }

    assert ledger.record_pending_signal(**signal) is True
    restarted = PoliticalExperimentalPaperLedger(
        store=PlatformOpportunityStore(path), cohort_id="political-v2-test"
    )
    assert restarted.record_pending_signal(**signal) is False
    assert restarted.store.political_experimental_pending_signals(
        cohort_id="political-v2-test"
    ) == [
        {
            "signal_id": "signal:causal-1",
            "replay_sequence": 1,
            "event_id": "event-1",
            "milestone_id": "milestone-1",
            "contract_id": "kalshi:KXTEST-26AUG",
            "side": "yes",
            "base_lane": "hot_pre_event",
            "phase": "hot",
            "signal_request_started_at": NOW.isoformat(),
            "signal_received_at": (NOW + timedelta(milliseconds=100)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=10)).isoformat(),
            "model_version": "depth-imbalance-reaction-experimental-v1",
            "config_hash": "config-hash",
            "state_hash": replay["state_hash"],
            "fee_hash": replay["fee_hash"],
            "features": {"mid": 0.52, "imbalance": 0.4},
        }
    ]
    assert restarted.store.political_experimental_paper_events(
        cohort_id="political-v2-test"
    ) == [
        {
            "sequence": 1,
            "event_type": "account_initialized",
            "occurred_at": NOW.isoformat(),
            "cash_micros": 1_000_000_000,
            "reserved_micros": 0,
            "realized_pnl_micros": 0,
            "payload": {"starting_cash_micros": 1_000_000_000},
        }
    ]


def test_political_pending_signal_rejects_unproven_provenance_and_id_collision(
    tmp_path,
):
    """A signal may only repeat its exact durable replay provenance."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:test")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    replay = store.record_replay_observation(
        cohort_id="cohort:test",
        contract_id="kalshi:KXTEST",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.51, 10]], "asks": [[0.52, 10]]},
            "no": {"bids": [[0.48, 10]], "asks": [[0.49, 10]]},
        },
        fee_schedule={"schema_version": 1, "venue": "kalshi", "fee_type": "none"},
        lock_phase="hot",
        observed_at=NOW + timedelta(milliseconds=100),
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    signal = {
        "signal_id": "signal:provenance",
        "replay_sequence": replay["event"]["sequence"],
        "event_id": "event-1",
        "milestone_id": "milestone-1",
        "contract_id": "kalshi:KXTEST",
        "side": "yes",
        "base_lane": "hot_pre_event",
        "phase": "hot",
        "signal_request_started_at": NOW,
        "signal_received_at": NOW + timedelta(milliseconds=100),
        "expires_at": NOW + timedelta(seconds=10),
        "model_version": "depth-imbalance-reaction-experimental-v1",
        "config_hash": "config-hash",
        "state_hash": replay["state_hash"],
        "fee_hash": replay["fee_hash"],
        "features": {"mid": 0.52, "imbalance": 0.4},
    }

    with pytest.raises(ValueError, match="provenance"):
        ledger.record_pending_signal(**{**signal, "fee_hash": "caller-asserted"})
    with pytest.raises(ValueError, match="replay event"):
        ledger.record_pending_signal(**{**signal, "replay_sequence": 2})

    assert ledger.record_pending_signal(**signal) is True
    assert ledger.record_pending_signal(**signal) is False
    with pytest.raises(ValueError, match="id collision"):
        ledger.record_pending_signal(
            **{**signal, "features": {"mid": 0.53, "imbalance": 0.4}}
        )


def test_political_paper_ttl_sweeper_durably_consumes_expired_signal_on_restart(
    tmp_path,
):
    """A missing later book becomes one durable terminal no-fill at TTL."""
    path = tmp_path / "opportunities.db"
    store = PlatformOpportunityStore(path)
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:ttl")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    replay = store.record_replay_observation(
        cohort_id="cohort:ttl",
        contract_id="kalshi:KXTTL",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.51, 10]], "asks": [[0.52, 10]]},
            "no": {"bids": [[0.48, 10]], "asks": [[0.49, 10]]},
        },
        fee_schedule={"schema_version": 1, "venue": "kalshi", "fee_type": "none"},
        lock_phase="hot",
        observed_at=NOW + timedelta(milliseconds=100),
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    expires_at = NOW + timedelta(seconds=10)
    assert ledger.record_pending_signal(
        signal_id="signal:ttl",
        replay_sequence=replay["event"]["sequence"],
        event_id="event-1",
        milestone_id="milestone-1",
        contract_id="kalshi:KXTTL",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=expires_at,
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=replay["state_hash"],
        fee_hash=replay["fee_hash"],
        features={"imbalance": 0.4},
    )

    assert ledger.expire_pending(as_of=expires_at - timedelta(microseconds=1)) == []
    expired = ledger.expire_pending(as_of=expires_at)
    assert expired == [
        {
            "signal_id": "signal:ttl",
            "replay_sequence": 1,
            "reason": "ttl_expired_without_later_book",
            "payload": {
                "replay_sequence": 1,
                "expires_at": expires_at.isoformat(),
                "expired_as_of": expires_at.isoformat(),
            },
        }
    ]
    restarted = PoliticalExperimentalPaperLedger(
        store=PlatformOpportunityStore(path), cohort_id="cohort:ttl"
    )
    assert restarted.expire_pending(as_of=expires_at + timedelta(minutes=1)) == []
    assert (
        restarted.store.political_experimental_pending_signals(cohort_id="cohort:ttl")
        == []
    )
    assert restarted._resolve_pending_signal(
        signal_id="signal:ttl",
        replay_sequence=1,
    ) == {
        "outcome": "no_fill",
        "reason": "ttl_expired_without_later_book",
        "idempotent": True,
        "payload": expired[0]["payload"],
    }


def test_political_paper_opens_only_from_a_strictly_later_causal_replay_book(tmp_path):
    """The signal book cannot fund a fill; the next causal book can."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:causal")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.50, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    first = store.record_replay_observation(
        cohort_id="cohort:causal",
        contract_id="kalshi:KXCAUSAL",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    signal = {
        "signal_id": "signal:causal-fill",
        "replay_sequence": first["event"]["sequence"],
        "event_id": "event-1",
        "milestone_id": "milestone-1",
        "contract_id": "kalshi:KXCAUSAL",
        "side": "yes",
        "base_lane": "hot_pre_event",
        "phase": "hot",
        "signal_request_started_at": NOW,
        "signal_received_at": NOW + timedelta(milliseconds=100),
        "expires_at": NOW + timedelta(seconds=10),
        "model_version": "depth-imbalance-reaction-experimental-v1",
        "config_hash": "config-hash",
        "state_hash": first["state_hash"],
        "fee_hash": first["fee_hash"],
        "features": {"imbalance": 0.4},
    }
    assert ledger.record_pending_signal(**signal)
    later = store.record_replay_observation(
        cohort_id="cohort:causal",
        contract_id="kalshi:KXCAUSAL",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )

    results = ledger.process_observation(replay_sequence=later["event"]["sequence"])
    assert len(results) == 1
    result = results[0]

    assert result["outcome"] == "filled"
    assert result["payload"]["quantity"] == 10
    assert result["payload"]["debit_micros"] == 4_270_000
    assert store.political_experimental_paper_events(cohort_id="cohort:causal")[-1] == {
        "sequence": 2,
        "event_type": "position_opened",
        "occurred_at": (NOW + timedelta(seconds=1)).isoformat(),
        "cash_micros": 995_730_000,
        "reserved_micros": 4_270_000,
        "realized_pnl_micros": 0,
        "payload": result["payload"],
    }

    first_exit = store.record_replay_observation(
        cohort_id="cohort:causal",
        contract_id="kalshi:KXCAUSAL",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.55, 40]], "asks": [[0.56, 100]]},
            "no": {"bids": [[0.44, 100]], "asks": [[0.45, 40]]},
        },
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=4),
        request_started_at=NOW + timedelta(seconds=3),
        received_at=NOW + timedelta(seconds=4),
    )
    partially_closed = ledger._exit_position(
        position_id=result["payload"]["position_id"],
        replay_sequence=first_exit["event"]["sequence"],
        trigger="event_boundary",
    )
    assert partially_closed["outcome"] == "partial"
    assert partially_closed["payload"]["quantity"] == 4
    assert partially_closed["payload"]["credit_micros"] == 2_090_000
    assert partially_closed["payload"]["basis_release_micros"] == 1_708_000

    final_exit = store.record_replay_observation(
        cohort_id="cohort:causal",
        contract_id="kalshi:KXCAUSAL",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.55, 60]], "asks": [[0.56, 100]]},
            "no": {"bids": [[0.44, 100]], "asks": [[0.45, 60]]},
        },
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=6),
        request_started_at=NOW + timedelta(seconds=5),
        received_at=NOW + timedelta(seconds=6),
    )
    closed = ledger._exit_position(
        position_id=result["payload"]["position_id"],
        replay_sequence=final_exit["event"]["sequence"],
        trigger="event_boundary",
    )
    assert closed["outcome"] == "closed"
    assert closed["payload"]["quantity"] == 6
    assert closed["payload"]["credit_micros"] == 3_130_000
    assert closed["payload"]["remaining_quantity"] == 0
    assert store.political_experimental_positions(cohort_id="cohort:causal") == []
    assert store.political_experimental_paper_events(cohort_id="cohort:causal")[-1] == {
        "sequence": 4,
        "event_type": "position_closed",
        "occurred_at": (NOW + timedelta(seconds=6)).isoformat(),
        "cash_micros": 1_000_950_000,
        "reserved_micros": 0,
        "realized_pnl_micros": 950_000,
        "payload": closed["payload"],
    }
    opportunities, exit_evidence = sealed_political_sizing_evidence(
        store=store, cohort_id="cohort:causal"
    )
    assert [
        (item.signal_id, item.entry_replay_sequence, item.entry_replay_hash)
        for item in opportunities
    ] == [("signal:causal-fill", 2, later["state_hash"])]
    assert [
        (item.exit_replay_sequence, item.exit_replay_hash, item.trigger)
        for item in exit_evidence
    ] == [
        (3, first_exit["state_hash"], "event_boundary"),
        (4, final_exit["state_hash"], "event_boundary"),
    ]
    assert ledger.snapshot() == {
        "account": {
            "starting_cash_micros": 1_000_000_000,
            "cash_micros": 1_000_950_000,
            "reserved_micros": 0,
            "realized_pnl_micros": 950_000,
        },
        "open_positions": [],
        "risk_group_exposure": [],
        "valuation_complete": True,
        "counts": {
            "signals": 1,
            "pending": 0,
            "filled": 1,
            "no_fill": 0,
            "open": 0,
            "closed": 1,
            "partial_exits": 1,
        },
    }


def test_political_paper_never_skips_the_first_later_same_contract_token(tmp_path):
    """A callback loss at sequence two cannot let sequence three fill it."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:barrier")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.50, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    first = store.record_replay_observation(
        cohort_id="cohort:barrier",
        contract_id="kalshi:KXBARRIER",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:barrier",
        replay_sequence=first["event"]["sequence"],
        event_id="event-barrier",
        milestone_id="milestone-barrier",
        contract_id="kalshi:KXBARRIER",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=first["state_hash"],
        fee_hash=first["fee_hash"],
        features={"imbalance": 0.4},
    )
    # This is the durable token whose callback/worker processing was lost.
    store.record_replay_observation(
        cohort_id="cohort:barrier",
        contract_id="kalshi:KXBARRIER",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )
    third = store.record_replay_observation(
        cohort_id="cohort:barrier",
        contract_id="kalshi:KXBARRIER",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=2),
        request_started_at=NOW + timedelta(seconds=1, milliseconds=1),
        received_at=NOW + timedelta(seconds=2),
    )

    [result] = ledger.process_observation(replay_sequence=third["event"]["sequence"])

    assert result["outcome"] == "no_fill"
    assert result["reason"] == "first_later_replay_sequence_unprocessed"
    assert ledger.snapshot()["counts"]["filled"] == 0
    assert ledger.snapshot()["counts"]["no_fill"] == 1


def test_political_paper_reports_cash_shortfall_not_fractional_depth(tmp_path):
    """An affordable-depth failure is a capital blocker, never a depth blocker."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:cash")
    ledger.initialize(
        starting_cash_micros=420_000,
        initialized_at=NOW,
        policy={
            "max_total_reserved_micros": 100_000_000,
            "max_position_reserved_micros": 25_000_000,
            "max_open_positions": 4,
            "entry_depth_fraction": "0.10",
        },
    )
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.39, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    first = store.record_replay_observation(
        cohort_id="cohort:cash",
        contract_id="kalshi:KXCASH",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:cash",
        replay_sequence=first["event"]["sequence"],
        event_id="event-1",
        milestone_id="milestone-1",
        contract_id="kalshi:KXCASH",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=first["state_hash"],
        fee_hash=first["fee_hash"],
        features={"imbalance": 0.4},
    )
    later = store.record_replay_observation(
        cohort_id="cohort:cash",
        contract_id="kalshi:KXCASH",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )

    result = ledger.process_observation(replay_sequence=later["event"]["sequence"])[0]

    assert result["outcome"] == "no_fill"
    assert result["reason"] == "insufficient_cash"
    assert result["payload"]["quantity"] == 1
    assert result["payload"]["debit_micros"] == 430_000
    assert result["payload"]["economics"] == {
        "sizing_context": {"minimum_whole_contract_debit_micros": 430_000}
    }


def test_political_paper_process_observation_exits_max_hold_without_caller_choice(
    tmp_path,
):
    """A later sealed token applies the frozen ten-minute exit by itself."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:max-hold")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.55, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.44, 100]], "asks": [[0.60, 100]]},
    }
    first = store.record_replay_observation(
        cohort_id="cohort:max-hold",
        contract_id="kalshi:KXMAXHOLD",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:max-hold",
        replay_sequence=first["event"]["sequence"],
        event_id="event-1",
        milestone_id="milestone-1",
        contract_id="kalshi:KXMAXHOLD",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=first["state_hash"],
        fee_hash=first["fee_hash"],
        features={"imbalance": 0.4},
    )
    fill = store.record_replay_observation(
        cohort_id="cohort:max-hold",
        contract_id="kalshi:KXMAXHOLD",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )
    assert (
        ledger.process_observation(replay_sequence=fill["event"]["sequence"])[0][
            "outcome"
        ]
        == "filled"
    )
    maximum_hold = store.record_replay_observation(
        cohort_id="cohort:max-hold",
        contract_id="kalshi:KXMAXHOLD",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=601),
        request_started_at=NOW + timedelta(seconds=600),
        received_at=NOW + timedelta(seconds=601),
    )

    transitions = ledger.process_observation(
        replay_sequence=maximum_hold["event"]["sequence"]
    )

    assert transitions[0]["outcome"] == "closed"
    assert (
        transitions[0]["payload"]["economics"]["exit_trigger"] == "max_hold_10_minutes"
    )
    assert ledger.snapshot()["counts"]["open"] == 0


def test_political_paper_process_observation_prioritizes_reviewed_event_boundary(
    tmp_path,
):
    """A sealed reviewed-event boundary closes an older position before max hold."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:boundary")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.55, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.44, 100]], "asks": [[0.60, 100]]},
    }
    reviewed_lock = {
        "event_id": "event-boundary",
        "milestone_id": "milestone-boundary",
        "event_start_at": (NOW - timedelta(minutes=1)).isoformat(),
        "event_end_at": (NOW + timedelta(seconds=4)).isoformat(),
        "selected_at": (NOW - timedelta(minutes=2)).isoformat(),
    }
    first = store.record_replay_observation(
        cohort_id="cohort:boundary",
        contract_id="kalshi:KXBOUNDARY",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="event_live",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
        reviewed_lock=reviewed_lock,
    )
    assert ledger.record_pending_signal(
        signal_id="signal:boundary",
        replay_sequence=first["event"]["sequence"],
        event_id="event-boundary",
        milestone_id="milestone-boundary",
        contract_id="kalshi:KXBOUNDARY",
        side="yes",
        base_lane="event_live",
        phase="event_live",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=first["state_hash"],
        fee_hash=first["fee_hash"],
        features={"imbalance": 0.4},
    )
    fill = store.record_replay_observation(
        cohort_id="cohort:boundary",
        contract_id="kalshi:KXBOUNDARY",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="event_live",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
        reviewed_lock=reviewed_lock,
    )
    assert (
        ledger.process_observation(replay_sequence=fill["event"]["sequence"])[0][
            "outcome"
        ]
        == "filled"
    )
    boundary = store.record_replay_observation(
        cohort_id="cohort:boundary",
        contract_id="kalshi:KXBOUNDARY",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="event_live",
        observed_at=NOW + timedelta(seconds=4),
        request_started_at=NOW + timedelta(seconds=3),
        received_at=NOW + timedelta(seconds=4),
        reviewed_lock=reviewed_lock,
    )

    transitions = ledger.process_observation(
        replay_sequence=boundary["event"]["sequence"]
    )

    assert transitions[0]["outcome"] == "closed"
    assert transitions[0]["payload"]["economics"]["exit_trigger"] == "event_boundary"
    assert ledger.snapshot()["counts"]["open"] == 0


@pytest.mark.parametrize(
    ("exit_book", "expected_trigger"),
    [
        (
            {
                "schema_version": 1,
                "yes": {"bids": [[0.30, 100]], "asks": [[0.32, 1000]]},
                "no": {"bids": [[0.67, 100]], "asks": [[0.69, 10]]},
            },
            "hard_stop_net_return_minus_0.05",
        ),
        (
            {
                "schema_version": 1,
                "yes": {"bids": [[0.45, 100]], "asks": [[0.46, 1000]]},
                "no": {"bids": [[0.53, 100]], "asks": [[0.54, 10]]},
            },
            "signal_reversal",
        ),
    ],
)
def test_political_paper_process_observation_applies_frozen_hard_stop_then_reversal(
    tmp_path, exit_book, expected_trigger
):
    """Sealed top-of-book loss precedes the frozen imbalance-reversal exit."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:auto-exit")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    entry_book = {
        "schema_version": 1,
        "yes": {"bids": [[0.39, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    first = store.record_replay_observation(
        cohort_id="cohort:auto-exit",
        contract_id="kalshi:KXAUTOEXIT",
        normalized_book=entry_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:auto-exit",
        replay_sequence=first["event"]["sequence"],
        event_id="event-auto-exit",
        milestone_id="milestone-auto-exit",
        contract_id="kalshi:KXAUTOEXIT",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=first["state_hash"],
        fee_hash=first["fee_hash"],
        features={"imbalance": 0.4},
    )
    fill = store.record_replay_observation(
        cohort_id="cohort:auto-exit",
        contract_id="kalshi:KXAUTOEXIT",
        normalized_book=entry_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )
    assert (
        ledger.process_observation(replay_sequence=fill["event"]["sequence"])[0][
            "outcome"
        ]
        == "filled"
    )
    exit_observation = store.record_replay_observation(
        cohort_id="cohort:auto-exit",
        contract_id="kalshi:KXAUTOEXIT",
        normalized_book=exit_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=4),
        request_started_at=NOW + timedelta(seconds=3),
        received_at=NOW + timedelta(seconds=4),
    )

    transitions = ledger.process_observation(
        replay_sequence=exit_observation["event"]["sequence"]
    )

    assert transitions[0]["outcome"] == "closed"
    assert transitions[0]["payload"]["economics"]["exit_trigger"] == expected_trigger
    assert ledger.snapshot()["counts"]["open"] == 0


def test_political_paper_partial_forced_exit_latches_liquidation_until_closed(tmp_path):
    """A neutral later book cannot cancel a partially executed hard-stop."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:sticky")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    entry_book = {
        "schema_version": 1,
        "yes": {"bids": [[0.39, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    first = store.record_replay_observation(
        cohort_id="cohort:sticky",
        contract_id="kalshi:KXSTICKY",
        normalized_book=entry_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:sticky",
        replay_sequence=first["event"]["sequence"],
        event_id="event-sticky",
        milestone_id="milestone-sticky",
        contract_id="kalshi:KXSTICKY",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=first["state_hash"],
        fee_hash=first["fee_hash"],
        features={"imbalance": 0.4},
    )
    fill = store.record_replay_observation(
        cohort_id="cohort:sticky",
        contract_id="kalshi:KXSTICKY",
        normalized_book=entry_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )
    assert (
        ledger.process_observation(replay_sequence=fill["event"]["sequence"])[0][
            "outcome"
        ]
        == "filled"
    )

    hard_stop = store.record_replay_observation(
        cohort_id="cohort:sticky",
        contract_id="kalshi:KXSTICKY",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.30, 40]], "asks": [[0.32, 1000]]},
            "no": {"bids": [[0.67, 100]], "asks": [[0.69, 10]]},
        },
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=4),
        request_started_at=NOW + timedelta(seconds=3),
        received_at=NOW + timedelta(seconds=4),
    )
    [partial] = ledger.process_observation(
        replay_sequence=hard_stop["event"]["sequence"]
    )
    assert partial["outcome"] == "partial"
    assert (
        partial["payload"]["economics"]["exit_trigger"]
        == "hard_stop_net_return_minus_0.05"
    )
    [open_position] = ledger.snapshot()["open_positions"]
    assert open_position["quantity"] == 6
    assert open_position["liquidation_trigger"] == "hard_stop_net_return_minus_0.05"

    neutral = store.record_replay_observation(
        cohort_id="cohort:sticky",
        contract_id="kalshi:KXSTICKY",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.55, 60]], "asks": [[0.56, 60]]},
            "no": {"bids": [[0.43, 100]], "asks": [[0.45, 100]]},
        },
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=6),
        request_started_at=NOW + timedelta(seconds=5),
        received_at=NOW + timedelta(seconds=6),
    )
    [closed] = ledger.process_observation(replay_sequence=neutral["event"]["sequence"])

    assert closed["outcome"] == "closed"
    assert (
        closed["payload"]["economics"]["exit_trigger"]
        == "hard_stop_net_return_minus_0.05"
    )
    assert ledger.snapshot()["counts"]["open"] == 0


def test_forced_exit_inside_minimum_hold_is_durable_and_retries_later(tmp_path):
    """A forced trigger before two seconds is evidence, not a worker exception."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:hold")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    entry_book = {
        "schema_version": 1,
        "yes": {"bids": [[0.39, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    signal = store.record_replay_observation(
        cohort_id="cohort:hold",
        contract_id="kalshi:KXHOLD",
        normalized_book=entry_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:hold",
        replay_sequence=signal["event"]["sequence"],
        event_id="event-hold",
        milestone_id="milestone-hold",
        contract_id="kalshi:KXHOLD",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=signal["state_hash"],
        fee_hash=signal["fee_hash"],
        features={"imbalance": 0.4},
    )
    fill = store.record_replay_observation(
        cohort_id="cohort:hold",
        contract_id="kalshi:KXHOLD",
        normalized_book=entry_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )
    assert (
        ledger.process_observation(replay_sequence=fill["event"]["sequence"])[0][
            "outcome"
        ]
        == "filled"
    )

    forced_book = {
        "schema_version": 1,
        "yes": {"bids": [[0.30, 100]], "asks": [[0.32, 1_000]]},
        "no": {"bids": [[0.67, 100]], "asks": [[0.69, 10]]},
    }
    early = store.record_replay_observation(
        cohort_id="cohort:hold",
        contract_id="kalshi:KXHOLD",
        normalized_book=forced_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=2),
        request_started_at=NOW + timedelta(seconds=1, milliseconds=500),
        received_at=NOW + timedelta(seconds=2),
    )
    [no_exit] = ledger.process_observation(replay_sequence=early["event"]["sequence"])
    assert no_exit["outcome"] == "no_exit"
    assert no_exit["reason"] == "minimum_hold_not_elapsed"
    assert no_exit["payload"]["exit_trigger"] == "hard_stop_net_return_minus_0.05"
    [position] = ledger.snapshot()["open_positions"]
    assert position["liquidation_trigger"] == "hard_stop_net_return_minus_0.05"

    later = store.record_replay_observation(
        cohort_id="cohort:hold",
        contract_id="kalshi:KXHOLD",
        normalized_book=entry_book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=4),
        request_started_at=NOW + timedelta(seconds=3),
        received_at=NOW + timedelta(seconds=4),
    )
    [closed] = ledger.process_observation(replay_sequence=later["event"]["sequence"])
    assert closed["outcome"] == "closed"
    assert closed["payload"]["economics"]["exit_trigger"] == (
        "hard_stop_net_return_minus_0.05"
    )


def test_forced_exit_with_stale_evidence_is_durable_no_exit_and_stays_latched(tmp_path):
    """A failed forced valuation cannot hide an open position or clear its trigger."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:exit-age")
    ledger.initialize(
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW,
        policy={
            "max_book_request_latency_seconds": "2",
            "max_fee_fetch_latency_seconds": "2",
            "max_fee_schedule_age_seconds": "60",
        },
    )
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.55, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    source = store.record_replay_observation(
        cohort_id="cohort:exit-age",
        contract_id="kalshi:KXEXITAGE",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:exit-age",
        replay_sequence=source["event"]["sequence"],
        event_id="event-exit-age",
        milestone_id="milestone-exit-age",
        contract_id="kalshi:KXEXITAGE",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=source["state_hash"],
        fee_hash=source["fee_hash"],
        features={"imbalance": 0.4},
    )
    fill = store.record_replay_observation(
        cohort_id="cohort:exit-age",
        contract_id="kalshi:KXEXITAGE",
        normalized_book=book,
        fee_schedule={
            **_authoritative_kalshi_fee(),
            "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
            "fetched_at": (NOW + timedelta(seconds=1)).isoformat(),
        },
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=900),
        received_at=NOW + timedelta(seconds=1),
        book_received_at=NOW + timedelta(seconds=1),
        fee_request_started_at=NOW + timedelta(milliseconds=900),
        fee_received_at=NOW + timedelta(seconds=1),
    )
    fill_transitions = ledger.process_observation(
        replay_sequence=fill["event"]["sequence"]
    )
    assert fill_transitions[0]["outcome"] == "filled", fill_transitions

    stale_exit = store.record_replay_observation(
        cohort_id="cohort:exit-age",
        contract_id="kalshi:KXEXITAGE",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW + timedelta(seconds=600),
        received_at=NOW + timedelta(seconds=601),
        book_received_at=NOW + timedelta(seconds=601),
        fee_request_started_at=NOW + timedelta(seconds=600),
        fee_received_at=NOW + timedelta(seconds=601),
    )
    [no_exit] = ledger.process_observation(
        replay_sequence=stale_exit["event"]["sequence"]
    )
    assert no_exit["outcome"] == "no_exit"
    assert no_exit["reason"] == "fee_metadata_stale"
    snapshot = ledger.snapshot()
    assert snapshot["valuation_complete"] is False
    assert snapshot["open_positions"][0]["liquidation_trigger"] == "max_hold_10_minutes"

    settled = store.record_replay_observation(
        cohort_id="cohort:exit-age",
        contract_id="kalshi:KXEXITAGE",
        normalized_book=book,
        fee_schedule={
            **_authoritative_kalshi_fee(),
            "observed_at": (NOW + timedelta(seconds=602)).isoformat(),
            "fetched_at": (NOW + timedelta(seconds=602)).isoformat(),
        },
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=602),
        request_started_at=NOW + timedelta(seconds=601),
        received_at=NOW + timedelta(seconds=602),
        book_received_at=NOW + timedelta(seconds=602),
        fee_request_started_at=NOW + timedelta(seconds=601),
        fee_received_at=NOW + timedelta(seconds=602),
    )
    [closed] = ledger.process_observation(replay_sequence=settled["event"]["sequence"])
    assert closed["outcome"] == "closed"
    assert ledger.snapshot()["valuation_complete"] is True


@pytest.mark.parametrize(
    ("book_delay", "fee_delay", "fee_age", "expected_reason"),
    [
        (timedelta(seconds=2), timedelta(seconds=2), timedelta(seconds=60), None),
        (
            timedelta(seconds=4, milliseconds=899),
            timedelta(seconds=2),
            timedelta(seconds=60),
            "book_request_too_slow",
        ),
        (
            timedelta(seconds=2),
            timedelta(seconds=4),
            timedelta(seconds=60),
            "fee_metadata_too_slow",
        ),
        (
            timedelta(seconds=2),
            timedelta(seconds=2),
            timedelta(seconds=301),
            "fee_metadata_stale",
        ),
    ],
)
def test_political_paper_rejects_slow_or_stale_replay_evidence(
    tmp_path, book_delay, fee_delay, fee_age, expected_reason
):
    """Typed policy timing limits gate fills; equality is admissible."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:timing")
    ledger.initialize(
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW,
        policy={
            "max_book_request_latency_seconds": "2",
            "max_fee_fetch_latency_seconds": "2",
            "max_fee_schedule_age_seconds": "60",
        },
    )
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.50, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }
    source = store.record_replay_observation(
        cohort_id="cohort:timing",
        contract_id="kalshi:KXTIMING",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:timing",
        replay_sequence=source["event"]["sequence"],
        event_id="event-1",
        milestone_id="milestone-1",
        contract_id="kalshi:KXTIMING",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(minutes=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=source["state_hash"],
        fee_hash=source["fee_hash"],
        features={"imbalance": 0.4},
    )
    request_started = NOW + timedelta(seconds=1)
    received_at = request_started + book_delay
    fee_observed_at = received_at - fee_age
    fee_schedule = {
        **_authoritative_kalshi_fee(),
        "observed_at": fee_observed_at.isoformat(),
        "fetched_at": (fee_observed_at + fee_delay).isoformat(),
    }
    later = store.record_replay_observation(
        cohort_id="cohort:timing",
        contract_id="kalshi:KXTIMING",
        normalized_book=book,
        fee_schedule=fee_schedule,
        lock_phase="hot",
        observed_at=received_at,
        request_started_at=request_started,
        received_at=received_at,
        book_received_at=received_at,
        fee_request_started_at=fee_observed_at,
        fee_received_at=fee_observed_at + fee_delay,
    )

    result = ledger._resolve_pending_signal(
        signal_id="signal:timing",
        replay_sequence=later["event"]["sequence"],
    )

    if expected_reason is None:
        assert result["outcome"] == "filled"
    else:
        assert result["outcome"] == "no_fill"
        assert result["reason"] == expected_reason
        assert ledger.snapshot()["account"]["cash_micros"] == 1_000_000_000


def test_political_paper_aggregates_only_whole_contract_depth_from_persisted_asks(
    tmp_path,
):
    """A causal entry may walk several canonical asks, never raw depth."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(
        store=store, cohort_id="cohort:multi-level"
    )
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {
            "bids": [[0.39, 100]],
            "asks": [[0.40, 50], [0.50, 100]],
        },
        "no": {"bids": [[0.49, 100]], "asks": [[0.61, 100]]},
    }
    signal_event = store.record_replay_observation(
        cohort_id="cohort:multi-level",
        contract_id="kalshi:KXMULTI",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:multi-level",
        replay_sequence=signal_event["event"]["sequence"],
        event_id="event-1",
        milestone_id="milestone-1",
        contract_id="kalshi:KXMULTI",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=signal_event["state_hash"],
        fee_hash=signal_event["fee_hash"],
        features={},
    )
    fill_event = store.record_replay_observation(
        cohort_id="cohort:multi-level",
        contract_id="kalshi:KXMULTI",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )

    result = ledger._resolve_pending_signal(
        signal_id="signal:multi-level",
        replay_sequence=fill_event["event"]["sequence"],
    )

    assert result["outcome"] == "filled"
    assert result["payload"]["quantity"] == 15
    # Ordinary-account cent rounding accumulates across the one simulated
    # order.  The second level rebates the cent carried by the first level;
    # entries and exits are separate orders and therefore reset this state.
    assert result["payload"]["debit_micros"] == 7_410_000
    assert result["payload"]["economics"]["levels"] == [
        {
            "displayed_ask": "0.4",
            "quantity": 5,
            "effective_price": "0.41",
            "raw_fee": "0.084665",
            "rounded_trade_fee": "0.0847",
            "balance_change_micros": -2_140_000,
            "ordinary_rounding_micros": 5_300,
            "rounding_accumulator_before_micros": 0,
            "rounding_rebate_micros": 0,
            "rounding_accumulator_after_micros": 5_300,
        },
        {
            "displayed_ask": "0.5",
            "quantity": 10,
            "effective_price": "0.51",
            "raw_fee": "0.17493",
            "rounded_trade_fee": "0.175",
            "balance_change_micros": -5_270_000,
            "ordinary_rounding_micros": 5_000,
            "rounding_accumulator_before_micros": 5_300,
            "rounding_rebate_micros": 10_000,
            "rounding_accumulator_after_micros": 300,
        },
    ]


def test_political_paper_fills_an_affordable_prefix_of_deeper_depth(tmp_path):
    """Control caps bound a book walk; they must not reject it wholesale."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:prefix")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.39, 100]], "asks": [[0.40, 1_000]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.61, 100]]},
    }
    source = store.record_replay_observation(
        cohort_id="cohort:prefix",
        contract_id="kalshi:KXPREFIX",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:prefix",
        replay_sequence=source["event"]["sequence"],
        event_id="event-prefix",
        milestone_id="milestone-prefix",
        contract_id="kalshi:KXPREFIX",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=source["state_hash"],
        fee_hash=source["fee_hash"],
        features={},
    )
    later = store.record_replay_observation(
        cohort_id="cohort:prefix",
        contract_id="kalshi:KXPREFIX",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=101),
        received_at=NOW + timedelta(seconds=1),
    )

    result = ledger._resolve_pending_signal(
        signal_id="signal:prefix",
        replay_sequence=later["event"]["sequence"],
    )

    assert result["outcome"] == "filled"
    assert result["payload"]["quantity"] == 58
    assert result["payload"]["debit_micros"] <= 25_000_000
    assert result["payload"]["economics"]["unconsumed_levels"] == [
        {
            "displayed_ask": "0.4",
            "eligible_quantity": 100,
            "unconsumed_quantity": 42,
        }
    ]


def test_political_paper_rejects_an_overlapping_later_request_as_a_named_no_fill(
    tmp_path,
):
    """A later sequence is insufficient when its request began before receipt."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:overlap")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.5, 100]], "asks": [[0.4, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.6, 100]]},
    }
    first = store.record_replay_observation(
        cohort_id="cohort:overlap",
        contract_id="kalshi:KXOVERLAP",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )
    assert ledger.record_pending_signal(
        signal_id="signal:overlap",
        replay_sequence=1,
        event_id="event-1",
        milestone_id="milestone-1",
        contract_id="kalshi:KXOVERLAP",
        side="yes",
        base_lane="hot_pre_event",
        phase="hot",
        signal_request_started_at=NOW,
        signal_received_at=NOW + timedelta(milliseconds=100),
        expires_at=NOW + timedelta(seconds=10),
        model_version="depth-imbalance-reaction-experimental-v1",
        config_hash="config-hash",
        state_hash=first["state_hash"],
        fee_hash=first["fee_hash"],
        features={},
    )
    second = store.record_replay_observation(
        cohort_id="cohort:overlap",
        contract_id="kalshi:KXOVERLAP",
        normalized_book=book,
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW + timedelta(seconds=1),
        request_started_at=NOW + timedelta(milliseconds=50),
        received_at=NOW + timedelta(seconds=1),
    )
    result = ledger._resolve_pending_signal(
        signal_id="signal:overlap",
        replay_sequence=second["event"]["sequence"],
    )
    assert result["outcome"] == "no_fill"
    assert result["reason"] == "request_not_strictly_after_signal_receipt"


def test_political_paper_scopes_base_lane_overlap_to_the_exact_occurrence(tmp_path):
    """Contracts are cohort-global while lanes are exclusive per milestone occurrence."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:overlap")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.50, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }

    def resolve(
        *,
        signal_id: str,
        event_id: str,
        milestone_id: str,
        contract_id: str,
        phase: str,
        started_at: datetime,
    ) -> dict:
        source = store.record_replay_observation(
            cohort_id="cohort:overlap",
            contract_id=contract_id,
            normalized_book=book,
            fee_schedule=_authoritative_kalshi_fee(),
            lock_phase=phase,
            observed_at=started_at,
            request_started_at=started_at,
            received_at=started_at + timedelta(milliseconds=100),
        )
        assert ledger.record_pending_signal(
            signal_id=signal_id,
            replay_sequence=source["event"]["sequence"],
            event_id=event_id,
            milestone_id=milestone_id,
            contract_id=contract_id,
            side="yes",
            base_lane=("hot_pre_event" if phase == "hot" else "event_live"),
            phase=phase,
            signal_request_started_at=started_at,
            signal_received_at=started_at + timedelta(milliseconds=100),
            expires_at=started_at + timedelta(seconds=10),
            model_version="depth-imbalance-reaction-experimental-v1",
            config_hash="config-hash",
            state_hash=source["state_hash"],
            fee_hash=source["fee_hash"],
            features={},
        )
        later = store.record_replay_observation(
            cohort_id="cohort:overlap",
            contract_id=contract_id,
            normalized_book=book,
            fee_schedule=_authoritative_kalshi_fee(),
            lock_phase=phase,
            observed_at=started_at + timedelta(seconds=1),
            request_started_at=started_at + timedelta(milliseconds=101),
            received_at=started_at + timedelta(seconds=1),
        )
        return ledger._resolve_pending_signal(
            signal_id=signal_id,
            replay_sequence=later["event"]["sequence"],
        )

    assert (
        resolve(
            signal_id="signal:first",
            event_id="event:one",
            milestone_id="milestone:one",
            contract_id="kalshi:KXONE",
            phase="hot",
            started_at=NOW,
        )["outcome"]
        == "filled"
    )
    assert (
        resolve(
            signal_id="signal:same-contract",
            event_id="event:two",
            milestone_id="milestone:two",
            contract_id="kalshi:KXONE",
            phase="event_live",
            started_at=NOW + timedelta(seconds=2),
        )["reason"]
        == "contract_overlap"
    )
    assert (
        resolve(
            signal_id="signal:same-event-different-occurrence",
            event_id="event:one",
            milestone_id="milestone:two",
            contract_id="kalshi:KXTWO",
            phase="hot",
            started_at=NOW + timedelta(seconds=4),
        )["outcome"]
        == "filled"
    )
    assert (
        resolve(
            signal_id="signal:same-occurrence-lane",
            event_id="event:three",
            milestone_id="milestone:one",
            contract_id="kalshi:KXFOUR",
            phase="hot",
            started_at=NOW + timedelta(seconds=6),
        )["reason"]
        == "base_lane_overlap"
    )
    assert (
        resolve(
            signal_id="signal:independent-event-lane",
            event_id="event:two",
            milestone_id="milestone:three",
            contract_id="kalshi:KXTHREE",
            phase="hot",
            started_at=NOW + timedelta(seconds=8),
        )["outcome"]
        == "filled"
    )


def test_political_paper_enforces_bound_reviewed_risk_group_caps(tmp_path):
    """Correlated reviewed events cannot consume the whole control envelope."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:risk")
    ledger.initialize(
        starting_cash_micros=1_000_000_000,
        initialized_at=NOW,
        policy={
            "max_total_reserved_micros": 100_000_000,
            "max_position_reserved_micros": 25_000_000,
            "max_open_positions": 4,
            "entry_depth_fraction": "0.10",
            "reviewed_risk_groups": [
                {
                    "risk_group_id": "trump-aug-10",
                    "reviewed_event_ids": ["event:trump-say", "event:trump-mention"],
                    "max_total_reserved_micros": 50_000_000,
                    "max_open_positions": 1,
                }
            ],
        },
    )
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.50, 100]], "asks": [[0.40, 100]]},
        "no": {"bids": [[0.59, 100]], "asks": [[0.60, 100]]},
    }

    def fill(event_id: str, contract_id: str, sequence_offset: int) -> dict:
        source_at = NOW + timedelta(seconds=sequence_offset)
        source = store.record_replay_observation(
            cohort_id="cohort:risk",
            contract_id=contract_id,
            normalized_book=book,
            fee_schedule=_authoritative_kalshi_fee(),
            lock_phase="hot",
            observed_at=source_at,
            request_started_at=source_at,
            received_at=source_at + timedelta(milliseconds=100),
        )
        signal_id = f"signal:{contract_id}"
        assert ledger.record_pending_signal(
            signal_id=signal_id,
            replay_sequence=source["event"]["sequence"],
            event_id=event_id,
            milestone_id=f"milestone:{contract_id}",
            contract_id=contract_id,
            side="yes",
            base_lane="hot_pre_event",
            phase="hot",
            signal_request_started_at=source_at,
            signal_received_at=source_at + timedelta(milliseconds=100),
            expires_at=source_at + timedelta(seconds=10),
            model_version="depth-imbalance-reaction-experimental-v1",
            config_hash="config-hash",
            state_hash=source["state_hash"],
            fee_hash=source["fee_hash"],
            features={},
        )
        later = store.record_replay_observation(
            cohort_id="cohort:risk",
            contract_id=contract_id,
            normalized_book=book,
            fee_schedule=_authoritative_kalshi_fee(),
            lock_phase="hot",
            observed_at=source_at + timedelta(seconds=1),
            request_started_at=source_at + timedelta(milliseconds=101),
            received_at=source_at + timedelta(seconds=1),
        )
        return ledger._resolve_pending_signal(
            signal_id=signal_id, replay_sequence=later["event"]["sequence"]
        )

    assert fill("event:trump-say", "kalshi:KXSAY", 0)["outcome"] == "filled"
    blocked = fill("event:trump-mention", "kalshi:KXMENTION", 2)
    assert blocked["outcome"] == "no_fill"
    assert blocked["reason"] == "risk_group_max_open_positions"
    position = store.political_experimental_positions(cohort_id="cohort:risk")[0]
    assert position["risk_group_id"] == "trump-aug-10"
    assert ledger.snapshot()["risk_group_exposure"] == [
        {
            "risk_group_id": "trump-aug-10",
            "open_positions": 1,
            "reserved_micros": position["cost_basis_micros"],
        }
    ]


def test_political_paper_records_a_missing_pending_signal_as_a_durable_no_fill(
    tmp_path,
):
    """An unknown signal is auditable without violating the fill-attempt FK."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    ledger = PoliticalExperimentalPaperLedger(store=store, cohort_id="cohort:missing")
    ledger.initialize(starting_cash_micros=1_000_000_000, initialized_at=NOW)
    event = store.record_replay_observation(
        cohort_id="cohort:missing",
        contract_id="kalshi:KXMISSING",
        normalized_book={
            "schema_version": 1,
            "yes": {"bids": [[0.5, 100]], "asks": [[0.4, 100]]},
            "no": {"bids": [[0.59, 100]], "asks": [[0.6, 100]]},
        },
        fee_schedule=_authoritative_kalshi_fee(),
        lock_phase="hot",
        observed_at=NOW,
        request_started_at=NOW,
        received_at=NOW + timedelta(milliseconds=100),
    )

    result = ledger._resolve_pending_signal(
        signal_id="signal:missing",
        replay_sequence=event["event"]["sequence"],
    )

    assert result["outcome"] == "no_fill"
    assert result["reason"] == "missing_signal"
    expected_attempts = [
        {
            "signal_id": "signal:missing",
            "replay_sequence": event["event"]["sequence"],
            "reason": "missing_signal",
            "attempted_at": (NOW + timedelta(milliseconds=100)).isoformat(),
            "payload": result["payload"],
        }
    ]
    assert (
        store.political_experimental_orphan_fill_attempts(cohort_id="cohort:missing")
        == expected_attempts
    )

    restarted = PoliticalExperimentalPaperLedger(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        cohort_id="cohort:missing",
    )
    retry = restarted._resolve_pending_signal(
        signal_id="signal:missing",
        replay_sequence=event["event"]["sequence"],
    )
    assert retry == {**result, "idempotent": True}
    assert (
        restarted.store.political_experimental_orphan_fill_attempts(
            cohort_id="cohort:missing"
        )
        == expected_attempts
    )


def test_replay_evidence_deduplicates_canonical_book_and_fee_payloads(tmp_path):
    """Replay storage retains normalized depth, never an adapter raw payload."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    # Hash-addressed payload writes are deliberately not public admission
    # APIs: only the transaction which also allocates an observation token may
    # make durable evidence visible to decisions.
    assert not hasattr(store, "record_normalized_book_state")
    assert not hasattr(store, "record_fee_schedule_payload")
    book = {
        "schema_version": 1,
        "yes": {
            "bids": [[0.61, 4], [0.60, 7]],
            "asks": [[0.62, 3], [0.63, 9]],
        },
        "no": {
            "bids": [[0.37, 9], [0.36, 3]],
            "asks": [[0.39, 7], [0.40, 4]],
        },
    }
    fee = {
        "schema_version": 1,
        "venue": "kalshi",
        "fee_type": "quadratic",
        "rate": "0.07",
        "exponent": "2",
        "multiplier": "1",
        "source": "official",
    }

    first_state = store._record_normalized_book_state(normalized_book=book)
    second_state = store._record_normalized_book_state(normalized_book=book)
    first_fee = store._record_fee_schedule_payload(fee_schedule=fee)
    second_fee = store._record_fee_schedule_payload(fee_schedule=fee)

    assert first_state == second_state
    assert first_fee == second_fee
    assert store.replay_book_state(first_state) == book
    assert store.replay_fee_schedule(first_fee) == fee
    counts = store.replay_evidence_counts()
    assert counts == {
        "book_states": 1,
        "fee_schedules": 1,
        "captured_bytes": counts["captured_bytes"],
        "store_bytes": counts["store_bytes"],
        "byte_cap": 4 * 1024**3,
    }
    assert counts["captured_bytes"] > 0
    assert counts["store_bytes"] >= counts["captured_bytes"]


@pytest.mark.parametrize(
    "level",
    (
        [True, 1],
        [0.40, True],
        [0.0, 1],
        [1.0, 1],
        [-0.01, 1],
        [0.40, 0],
        [0.40, -1],
        [float("inf"), 1],
        [0.40, float("nan")],
        ["0.40", 1],
    ),
)
def test_replay_book_validation_rejects_malformed_binary_levels_before_hashing(
    tmp_path, level
):
    """Only finite, non-boolean binary prices and positive numeric sizes hash."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    book = {
        "schema_version": 1,
        "yes": {"bids": [level], "asks": [[0.60, 1]]},
        "no": {"bids": [[0.40, 1]], "asks": [[0.60, 1]]},
    }

    with pytest.raises(ValueError):
        store._record_normalized_book_state(normalized_book=book)

    assert store.replay_evidence_counts()["book_states"] == 0


def test_replay_payload_decode_rejects_a_tampered_hash_addressed_book(tmp_path):
    """A row addressed by a digest cannot replay altered normalized depth."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.62, 3]]},
        "no": {"bids": [[0.37, 3]], "asks": [[0.39, 4]]},
    }
    state_hash = store._record_normalized_book_state(normalized_book=book)
    altered_book = {
        **book,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.01, 3]]},
    }
    altered_payload = zlib.compress(
        json.dumps(altered_book, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        level=9,
    )
    with store._connection:
        store._connection.execute(
            "UPDATE normalized_book_states SET compressed_payload = ? WHERE state_hash = ?",
            (altered_payload, state_hash),
        )

    with pytest.raises(ReplayEvidenceIntegrityError, match="digest"):
        store.replay_book_state(state_hash)


@pytest.mark.parametrize(
    ("table", "hash_column", "decode"),
    (
        ("normalized_book_states", "state_hash", "replay_book_state"),
        ("normalized_fee_schedules", "fee_hash", "replay_fee_schedule"),
    ),
)
def test_tampered_replay_payload_permanently_invalidates_every_referencing_cohort(
    tmp_path, table, hash_column, decode
):
    """Book and fee integrity failures invalidate every dependent cohort."""
    path = tmp_path / "opportunities.db"
    store = PlatformOpportunityStore(path)
    system = PlatformOpportunitySystem(store=store)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.62, 3]]},
        "no": {"bids": [[0.37, 3]], "asks": [[0.39, 4]]},
    }
    fee = {"schema_version": 1, "venue": "kalshi", "fee_type": "none"}
    for cohort_id in (system.cohort_id, "cohort:also-references-payload"):
        store.record_replay_observation(
            cohort_id=cohort_id,
            contract_id="kalshi:KXTEST",
            normalized_book=book,
            fee_schedule=fee,
            lock_phase="hot",
            observed_at=NOW,
            request_started_at=NOW,
            received_at=NOW,
        )

    event = store.replay_observation_events(cohort_id=system.cohort_id)[0]
    payload_hash = event[hash_column]
    with store._connection:
        store._connection.execute(
            f"UPDATE {table} SET compressed_payload = ? WHERE {hash_column} = ?",
            (zlib.compress(b'{"tampered":true}', level=9), payload_hash),
        )

    with pytest.raises(ReplayEvidenceIntegrityError):
        getattr(store, decode)(payload_hash)

    for cohort_id in (system.cohort_id, "cohort:also-references-payload"):
        assert store.replay_evidence_status(cohort_id=cohort_id) == {
            "cohort_valid": False,
            "degraded_reason": "replay_evidence_integrity_failure",
        }
    assert (
        "replay_evidence_invalid"
        in system.acceptance_report("depth_imbalance_reaction_experimental_v1").reasons
    )

    restarted = PlatformOpportunitySystem(store=PlatformOpportunityStore(path))
    assert (
        "replay_evidence_invalid"
        in restarted.acceptance_report(
            "depth_imbalance_reaction_experimental_v1"
        ).reasons
    )


def test_replay_evidence_byte_cap_rejects_atomically_and_invalidates_cohort(tmp_path):
    """Capacity loss is durable, visible, and cannot create a scored observation."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db", replay_byte_cap=1)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.62, 3]]},
        "no": {"bids": [[0.37, 3]], "asks": [[0.39, 4]]},
    }
    fee = {"schema_version": 1, "venue": "kalshi", "fee_type": "none"}

    with pytest.raises(ReplayEvidenceCapacityError, match="byte cap"):
        store.record_replay_observation(
            cohort_id="cohort:capped",
            contract_id="kalshi:KXTEST",
            normalized_book=book,
            fee_schedule=fee,
            lock_phase="hot",
            observed_at=NOW,
            request_started_at=NOW,
            received_at=NOW,
        )

    counts = store.replay_evidence_counts()
    assert counts == {
        "book_states": 0,
        "fee_schedules": 0,
        "captured_bytes": 0,
        "store_bytes": counts["store_bytes"],
        "byte_cap": 1,
    }
    assert store.replay_observation_events(cohort_id="cohort:capped") == []
    assert store.replay_evidence_status(cohort_id="cohort:capped") == {
        "cohort_valid": False,
        "degraded_reason": "replay_evidence_byte_cap_exceeded",
    }


def test_invalid_replay_cohort_cannot_count_or_score_later_observations(tmp_path):
    """A failed evidence cohort stays fail-closed even on direct system calls."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db", replay_byte_cap=1)
    system = PlatformOpportunitySystem(
        store=store,
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())

    with pytest.raises(ReplayEvidenceCapacityError, match="byte cap"):
        system.persist_replay_observation(
            "polymarket:p1",
            _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
            observed_at=NOW,
            request_started_at=NOW,
            received_at=NOW,
            fee_schedule=_zero_fee(),
        )

    system.record_successful_observation(
        "polymarket:p1", observed_at=NOW + timedelta(seconds=1)
    )
    assert (
        system._observe_book(
            "polymarket:p1",
            _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
            observed_at=NOW + timedelta(seconds=1),
        ).intents
        == ()
    )
    assert (
        system._observe_book(
            "polymarket:p1",
            _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
            observed_at=NOW + timedelta(seconds=6),
        ).intents
        == ()
    )
    assert system.store.observation_telemetry(cohort_id=system.cohort_id) == {}
    assert system.store.intent_rows(cohort_id=system.cohort_id) == []


def test_invalid_replay_cohort_fails_closed_in_acceptance_report(tmp_path):
    """A capacity-invalid replay chain cannot pass research acceptance."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db", replay_byte_cap=1)
    system = PlatformOpportunitySystem(store=store)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.62, 3]]},
        "no": {"bids": [[0.37, 3]], "asks": [[0.39, 4]]},
    }
    fee = {"schema_version": 1, "venue": "kalshi", "fee_type": "none"}

    with pytest.raises(ReplayEvidenceCapacityError, match="byte cap"):
        store.record_replay_observation(
            cohort_id=system.cohort_id,
            contract_id="kalshi:KXTEST",
            normalized_book=book,
            fee_schedule=fee,
            lock_phase="hot",
            observed_at=NOW,
            request_started_at=NOW,
            received_at=NOW,
        )

    report = system.acceptance_report("depth_imbalance_reaction_experimental_v1")

    assert report.research_threshold_passed is False
    assert "replay_evidence_invalid" in report.reasons


def test_replay_sequence_allocation_is_unique_and_ordered_across_connections(tmp_path):
    """Two independent SQLite connections receive unique durable tokens."""
    path = tmp_path / "opportunities.db"
    cohort_id = "cohort:concurrent-sequences"
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.62, 3]]},
        "no": {"bids": [[0.37, 3]], "asks": [[0.39, 4]]},
    }
    fee = {"schema_version": 1, "venue": "kalshi", "fee_type": "none"}

    def persist(index: int) -> int:
        store = PlatformOpportunityStore(path)
        try:
            return int(
                store.record_replay_observation(
                    cohort_id=cohort_id,
                    contract_id=f"kalshi:KXTEST-{index}",
                    normalized_book=book,
                    fee_schedule=fee,
                    lock_phase="hot",
                    observed_at=NOW,
                    request_started_at=NOW,
                    received_at=NOW,
                )["event"]["sequence"]
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(persist, range(32)))

    assert sorted(sequences) == list(range(1, 33))
    reopened = PlatformOpportunityStore(path)
    try:
        assert [
            event["sequence"]
            for event in reopened.replay_observation_events(cohort_id=cohort_id)
        ] == list(range(1, 33))
    finally:
        reopened.close()


def test_replay_evidence_cap_accounts_for_sqlite_store_and_wal_bytes(tmp_path):
    """The configured cap is physical store capacity, not payload-only capacity."""
    path = tmp_path / "opportunities.db"
    # The normalized payload itself is well below 10 KiB. A payload-only cap
    # would accept it, while the initialized SQLite/WAL store already exceeds
    # this physical budget and must fail closed.
    physical_cap = 10 * 1024
    store = PlatformOpportunityStore(path, replay_byte_cap=physical_cap)
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.62, 3]]},
        "no": {"bids": [[0.37, 3]], "asks": [[0.39, 4]]},
    }
    fee = {"schema_version": 1, "venue": "kalshi", "fee_type": "none"}

    with pytest.raises(ReplayEvidenceCapacityError, match="byte cap"):
        store.record_replay_observation(
            cohort_id="cohort:physical-cap",
            contract_id="kalshi:KXTEST",
            normalized_book=book,
            fee_schedule=fee,
            lock_phase="hot",
            observed_at=NOW,
            request_started_at=NOW,
            received_at=NOW,
        )

    counts = store.replay_evidence_counts()
    assert counts["store_bytes"] > physical_cap
    assert counts["captured_bytes"] == 0
    assert store.replay_evidence_status(cohort_id="cohort:physical-cap") == {
        "cohort_valid": False,
        "degraded_reason": "replay_evidence_byte_cap_exceeded",
    }


def test_replay_observation_events_are_ordered_and_cover_fee_phase_heartbeats(
    tmp_path,
):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    book = {
        "schema_version": 1,
        "yes": {"bids": [[0.61, 4]], "asks": [[0.62, 3]]},
        "no": {"bids": [[0.37, 3]], "asks": [[0.39, 4]]},
    }
    fee = {"schema_version": 1, "venue": "kalshi", "fee_type": "none"}
    common = {
        "cohort_id": "cohort:test",
        "contract_id": "kalshi:KXTEST",
        "fee_schedule": fee,
        "lock_phase": "hot",
        "request_started_at": NOW,
    }

    store.record_replay_observation(
        normalized_book=book, observed_at=NOW, received_at=NOW, **common
    )
    store.record_replay_observation(
        normalized_book=book,
        observed_at=NOW + timedelta(seconds=10),
        received_at=NOW + timedelta(seconds=10),
        **common,
    )
    store.record_replay_observation(
        normalized_book=book,
        observed_at=NOW + timedelta(seconds=31),
        received_at=NOW + timedelta(seconds=31),
        **common,
    )
    changed_book = {**book, "yes": {"bids": [[0.60, 4]], "asks": [[0.62, 3]]}}
    store.record_replay_observation(
        normalized_book=changed_book,
        observed_at=NOW + timedelta(seconds=32),
        received_at=NOW + timedelta(seconds=32),
        **common,
    )
    changed_fee = {**fee, "fee_type": "quadratic"}
    store.record_replay_observation(
        normalized_book=changed_book,
        fee_schedule=changed_fee,
        observed_at=NOW + timedelta(seconds=33),
        received_at=NOW + timedelta(seconds=33),
        **{key: value for key, value in common.items() if key != "fee_schedule"},
    )
    store.record_replay_observation(
        normalized_book=changed_book,
        fee_schedule=changed_fee,
        lock_phase="event_live",
        observed_at=NOW + timedelta(seconds=34),
        received_at=NOW + timedelta(seconds=34),
        **{
            key: value
            for key, value in common.items()
            if key not in {"fee_schedule", "lock_phase"}
        },
    )

    events = store.replay_observation_events(cohort_id="cohort:test")
    assert [
        (
            event["sequence"],
            event["kind"],
            event["lock_phase"],
            event["state_hash"],
            event["fee_hash"],
        )
        for event in events
    ] == [
        (1, "change", "hot", events[0]["state_hash"], events[0]["fee_hash"]),
        (2, "heartbeat", "hot", events[0]["state_hash"], events[0]["fee_hash"]),
        (3, "heartbeat", "hot", events[0]["state_hash"], events[0]["fee_hash"]),
        (4, "change", "hot", events[3]["state_hash"], events[0]["fee_hash"]),
        (5, "change", "hot", events[3]["state_hash"], events[4]["fee_hash"]),
        (
            6,
            "change",
            "event_live",
            events[3]["state_hash"],
            events[4]["fee_hash"],
        ),
    ]
    assert (
        store.observation_telemetry(cohort_id="cohort:test")["kalshi:KXTEST"][
            "observation_count"
        ]
        == 6
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
    assert second.venue_coverage == {
        "polymarket": {
            "status": "complete",
            "reason": "complete",
            "incoming": 2,
            "effective": 2,
            "retained": 2,
            "unchanged": 2,
            "added": 0,
            "updated": 0,
            "retired": 0,
            "replaced": 0,
        },
        "kalshi": {
            "status": "complete",
            "reason": "complete",
            "incoming": 1,
            "effective": 1,
            "retained": 1,
            "unchanged": 1,
            "added": 0,
            "updated": 0,
            "retired": 0,
            "replaced": 0,
        },
    }
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


def test_repeated_partial_catalogs_retire_stale_structural_relations(tmp_path):
    """Partial cohorts replace live relations instead of accumulating old ones."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    close = NOW + timedelta(minutes=30)

    for index, threshold in enumerate((3, 5, 7)):
        refresh = system.refresh_catalog(
            polymarket_markets=[
                _poly(
                    f"p{index}-low",
                    f"Will August CPI be above {threshold}%?",
                    end_date=close,
                ),
                _poly(
                    f"p{index}-high",
                    f"Will August CPI be above {threshold + 1}%?",
                    end_date=close,
                ),
            ],
            kalshi_markets=[],
            snapshot_complete=False,
            observed_at=NOW + timedelta(minutes=index),
        )

        assert refresh.catalog_contracts == 2
        assert store.summary()["relations"] == 1


def test_venue_scoped_coverage_retains_only_failed_venue_cohort(tmp_path):
    """A source failure must not retire its last-good rows or stale healthy rows."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    close = NOW + timedelta(minutes=30)
    system.refresh_catalog(
        polymarket_markets=[_poly("p-old", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[_kalshi("k-old", "CPI above 3%", close_time=close)],
        venue_coverage={
            "polymarket": {"status": "complete", "reason": "complete"},
            "kalshi": {"status": "complete", "reason": "complete"},
        },
        observed_at=NOW,
    )

    kalshi_replaced = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[_kalshi("k-new", "CPI above 4%", close_time=close)],
        venue_coverage={
            "polymarket": {"status": "failure", "reason": "network_error"},
            "kalshi": {"status": "complete", "reason": "complete"},
        },
        observed_at=NOW + timedelta(minutes=1),
    )
    assert set(store.current_contract_payloads_for_venue("polymarket")) == {
        "polymarket:p-old"
    }
    assert set(store.current_contract_payloads_for_venue("kalshi")) == {"kalshi:k-new"}
    assert kalshi_replaced.venue_coverage["polymarket"] == {
        "status": "failure",
        "reason": "network_error",
        "incoming": 0,
        "effective": 1,
        "retained": 1,
        "unchanged": 1,
        "added": 0,
        "updated": 0,
        "retired": 0,
        "replaced": 0,
    }

    poly_replaced = system.refresh_catalog(
        polymarket_markets=[_poly("p-new", "Will CPI be above 5%?", end_date=close)],
        kalshi_markets=[],
        venue_coverage={
            "polymarket": {"status": "partial", "reason": "page_limit"},
            "kalshi": {"status": "failure", "reason": "network_error"},
        },
        observed_at=NOW + timedelta(minutes=2),
    )
    assert set(store.current_contract_payloads_for_venue("polymarket")) == {
        "polymarket:p-new"
    }
    assert set(store.current_contract_payloads_for_venue("kalshi")) == {"kalshi:k-new"}
    assert poly_replaced.venue_coverage["polymarket"]["replaced"] == 1
    assert poly_replaced.venue_coverage["kalshi"]["retained"] == 1
    assert store.catalog_counts() == {"current": 2, "revisions": 4}


def test_catalog_coverage_names_deterministic_budgets_bounded(tmp_path):
    """Budgeted source cohorts replace; a transport failure retains last good."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(store=store)
    close = NOW + timedelta(minutes=30)

    system.refresh_catalog(
        polymarket_markets=[_poly("p-old", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    bounded = system.refresh_catalog(
        polymarket_markets=[_poly("p-new", "Will CPI be above 4%?", end_date=close)],
        kalshi_markets=[],
        venue_coverage={
            "polymarket": {"status": "bounded", "reason": "page_budget"},
            "kalshi": {"status": "complete", "reason": "source_exhausted"},
        },
        observed_at=NOW + timedelta(minutes=1),
    )

    assert bounded.venue_coverage["polymarket"]["status"] == "bounded"
    assert bounded.venue_coverage["polymarket"]["reason"] == "page_budget"
    assert set(store.current_contract_payloads_for_venue("polymarket")) == {
        "polymarket:p-new"
    }

    retained = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[],
        venue_coverage={
            "polymarket": {"status": "failure", "reason": "decode_error"},
            "kalshi": {"status": "complete", "reason": "source_exhausted"},
        },
        observed_at=NOW + timedelta(minutes=2),
    )
    assert retained.venue_coverage["polymarket"]["status"] == "failure"
    assert set(store.current_contract_payloads_for_venue("polymarket")) == {
        "polymarket:p-new"
    }


def test_partial_catalog_rehydrates_active_political_lock_after_restart(tmp_path):
    path = tmp_path / "opportunities.db"
    occurrence = NOW + timedelta(hours=2)
    original = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path),
        political_watch_policy=PoliticalWatchPolicy(max_events=1),
    )
    original.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXELECTION-26AUG-T1",
                "Will the President win the election?",
                event_ticker="KXELECTION-26AUG",
            )
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="election-primary",
                title="President election",
                category="Politics",
                milestone_type="election",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=45),
                related_event_tickers=("KXELECTION-26AUG",),
                primary_event_tickers=("KXELECTION-26AUG",),
                source_id="reviewed-calendar",
            )
        ],
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
        "kalshi:KXELECTION-26AUG-T1"
    ]
    # A restored active lock is part of the effective monitoring cohort.  The
    # durable catalog must agree with the returned catalog/monitoring plan.
    assert resumed_store.catalog_counts() == {"current": 1, "revisions": 1}


def test_selected_political_event_survives_refresh_volume_displacement_until_cooldown(
    tmp_path,
):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=2,
        cooldown_after=timedelta(hours=2),
    )
    system = PlatformOpportunitySystem(store=store, political_watch_policy=policy)
    occurrence = NOW + timedelta(minutes=30)

    first = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXELECTION-26AUG-T1",
                "Will the President win the election?",
                event_ticker="KXELECTION-26AUG",
                volume=100,
            ),
            _kalshi(
                "KXAPPROVAL-26AUG-T1",
                "Will presidential approval exceed 50%?",
                event_ticker="KXAPPROVAL-26AUG",
                volume=50,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "election-primary",
                "President election",
                "Politics",
                "election",
                occurrence,
                occurrence + timedelta(minutes=45),
                ("KXELECTION-26AUG",),
                ("KXELECTION-26AUG",),
                "reviewed-calendar",
            ),
            KalshiMilestone(
                "approval-primary",
                "President approval",
                "Politics",
                "approval",
                occurrence,
                occurrence + timedelta(minutes=45),
                ("KXAPPROVAL-26AUG",),
                ("KXAPPROVAL-26AUG",),
                "reviewed-calendar",
            ),
        ],
        observed_at=NOW,
    )
    assert [item.contract_id for item in first.monitoring.hot] == [
        "kalshi:KXELECTION-26AUG-T1"
    ]

    displaced = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXELECTION-26AUG-T1",
                "Will the President win the election?",
                event_ticker="KXELECTION-26AUG",
                volume=1,
            ),
            _kalshi(
                "KXAPPROVAL-26AUG-T1",
                "Will presidential approval exceed 50%?",
                event_ticker="KXAPPROVAL-26AUG",
                volume=100_000,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "election-primary",
                "President election",
                "Politics",
                "election",
                occurrence,
                occurrence + timedelta(minutes=45),
                ("KXELECTION-26AUG",),
                ("KXELECTION-26AUG",),
                "reviewed-calendar",
            ),
            KalshiMilestone(
                "approval-primary",
                "President approval",
                "Politics",
                "approval",
                occurrence,
                occurrence + timedelta(minutes=45),
                ("KXAPPROVAL-26AUG",),
                ("KXAPPROVAL-26AUG",),
                "reviewed-calendar",
            ),
        ],
        observed_at=NOW + timedelta(minutes=1),
    )

    assert [item.contract_id for item in displaced.monitoring.hot] == [
        "kalshi:KXELECTION-26AUG-T1"
    ]
    locks = store.active_political_event_locks(now=NOW + timedelta(minutes=1))
    assert [lock["event_id"] for lock in locks] == ["KXELECTION-26AUG"]


def test_automatic_political_locks_dedupe_a_shared_reviewed_milestone(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        political_watch_policy=PoliticalWatchPolicy(
            max_events=2, max_contracts_per_event=1
        ),
    )
    occurrence = NOW + timedelta(hours=4)
    tomorrow = NOW + timedelta(days=1)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXSCRSENS-26-A",
                "South Carolina senate primary",
                event_ticker="KXSCRSENS-26",
                volume=1_000,
            ),
            _kalshi(
                "KXDERIVATIVE-26-A",
                "South Carolina senate derivative",
                event_ticker="KXDERIVATIVE-26",
                volume=2_000,
            ),
            _kalshi(
                "KXTOMORROW-26-A",
                "President speech tomorrow",
                event_ticker="KXTOMORROW-26",
                volume=100,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "shared-sc-primary",
                "South Carolina Republican Senate primary",
                "Politics",
                "political_race",
                occurrence,
                occurrence + timedelta(hours=24),
                ("KXSCRSENS-26", "KXDERIVATIVE-26"),
                ("KXSCRSENS-26", "KXDERIVATIVE-26"),
                "reviewed-calendar",
            ),
            KalshiMilestone(
                "tomorrow-speech",
                "President remarks",
                "Politics",
                "speech",
                tomorrow,
                tomorrow + timedelta(minutes=45),
                ("KXTOMORROW-26",),
                ("KXTOMORROW-26",),
                "reviewed-calendar",
            ),
        ],
        observed_at=NOW,
    )

    locks = store.active_political_event_locks(now=NOW)
    assert len(locks) == 2
    assert (
        sum(
            lock["selected_contract"]["milestone_id"] == "shared-sc-primary"
            for lock in locks
        )
        == 1
    )
    assert {lock["event_id"] for lock in locks} & {"KXTOMORROW-26"}


def test_reviewed_pin_consumes_shared_milestone_before_automatic_selection(tmp_path):
    """A reviewed pin and its derivative cannot lock one occurrence twice."""
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        political_watch_policy=PoliticalWatchPolicy(
            max_events=2,
            max_contracts_per_event=1,
            reviewed_pinned_event_ids=("kalshi:KXPINNED-26",),
        ),
    )
    occurrence = NOW + timedelta(hours=4)
    tomorrow = NOW + timedelta(days=1)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXPINNED-26-A",
                "Reviewed political event",
                event_ticker="KXPINNED-26",
                volume=1,
            ),
            _kalshi(
                "KXPINNED-DERIVATIVE-26-A",
                "Derivative of reviewed event",
                event_ticker="KXPINNED-DERIVATIVE-26",
                volume=1_000_000,
            ),
            _kalshi(
                "KXOTHER-26-A",
                "Separate political event",
                event_ticker="KXOTHER-26",
                volume=100,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "reviewed-occurrence",
                "Reviewed occurrence",
                "Politics",
                "speech",
                occurrence,
                occurrence + timedelta(minutes=45),
                ("KXPINNED-26", "KXPINNED-DERIVATIVE-26"),
                ("KXPINNED-26", "KXPINNED-DERIVATIVE-26"),
                "reviewed-calendar",
            ),
            KalshiMilestone(
                "separate-occurrence",
                "Separate occurrence",
                "Politics",
                "speech",
                tomorrow,
                tomorrow + timedelta(minutes=45),
                ("KXOTHER-26",),
                ("KXOTHER-26",),
                "reviewed-calendar",
            ),
        ],
        observed_at=NOW,
    )

    assert {
        lock["event_id"] for lock in store.active_political_event_locks(now=NOW)
    } == {"KXPINNED-26", "KXOTHER-26"}


def test_retained_lock_consumes_shared_milestone_after_restart(tmp_path):
    """A restarted worker seeds automatic selection from active lock evidence."""
    database = tmp_path / "opportunities.db"
    occurrence = NOW + timedelta(days=2)
    first = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(database),
        political_watch_policy=PoliticalWatchPolicy(
            max_events=1, max_contracts_per_event=1
        ),
    )
    first.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXRETAINED-26-A",
                "Retained political event",
                event_ticker="KXRETAINED-26",
            )
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "retained-occurrence",
                "Retained occurrence",
                "Politics",
                "speech",
                occurrence,
                occurrence + timedelta(minutes=45),
                ("KXRETAINED-26",),
                ("KXRETAINED-26",),
                "reviewed-calendar",
            )
        ],
        observed_at=NOW,
    )

    restarted = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(database),
        political_watch_policy=PoliticalWatchPolicy(
            max_events=2, max_contracts_per_event=1
        ),
    )
    restarted.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXRETAINED-DERIVATIVE-26-A",
                "Retained event derivative",
                event_ticker="KXRETAINED-DERIVATIVE-26",
                volume=1_000_000,
            ),
            _kalshi(
                "KXRESTART-OTHER-26-A",
                "President's separate restarted remarks",
                event_ticker="KXRESTART-OTHER-26",
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "retained-occurrence",
                "Retained occurrence",
                "Politics",
                "speech",
                occurrence,
                occurrence + timedelta(minutes=45),
                ("KXRETAINED-DERIVATIVE-26",),
                ("KXRETAINED-DERIVATIVE-26",),
                "reviewed-calendar",
            ),
            KalshiMilestone(
                "restarted-separate-occurrence",
                "Separate restarted occurrence",
                "Politics",
                "speech",
                occurrence + timedelta(hours=2),
                occurrence + timedelta(hours=2, minutes=45),
                ("KXRESTART-OTHER-26",),
                ("KXRESTART-OTHER-26",),
                "reviewed-calendar",
            ),
        ],
        observed_at=NOW + timedelta(minutes=1),
    )

    locked_event_ids = {
        lock["event_id"]
        for lock in restarted.store.active_political_event_locks(now=NOW)
    }
    assert locked_event_ids == {"KXRETAINED-26", "KXRESTART-OTHER-26"}, (
        locked_event_ids,
        restarted._political_locks,
    )


def test_week_long_live_political_milestone_cannot_displace_short_horizon_event(
    tmp_path,
):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        political_watch_policy=PoliticalWatchPolicy(
            max_events=1,
            max_contracts_per_event=1,
            max_event_duration=timedelta(hours=30),
        ),
    )
    tomorrow = NOW + timedelta(days=1)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXWEEK-26-A",
                "Tennessee election",
                event_ticker="KXWEEK-26",
                volume=1_000_000,
            ),
            _kalshi(
                "KXTOMORROW-26-A",
                "President speech tomorrow",
                event_ticker="KXTOMORROW-26",
                volume=10,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "week-long",
                "Tennessee election",
                "Politics",
                "political_race",
                NOW - timedelta(hours=1),
                NOW + timedelta(days=6),
                ("KXWEEK-26",),
                ("KXWEEK-26",),
                "reviewed-calendar",
            ),
            KalshiMilestone(
                "tomorrow-speech",
                "President remarks",
                "Politics",
                "speech",
                tomorrow,
                tomorrow + timedelta(minutes=45),
                ("KXTOMORROW-26",),
                ("KXTOMORROW-26",),
                "reviewed-calendar",
            ),
        ],
        observed_at=NOW,
    )

    assert [
        lock["event_id"] for lock in store.active_political_event_locks(now=NOW)
    ] == ["KXTOMORROW-26"]


def test_reviewed_pinned_kalshi_event_beats_automatic_volume_ranking(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=2,
        reviewed_pinned_event_ids=("kalshi:KXTRUMPMENTION-26AUG10",),
    )
    system = PlatformOpportunitySystem(store=store, political_watch_policy=policy)
    occurrence = NOW + timedelta(hours=2)

    refresh = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXTRUMPMENTION-26AUG10-T1",
                "Will Trump mention tariffs?",
                event_ticker="KXTRUMPMENTION-26AUG10",
                volume=1,
            ),
            _kalshi(
                "KXTRUMPRALLY-26AUG10-T1",
                "Will Trump hold a rally?",
                event_ticker="KXTRUMPRALLY-26AUG10",
                volume=1_000_000,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="mention",
                title="Trump remarks",
                category="Politics",
                milestone_type="speech",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=45),
                related_event_tickers=("KXTRUMPMENTION-26AUG10",),
                primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
                source_id="briefing",
            ),
            KalshiMilestone(
                milestone_id="rally",
                title="Trump rally",
                category="Politics",
                milestone_type="speech",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=45),
                related_event_tickers=("KXTRUMPRALLY-26AUG10",),
                primary_event_tickers=("KXTRUMPRALLY-26AUG10",),
                source_id="briefing",
            ),
        ],
        observed_at=NOW,
    )

    assert [
        item.contract_id
        for item in refresh.monitoring.warm
        if item.reason == "political_event_lock"
    ] == ["kalshi:KXTRUMPMENTION-26AUG10-T1"]
    assert [
        lock["event_id"] for lock in store.active_political_event_locks(now=NOW)
    ] == ["KXTRUMPMENTION-26AUG10"]


def test_automatic_political_selection_prefers_nearest_event_before_volume(tmp_path):
    """A far, liquid event cannot displace tomorrow's reviewed occurrence."""
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(
            max_events=1,
            max_contracts_per_event=1,
            lookahead=timedelta(days=7),
        ),
    )
    tomorrow = NOW + timedelta(days=1)
    far = NOW + timedelta(days=6, hours=23)

    refresh = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXTOMORROW-26AUG-T1",
                "Will the President speak tomorrow?",
                event_ticker="KXTOMORROW-26AUG",
                volume=100,
            ),
            _kalshi(
                "KXFAR-26AUG-T1",
                "Will the President speak next week?",
                event_ticker="KXFAR-26AUG",
                volume=1_000_000,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "tomorrow-speech",
                "President remarks tomorrow",
                "Politics",
                "political_speech",
                tomorrow,
                tomorrow + timedelta(minutes=30),
                ("KXTOMORROW-26AUG",),
                ("KXTOMORROW-26AUG",),
                "reviewed-calendar",
            ),
            KalshiMilestone(
                "far-speech",
                "President remarks next week",
                "Politics",
                "political_speech",
                far,
                far + timedelta(minutes=30),
                ("KXFAR-26AUG",),
                ("KXFAR-26AUG",),
                "reviewed-calendar",
            ),
        ],
        observed_at=NOW,
    )

    assert [
        item.contract_id
        for item in refresh.monitoring.warm
        if item.reason == "political_event_lock"
    ] == ["kalshi:KXTOMORROW-26AUG-T1"]
    assert [
        lock["event_id"] for lock in system.store.active_political_event_locks(now=NOW)
    ] == ["KXTOMORROW-26AUG"]


def test_automatic_political_selection_prefers_explicit_one_off_type_over_volume(
    tmp_path,
):
    """Equal-window generic metadata cannot out-rank a reviewed speech."""
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(
            max_events=1,
            max_contracts_per_event=1,
        ),
    )
    occurrence = NOW + timedelta(days=1)

    refresh = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            _kalshi(
                "KXGENERIC-26AUG-T1",
                "Will the President make an announcement?",
                event_ticker="KXGENERIC-26AUG",
                volume=1_000_000,
            ),
            _kalshi(
                "KXSPEECH-26AUG-T1",
                "Will the President mention immigration?",
                event_ticker="KXSPEECH-26AUG",
                volume=1,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                "generic-event",
                "Presidential announcement",
                "Politics",
                "event",
                occurrence,
                occurrence + timedelta(minutes=30),
                ("KXGENERIC-26AUG",),
                ("KXGENERIC-26AUG",),
                "official-schedule",
            ),
            KalshiMilestone(
                "explicit-speech",
                "Presidential remarks",
                "Politics",
                "political_speech",
                occurrence,
                occurrence + timedelta(minutes=30),
                ("KXSPEECH-26AUG",),
                ("KXSPEECH-26AUG",),
                "official-schedule",
            ),
        ],
        observed_at=NOW,
    )

    assert [
        item.contract_id
        for item in refresh.monitoring.warm
        if item.reason == "political_event_lock"
    ] == ["kalshi:KXSPEECH-26AUG-T1"]
    assert [
        lock["event_id"] for lock in system.store.active_political_event_locks(now=NOW)
    ] == ["KXSPEECH-26AUG"]


def test_normalized_milestone_metadata_is_separator_equivalent_and_stable():
    """Reviewed one-off types must not vary with Python hash seed ordering."""
    assert {
        _normalized_milestone_metadata(value)
        for value in ("press conference", "press-conference", "press_conference")
    } == {"press_conference"}


def test_polymarket_settlement_metadata_never_creates_a_political_lock(tmp_path):
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

    refresh = system.refresh_catalog(
        polymarket_markets=[market], kalshi_markets=[], observed_at=NOW
    )

    assert system.store.active_political_event_locks(now=NOW) == []
    assert system.dashboard_summary()["political_event_locks"] == []
    assert (
        any(item.reason == "political_event_lock" for item in refresh.monitoring.warm)
        is False
    )


def test_polymarket_end_date_is_close_metadata_not_occurrence_evidence(tmp_path):
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
    assert contract.close_time == close
    assert contract.occurrence_at is None
    assert contract.occurrence_evidence == "unknown"
    assert contract.occurrence_sources == ()
    assert contract.event_start_at is None
    assert contract.event_end_at is None
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
    assert contract.event_start_at == occurrence
    assert contract.event_end_at == occurrence + timedelta(minutes=45)
    assert contract.milestone_id == "milestone-mention"
    assert contract.milestone_category == "Politics"
    assert contract.milestone_type == "political_speech"
    assert contract.milestone_source_id == "kalshi"
    assert contract.milestone_relationship_role == "primary"
    assert contract.catalyst_at == occurrence
    locks = system.store.active_political_event_locks(now=NOW)
    assert [lock["event_id"] for lock in locks] == ["KXTRUMPMENTION-26AUG10"]
    assert locks[0]["event_start_at"] == occurrence.isoformat()
    assert locks[0]["event_end_at"] == (occurrence + timedelta(minutes=45)).isoformat()
    assert (
        locks[0]["locked_until"]
        == (occurrence + timedelta(hours=2, minutes=45)).isoformat()
    )


def test_political_locks_require_token_bound_political_venue_provenance(tmp_path):
    """Generic regulatory approval must not enter the political watchlist."""
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(max_events=3),
    )
    occurrence = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[
            KalshiMarket(
                ticker="KXFDA-APPROVAL-A",
                event_ticker="KXFDA-APPROVAL",
                series_ticker="KXFDAAPPROVAL",
                title="Will the FDA approve the drug?",
                event_title="FDA drug approval decision",
                category="Health",
                close_time=NOW + timedelta(days=30),
                volume=10_000,
                open_interest=5_000,
            ),
            KalshiMarket(
                ticker="KXPRES-APPROVAL-A",
                event_ticker="KXPRES-APPROVAL",
                series_ticker="KXPRESAPPROVAL",
                title="Will presidential approval exceed 50%?",
                event_title="President approval rating",
                category="Politics",
                close_time=NOW + timedelta(days=30),
                volume=1_000,
                open_interest=500,
            ),
            KalshiMarket(
                ticker="KXBOARD-DISAPPROVAL-A",
                event_ticker="KXBOARD-DISAPPROVAL",
                series_ticker="KXBOARD",
                title="Will board disapproval exceed 50%?",
                event_title="Corporate board vote",
                category="Business",
                close_time=NOW + timedelta(days=30),
                volume=8_000,
                open_interest=4_000,
            ),
            KalshiMarket(
                ticker="KXSERIES-GENERIC-A",
                event_ticker="KXSERIES-GENERIC",
                series_ticker="POLITICS-2026",
                title="Will the named outcome occur?",
                event_title="Scheduled market event",
                category="General",
                close_time=NOW + timedelta(days=30),
                volume=500,
                open_interest=250,
            ),
        ],
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="fda-approval",
                title="FDA decision",
                category="Health",
                milestone_type="regulatory_decision",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=30),
                related_event_tickers=("KXFDA-APPROVAL",),
                primary_event_tickers=("KXFDA-APPROVAL",),
            ),
            KalshiMilestone(
                milestone_id="pres-approval",
                title="President approval",
                category="Politics",
                milestone_type="polling_release",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=30),
                related_event_tickers=("KXPRES-APPROVAL",),
                primary_event_tickers=("KXPRES-APPROVAL",),
            ),
            KalshiMilestone(
                milestone_id="board-disapproval",
                title="Board vote",
                category="Business",
                milestone_type="corporate_event",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=30),
                related_event_tickers=("KXBOARD-DISAPPROVAL",),
                primary_event_tickers=("KXBOARD-DISAPPROVAL",),
            ),
            KalshiMilestone(
                milestone_id="series-generic",
                title="Scheduled event",
                category="General",
                milestone_type="event",
                start_time=occurrence,
                end_time=occurrence + timedelta(minutes=30),
                related_event_tickers=("KXSERIES-GENERIC",),
                primary_event_tickers=("KXSERIES-GENERIC",),
            ),
        ],
        observed_at=NOW,
    )

    assert [
        lock["event_id"] for lock in system.store.active_political_event_locks(now=NOW)
    ] == [
        "KXPRES-APPROVAL",
        "KXSERIES-GENERIC",
    ]


def test_political_lock_uses_milestone_end_for_live_event_cadence(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(
            max_events=1, max_contracts_per_event=1
        ),
    )
    start = datetime(2026, 8, 10, 22, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 10, 23, 15, tzinfo=timezone.utc)
    observed = start - timedelta(hours=2)
    market = _kalshi(
        "KXTRUMPMENTION-26AUG10-A",
        "Will Trump mention immigration?",
        event_ticker="KXTRUMPMENTION-26AUG10",
    )
    milestone = KalshiMilestone(
        milestone_id="mention-2026-08-10",
        title="Trump remarks",
        category="Politics",
        milestone_type="political_speech",
        start_time=start,
        end_time=end,
        related_event_tickers=("KXTRUMPMENTION-26AUG10",),
        primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
        source_id="kalshi-milestones",
    )

    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[market],
        kalshi_milestones=[milestone],
        observed_at=observed,
    )
    live = system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[market],
        kalshi_milestones=[milestone],
        observed_at=start + timedelta(minutes=30),
    ).monitoring
    assert [(item.cadence, item.interval_seconds) for item in live.hot] == [
        ("event_live", 2.0)
    ]
    locks = system.store.active_political_event_locks(now=observed)
    assert locks[0]["event_start_at"] == "2026-08-10T22:30:00+00:00"
    assert locks[0]["event_end_at"] == "2026-08-10T23:15:00+00:00"
    assert locks[0]["locked_until"] == "2026-08-11T01:15:00+00:00"


def test_political_lock_monitoring_uses_public_lifecycle_states_and_expires(tmp_path):
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=1,
        warm_before=timedelta(hours=4),
        hot_before=timedelta(hours=1),
        cooldown_after=timedelta(hours=2),
    )
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=policy,
    )
    start = NOW + timedelta(hours=2)
    end = start + timedelta(minutes=45)
    market = _kalshi(
        "KXTRUMPMENTION-26AUG10-A",
        "Will Trump mention immigration?",
        event_ticker="KXTRUMPMENTION-26AUG10",
    )
    milestone = KalshiMilestone(
        milestone_id="mention-2026-08-10",
        title="Trump remarks",
        category="Politics",
        milestone_type="political_speech",
        start_time=start,
        end_time=end,
        related_event_tickers=("KXTRUMPMENTION-26AUG10",),
        primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
        source_id="kalshi-milestones",
    )
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[market],
        kalshi_milestones=[milestone],
        observed_at=NOW,
    )

    def cadences(at: datetime) -> list[str]:
        monitoring = system.plan_monitoring(at)
        return [item.cadence for item in (*monitoring.warm, *monitoring.hot)]

    assert cadences(NOW) == ["warm"]
    assert cadences(start - timedelta(minutes=30)) == ["hot"]
    assert cadences(start + timedelta(minutes=1)) == ["event_live"]
    assert cadences(end + timedelta(minutes=1)) == ["cooldown"]
    assert cadences(end + policy.cooldown_after + timedelta(microseconds=1)) == []


def test_political_reaction_signals_are_limited_to_hot_and_event_live_phases(tmp_path):
    """Warm books establish a baseline; cooldown/expired books cannot enter."""
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=1,
        warm_before=timedelta(hours=4),
        hot_before=timedelta(hours=1),
        cooldown_after=timedelta(hours=2),
    )
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=policy,
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    start = NOW + timedelta(hours=2)
    end = start + timedelta(minutes=45)
    market = _kalshi(
        "KXTRUMPMENTION-26AUG10-A",
        "Will Trump mention immigration?",
        event_ticker="KXTRUMPMENTION-26AUG10",
    )
    milestone = KalshiMilestone(
        milestone_id="mention-2026-08-10",
        title="Trump remarks",
        category="Politics",
        milestone_type="political_speech",
        start_time=start,
        end_time=end,
        related_event_tickers=("KXTRUMPMENTION-26AUG10",),
        primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
        source_id="kalshi-milestones",
    )
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[market],
        kalshi_milestones=[milestone],
        observed_at=NOW,
    )
    contract_id = "kalshi:KXTRUMPMENTION-26AUG10-A"
    system.set_fee_schedule(contract_id, _zero_fee("kalshi"))

    # Warm samples become the baseline but are not allowed to create signals.
    system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    assert (
        system._observe_book(
            contract_id,
            _book(market.ticker, bid=0.51, ask=0.53, bid_size=300, ask_size=50),
            observed_at=NOW + timedelta(seconds=5),
        ).intents
        == ()
    )

    hot = system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.53, ask=0.55, bid_size=300, ask_size=50),
        observed_at=start - timedelta(minutes=30),
    )
    assert [intent.direction for intent in hot.intents] == ["yes"]
    assert hot.intents[0].expires_at == start - timedelta(minutes=30) + timedelta(
        seconds=10
    )

    live = system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.49, ask=0.51, bid_size=50, ask_size=300),
        observed_at=start + timedelta(minutes=1),
    )
    assert [intent.direction for intent in live.intents] == ["no"]
    assert live.intents[0].expires_at == start + timedelta(minutes=1, seconds=10)

    # A reaction near the reviewed event end cannot spill into cooldown,
    # even though the configured signal TTL would otherwise extend past it.
    system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=end - timedelta(seconds=6),
    )
    ending = system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=end - timedelta(seconds=5),
    )
    assert [intent.direction for intent in ending.intents] == ["yes"]
    assert ending.intents[0].expires_at == end

    cooldown = system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.53, ask=0.55, bid_size=300, ask_size=50),
        observed_at=end + timedelta(minutes=1),
    )
    expired = system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.49, ask=0.51, bid_size=50, ask_size=300),
        observed_at=end + policy.cooldown_after + timedelta(seconds=1),
    )
    assert cooldown.intents == ()
    assert expired.intents == ()


def test_political_replay_context_uses_sealed_reviewed_lock_provenance(tmp_path):
    """Later lock replacement cannot rewrite a token's event attribution."""
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=1,
        warm_before=timedelta(hours=4),
        hot_before=timedelta(hours=1),
    )
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=policy,
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    start = NOW + timedelta(hours=2)
    end = start + timedelta(minutes=45)
    market = _kalshi(
        "KXTRUMPMENTION-26AUG10-A",
        "Will Trump mention immigration?",
        event_ticker="KXTRUMPMENTION-26AUG10",
    )
    milestone = KalshiMilestone(
        milestone_id="mention-2026-08-10",
        title="Trump remarks",
        category="Politics",
        milestone_type="political_speech",
        start_time=start,
        end_time=end,
        related_event_tickers=("KXTRUMPMENTION-26AUG10",),
        primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
        source_id="kalshi-milestones",
    )
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[market],
        kalshi_milestones=[milestone],
        observed_at=NOW,
    )
    contract_id = "kalshi:KXTRUMPMENTION-26AUG10-A"
    hot = start - timedelta(minutes=30)
    system.set_fee_schedule(contract_id, _zero_fee("kalshi"))
    replay = system.persist_replay_observation(
        contract_id,
        _book(market.ticker, bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=hot,
        request_started_at=hot - timedelta(milliseconds=100),
        received_at=hot,
        fee_schedule=_zero_fee("kalshi"),
    )
    sequence = int(replay["event"]["sequence"])

    # A future occurrence lane can rebuild only its own durable backlog after
    # a lost wakeup, without reading the mutable reviewed-lock catalog.
    assert system.store.unprocessed_replay_observation_sequences_for_occurrence(
        cohort_id=system.cohort_id,
        milestone_id="mention-2026-08-10",
    ) == [sequence]
    system.store.record_replay_processing_receipt(
        cohort_id=system.cohort_id,
        sequence=sequence,
        completed_at=hot,
    )
    assert (
        system.store.unprocessed_replay_observation_sequences_for_occurrence(
            cohort_id=system.cohort_id,
            milestone_id="mention-2026-08-10",
        )
        == []
    )

    # Simulate a later catalog refresh retiring/replacing the live lock.  The
    # token must retain the lock that was already durable at read time.
    system._political_locks.clear()
    system.store._connection.execute("DELETE FROM political_event_locks")
    system.store._connection.commit()

    token = ReplayObservationToken(system.cohort_id, sequence)
    # The future dispatcher can route this token without consulting the now
    # replaced catalog lock.  A milestone, rather than an event ticker, is
    # the occurrence identity shared by derivative contracts.
    assert system.political_replay_route(token) == ReplayOccurrenceRoute(
        cohort_id=system.cohort_id,
        event_id="KXTRUMPMENTION-26AUG10",
        milestone_id="mention-2026-08-10",
    )
    assert system.political_replay_route(token).lane_key == (
        "occurrence:mention-2026-08-10"
    )

    context = system.political_replay_context(token)

    assert context == {
        "event_id": "KXTRUMPMENTION-26AUG10",
        "milestone_id": "mention-2026-08-10",
        "contract_id": contract_id,
        "phase": "hot",
        "base_lane": "hot_pre_event",
        "request_started_at": (hot - timedelta(milliseconds=100)).isoformat(),
        "received_at": hot.isoformat(),
        "state_hash": replay["state_hash"],
        "fee_hash": replay["fee_hash"],
        "event_start_at": start.isoformat(),
        "event_end_at": end.isoformat(),
        "lock_selected_at": NOW.isoformat(),
    }


def test_public_decision_boundary_rejects_unpersisted_raw_books(tmp_path):
    """Only a durable replay token may enter the political decision path."""
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(max_events=1),
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )

    with pytest.raises(AttributeError):
        system.observe_book(  # type: ignore[attr-defined]
            "kalshi:KXTRUMPMENTION-26AUG10-A",
            _book("KXTRUMPMENTION-26AUG10-A", bid=0.49, ask=0.51),
            observed_at=NOW,
        )

    assert system.store.replay_observation_events(cohort_id=system.cohort_id) == []
    assert system.store.intent_rows(cohort_id=system.cohort_id) == []


def test_political_reaction_rearms_after_typed_short_interval_and_restart(tmp_path):
    """Political causal signals do not inherit the legacy ten-minute cooldown."""
    path = tmp_path / "opportunities.db"
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=1,
        warm_before=timedelta(hours=4),
        hot_before=timedelta(hours=1),
    )
    start = NOW + timedelta(hours=2)
    end = start + timedelta(minutes=45)
    market = _kalshi(
        "KXTRUMPMENTION-26AUG10-A",
        "Will Trump mention immigration?",
        event_ticker="KXTRUMPMENTION-26AUG10",
    )
    milestone = KalshiMilestone(
        milestone_id="mention-2026-08-10",
        title="Trump remarks",
        category="Politics",
        milestone_type="political_speech",
        start_time=start,
        end_time=end,
        related_event_tickers=("KXTRUMPMENTION-26AUG10",),
        primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
        source_id="kalshi-milestones",
    )

    def system_for(store: PlatformOpportunityStore) -> PlatformOpportunitySystem:
        system = PlatformOpportunitySystem(
            store=store,
            political_watch_policy=policy,
            political_signal_rearm=timedelta(seconds=3),
            lane_authorities=_lane_authorities(
                "depth_imbalance_reaction_experimental_v1"
            ),
        )
        system.refresh_catalog(
            polymarket_markets=[],
            kalshi_markets=[market],
            kalshi_milestones=[milestone],
            observed_at=NOW,
        )
        system.set_fee_schedule("kalshi:KXTRUMPMENTION-26AUG10-A", _zero_fee("kalshi"))
        return system

    contract_id = "kalshi:KXTRUMPMENTION-26AUG10-A"
    system = system_for(PlatformOpportunityStore(path))
    changed_rearm = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "changed-rearm.db"),
        political_watch_policy=policy,
        political_signal_rearm=timedelta(seconds=4),
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    assert changed_rearm.cohort_id != system.cohort_id
    hot = start - timedelta(minutes=30)
    system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    first = system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.53, ask=0.55, bid_size=300, ask_size=50),
        observed_at=hot,
    )
    assert [intent.direction for intent in first.intents] == ["yes"]
    suppressed = system._observe_book(
        contract_id,
        _book(market.ticker, bid=0.55, ask=0.57, bid_size=300, ask_size=50),
        observed_at=hot + timedelta(seconds=2),
    )
    assert suppressed.intents == ()

    restarted = system_for(PlatformOpportunityStore(path))
    restarted._observe_book(
        contract_id,
        _book(market.ticker, bid=0.55, ask=0.57, bid_size=300, ask_size=50),
        observed_at=hot + timedelta(seconds=5, milliseconds=900),
    )
    rearmed = restarted._observe_book(
        contract_id,
        _book(market.ticker, bid=0.57, ask=0.59, bid_size=300, ask_size=50),
        observed_at=hot + timedelta(seconds=6),
    )
    assert [intent.direction for intent in rearmed.intents] == ["yes"]


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
    assert contract.occurrence_sources == ("kalshi.milestone:speech-a:start_date",)


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


def test_kalshi_political_lock_requires_valid_primary_end_bounded_milestone(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        political_watch_policy=PoliticalWatchPolicy(max_events=3),
    )
    start = NOW + timedelta(hours=1)
    markets = [
        _kalshi(
            "KXRELATED-26-A",
            "Will the President mention immigration?",
            event_ticker="KXRELATED-26",
        ),
        _kalshi(
            "KXOPEN-26-A",
            "Will the President mention immigration?",
            event_ticker="KXOPEN-26",
        ),
        _kalshi(
            "KXINVALID-26-A",
            "Will the President mention immigration?",
            event_ticker="KXINVALID-26",
        ),
    ]
    system.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=markets,
        kalshi_milestones=[
            KalshiMilestone(
                milestone_id="related-only",
                title="President remarks",
                category="Politics",
                milestone_type="speech",
                start_time=start,
                end_time=start + timedelta(minutes=30),
                related_event_tickers=("KXRELATED-26",),
                primary_event_tickers=(),
            ),
            KalshiMilestone(
                milestone_id="open-ended",
                title="President remarks",
                category="Politics",
                milestone_type="speech",
                start_time=start,
                end_time=None,
                related_event_tickers=("KXOPEN-26",),
                primary_event_tickers=("KXOPEN-26",),
            ),
            KalshiMilestone(
                milestone_id="end-before-start",
                title="President remarks",
                category="Politics",
                milestone_type="speech",
                start_time=start,
                end_time=start - timedelta(minutes=1),
                related_event_tickers=("KXINVALID-26",),
                primary_event_tickers=("KXINVALID-26",),
            ),
        ],
        observed_at=NOW,
    )

    related = next(
        contract for contract in system.contracts if contract.event_id == "KXRELATED-26"
    )
    assert related.milestone_relationship_role == "related"
    assert system.store.active_political_event_locks(now=NOW) == []


def test_exact_primary_milestone_already_live_locks_and_restores_after_restart(
    tmp_path,
):
    db_path = tmp_path / "opportunities.db"
    policy = PoliticalWatchPolicy(
        max_events=1,
        max_contracts_per_event=1,
        reviewed_pinned_event_ids=("kalshi:KXTRUMPSAY-26AUG10",),
    )
    start = NOW - timedelta(minutes=15)
    end = NOW + timedelta(minutes=30)
    market = _kalshi(
        "KXTRUMPSAY-26AUG10-A",
        "Will Trump say immigration?",
        event_ticker="KXTRUMPSAY-26AUG10",
    )
    milestone = KalshiMilestone(
        milestone_id="trump-say-live",
        title="Trump remarks",
        category="Politics",
        milestone_type="speech",
        start_time=start,
        end_time=end,
        related_event_tickers=("KXTRUMPSAY-26AUG10",),
        primary_event_tickers=("KXTRUMPSAY-26AUG10",),
    )
    original = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(db_path), political_watch_policy=policy
    )
    first = original.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[market],
        kalshi_milestones=[milestone],
        observed_at=NOW,
    ).monitoring
    assert [(item.reason, item.cadence) for item in first.hot] == [
        ("political_event_lock", "event_live")
    ]

    resumed = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(db_path), political_watch_policy=policy
    )
    restored = resumed.refresh_catalog(
        polymarket_markets=[],
        kalshi_markets=[],
        kalshi_milestones=[],
        observed_at=NOW + timedelta(minutes=1),
    ).monitoring
    assert [(item.contract_id, item.cadence) for item in restored.hot] == [
        ("kalshi:KXTRUMPSAY-26AUG10-A", "event_live")
    ]

    [lock] = resumed.dashboard_summary()["political_event_locks"]
    assert lock["selected_at"] == NOW.isoformat()
    assert lock["selected_contract"] == {
        "contract_id": "kalshi:KXTRUMPSAY-26AUG10-A",
        "milestone_id": "trump-say-live",
        "milestone_category": "Politics",
        "milestone_type": "speech",
        "milestone_source_id": None,
        "milestone_relationship_role": "primary",
        "milestone_provenance": "kalshi.milestone:trump-say-live:source_id=",
    }


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


def test_structural_relation_is_retained_when_sampling_capacity_excludes_a_leg(
    tmp_path,
):
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


def test_disabled_directional_lane_collects_features_without_creating_an_intent(
    tmp_path,
):
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
    system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    result = system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert result.intents == ()
    assert len(system._features["polymarket:p1"]) == 2
    assert system.dashboard_summary()["strategy_lanes"][
        "depth_imbalance_reaction_experimental_v1"
    ] == {"authority": "disabled"}


def test_directional_intent_is_immutable_and_scored_only_from_later_books(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    system = PlatformOpportunitySystem(
        store=store,
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())

    assert (
        system._observe_book(
            "polymarket:p1",
            _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
            observed_at=NOW,
        ).intents
        == ()
    )
    emitted = system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert len(emitted.intents) == 1
    intent = emitted.intents[0]
    assert intent.lane == "depth_imbalance_reaction_experimental_v1"
    assert intent.direction == "yes"
    assert intent.entry_price == 0.53
    assert intent.model_version == "microstructure-baseline-v1"
    assert (
        store.mark_rows(
            lane="depth_imbalance_reaction_experimental_v1", horizon_seconds=30
        )
        == []
    )

    scored = system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.57, ask=0.59, bid_size=200, ask_size=100),
        observed_at=NOW + timedelta(seconds=36),
    )

    marks = [mark for mark in scored.marks if mark.horizon_seconds == 30]
    assert {mark.capacity_fraction for mark in marks} == {0.05, 0.1, 0.2, 1.0}
    assert all(mark.net_return is not None and mark.net_return > 0 for mark in marks)
    assert len(store.intent_rows(lane="depth_imbalance_reaction_experimental_v1")) == 1

    exited = system._observe_book(
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
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    close = NOW + timedelta(minutes=30)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    result = system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert result.intents == ()


def test_zero_momentum_imbalance_does_not_emit_directional_no_intent(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())

    system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    result = system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=20, ask_size=300),
        observed_at=NOW + timedelta(seconds=5),
    )

    assert result.intents == ()
    assert (
        system.store.intent_rows(lane="depth_imbalance_reaction_experimental_v1") == []
    )


def test_directional_intent_uses_signed_composite_not_momentum_alone(tmp_path):
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())

    system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    emitted = system._observe_book(
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
        store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    close = NOW + timedelta(hours=2)
    system.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=NOW,
    )
    system.set_fee_schedule("polymarket:p1", _zero_fee())
    system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=NOW,
    )
    emitted = system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=NOW + timedelta(seconds=5),
    )
    assert len(emitted.intents) == 1

    scored = system._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.57, ask=0.59, bid_size=0, ask_size=250),
        observed_at=NOW + timedelta(seconds=36),
    )

    marks = [mark for mark in scored.marks if mark.horizon_seconds == 30]
    assert len(marks) == 4
    assert {mark.reason for mark in marks} == {"insufficient_later_executable_depth"}
    assert all(mark.max_notional == 0 for mark in marks)
    assert all(mark.net_return is None for mark in marks)


def test_relative_value_lane_is_disabled_before_residual_signal_can_create_intent(
    tmp_path,
):
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
    system.discover_structural_relations(observed_at=NOW)
    system.set_fee_schedule("polymarket:p3", _zero_fee())
    system.set_fee_schedule("polymarket:p4", _zero_fee())

    # Establish the observed structural residual, then compress it sharply.
    system._observe_book(
        "polymarket:p3",
        _book("p3", bid=0.59, ask=0.61, bid_size=200, ask_size=200),
        observed_at=NOW,
    )
    system._observe_book(
        "polymarket:p4",
        _book("p4", bid=0.49, ask=0.51, bid_size=200, ask_size=200),
        observed_at=NOW,
    )
    system._observe_book(
        "polymarket:p3",
        _book("p3", bid=0.60, ask=0.62, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=2),
    )
    system._observe_book(
        "polymarket:p4",
        _book("p4", bid=0.50, ask=0.52, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=2),
    )
    system._observe_book(
        "polymarket:p3",
        _book("p3", bid=0.55, ask=0.57, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=4),
    )
    emitted = system._observe_book(
        "polymarket:p4",
        _book("p4", bid=0.52, ask=0.54, bid_size=200, ask_size=200),
        observed_at=NOW + timedelta(seconds=4),
    )

    relative = [intent for intent in emitted.intents if intent.lane == "relative_value"]
    assert relative == []
    assert system.dashboard_summary()["strategy_lanes"]["relative_value"] == {
        "authority": "disabled"
    }


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

    report = system.acceptance_report("depth_imbalance_reaction_experimental_v1")

    assert report.lane == "depth_imbalance_reaction_experimental_v1"
    assert "fewer_than_50_event_clusters" in report.reasons
    assert "fewer_than_200_intents" in report.reasons
    assert report.research_threshold_passed is False
    assert report.execution_authority == "none"


def test_legacy_directional_reaction_configuration_cannot_join_v2_lane(tmp_path):
    """Old records remain queryable, but old lane config cannot emit into v2."""
    with pytest.raises(ValueError, match="unknown strategy lanes"):
        PlatformOpportunitySystem(
            store=PlatformOpportunityStore(tmp_path / "opportunities.db"),
            lane_authorities={"directional_reaction": "forward_only_unvalidated"},
        )


def test_restart_restores_open_intent_marks_and_cooldown(tmp_path):
    path = tmp_path / "opportunities.db"
    restart_now = datetime.now(timezone.utc)
    close = restart_now + timedelta(hours=2)
    first_store = PlatformOpportunityStore(path)
    first = PlatformOpportunitySystem(
        store=first_store,
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    first.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=restart_now,
    )
    first.set_fee_schedule("polymarket:p1", _zero_fee())
    first._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        observed_at=restart_now,
    )
    emitted = first._observe_book(
        "polymarket:p1",
        _book("p1", bid=0.51, ask=0.53, bid_size=300, ask_size=50),
        observed_at=restart_now + timedelta(seconds=5),
    )
    assert len(emitted.intents) == 1
    first_store.close()

    second_store = PlatformOpportunityStore(path)
    second = PlatformOpportunitySystem(
        store=second_store,
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    second.refresh_catalog(
        polymarket_markets=[_poly("p1", "Will CPI be above 3%?", end_date=close)],
        kalshi_markets=[],
        observed_at=restart_now + timedelta(seconds=10),
    )
    second.set_fee_schedule("polymarket:p1", _zero_fee())
    scored = second._observe_book(
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
    assert (
        changed.acceptance_report("depth_imbalance_reaction_experimental_v1").intents
        == 0
    )


def test_cohort_identity_changes_for_monitoring_political_and_replay_policy(tmp_path):
    """A decision-changing restart cannot append evidence to an old cohort."""
    path = tmp_path / "opportunities.db"
    baseline = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path, replay_byte_cap=4 * 1024**3),
        monitoring_policy=MonitoringPolicy(max_hot_contracts=100),
        political_watch_policy=PoliticalWatchPolicy(
            reviewed_pinned_event_ids=("kalshi:KXTRUMPMENTION-26AUG10",),
        ),
    )
    changed_monitoring = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path, replay_byte_cap=4 * 1024**3),
        monitoring_policy=MonitoringPolicy(max_hot_contracts=99),
        political_watch_policy=baseline.political_watch_policy,
    )
    changed_political = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path, replay_byte_cap=4 * 1024**3),
        monitoring_policy=baseline.monitoring_policy,
        political_watch_policy=PoliticalWatchPolicy(
            reviewed_pinned_event_ids=("kalshi:KXSCRSENS-26",),
        ),
    )
    changed_replay_limit = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(path, replay_byte_cap=3 * 1024**3),
        monitoring_policy=baseline.monitoring_policy,
        political_watch_policy=baseline.political_watch_policy,
    )

    assert (
        len(
            {
                baseline.cohort_id,
                changed_monitoring.cohort_id,
                changed_political.cohort_id,
                changed_replay_limit.cohort_id,
            }
        )
        == 4
    )


def test_dashboard_summary_and_research_pnl_do_not_mix_cohorts(tmp_path):
    store = PlatformOpportunityStore(tmp_path / "opportunities.db")
    close = NOW + timedelta(hours=2)
    first = PlatformOpportunitySystem(
        store=store,
        experiment_id="first",
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
    second = PlatformOpportunitySystem(
        store=store,
        experiment_id="second",
        lane_authorities=_lane_authorities("depth_imbalance_reaction_experimental_v1"),
    )
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
        system._observe_book(
            contract_id,
            _book(market_id, bid=0.49, ask=0.51, bid_size=100, ask_size=100),
            observed_at=NOW,
        )
        assert (
            len(
                system._observe_book(
                    contract_id,
                    _book(market_id, bid=0.51, ask=0.53, bid_size=300, ask_size=50),
                    observed_at=NOW + timedelta(seconds=5),
                ).intents
            )
            == 1
        )

    first._observe_book(
        "polymarket:first",
        _book("first", bid=0.57, ask=0.59, bid_size=200, ask_size=100),
        observed_at=NOW + timedelta(seconds=36),
    )

    first_dashboard = first.dashboard_summary()
    second_dashboard = second.dashboard_summary()
    assert first_dashboard["intents"] == {"depth_imbalance_reaction_experimental_v1": 1}
    assert second_dashboard["intents"] == {
        "depth_imbalance_reaction_experimental_v1": 1
    }
    assert first_dashboard["marks"] == 4
    assert second_dashboard["marks"] == 0
    assert first_dashboard["research_pnl"]["horizons"]["30"]["marks"] == 1
    assert second_dashboard["research_pnl"]["horizons"] == {}
