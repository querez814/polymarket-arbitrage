from core.cross_platform_arb import CrossPlatformOpportunity, MarketPair
from core.paper_locked_arb import PaperLockedArbitrageLedger
from utils.paper_trade_store import PaperTradeStore
import pytest
from datetime import datetime, timedelta, timezone


def _opportunity(pair_id: str = "poly-1") -> CrossPlatformOpportunity:
    return CrossPlatformOpportunity(
        opportunity_id="opp-1",
        market_pair=MarketPair(
            polymarket_id=pair_id,
            kalshi_ticker="KX-1",
            polymarket_question="Equivalent event?",
            kalshi_title="Equivalent event?",
            similarity_score=1.0,
        ),
        buy_platform="polymarket",
        sell_platform="kalshi",
        token="YES",
        buy_price=0.40,
        sell_price=0.48,
        gross_edge=0.08,
        net_edge=0.05,
        edge_pct=0.125,
        suggested_size=100.0,
        max_size=100.0,
        buy_liquidity=100.0,
        sell_liquidity=80.0,
    )


def test_shadow_trade_requires_confirmation_and_never_recycles_capital():
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=2,
        slippage_buffer_per_contract=0.02,
        liquidity_fraction=0.10,
        min_effective_edge=0.01,
    )
    opportunity = _opportunity()

    assert ledger.observe(opportunity) is None
    trade = ledger.observe(opportunity)

    assert trade is not None
    assert trade.contracts == 8.0
    assert trade.committed_capital == pytest.approx(7.76)
    assert trade.projected_locked_pnl == pytest.approx(0.24)
    assert ledger.observe(opportunity) is None
    assert ledger.summary()["capital_recycled"] is False
    assert ledger.summary()["decision_counts"] == {
        "awaiting_confirmation": 1,
        "pair_cooldown_active": 1,
        "paper_trade_recorded": 1,
    }


@pytest.mark.parametrize("required_observations", [float("nan"), 1.5, True])
def test_shadow_ledger_requires_an_integer_confirmation_count(required_observations):
    with pytest.raises(ValueError, match="required_observations"):
        PaperLockedArbitrageLedger(
            initial_balance=1_000.0,
            max_plan_capital=100.0,
            required_observations=required_observations,
        )


def test_shadow_ledger_cannot_commit_more_than_fixed_bankroll():
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=1_000.0,
        required_observations=1,
        slippage_buffer_per_contract=0.0,
        liquidity_fraction=1.0,
        min_effective_edge=0.01,
    )

    for index in range(20):
        ledger.observe(_opportunity(pair_id=f"poly-{index}"))

    assert ledger.committed_capital <= 1_000.0
    assert ledger.available_capital >= 0.0


def test_shadow_ledger_caps_cumulative_capital_for_one_pair():
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)

    def clock():
        return now

    opportunity = _opportunity()
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        max_pair_capital=15.0,
        max_total_capital=1_000.0,
        required_observations=1,
        slippage_buffer_per_contract=0.0,
        liquidity_fraction=1.0,
        min_effective_edge=0.01,
        pair_cooldown_seconds=60,
        clock=clock,
    )

    first = ledger.observe(opportunity)
    now += timedelta(seconds=61)
    second = ledger.observe(opportunity)
    summary = ledger.summary()

    assert first is not None
    assert first.committed_capital == pytest.approx(15.0)
    assert second is None
    assert summary["committed_capital_by_pair"] == {
        opportunity.market_pair.pair_id: pytest.approx(15.0)
    }
    assert summary["decision_counts"]["insufficient_paper_capital_or_liquidity"] == 1


def test_shadow_ledger_caps_total_capital_across_pairs():
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        max_pair_capital=100.0,
        max_total_capital=20.0,
        required_observations=1,
        slippage_buffer_per_contract=0.0,
        liquidity_fraction=1.0,
        min_effective_edge=0.01,
    )

    first = ledger.observe(_opportunity(pair_id="poly-1"))
    second = ledger.observe(_opportunity(pair_id="poly-2"))

    assert first is not None
    assert first.committed_capital == pytest.approx(20.0)
    assert second is None
    assert ledger.committed_capital == pytest.approx(20.0)
    assert ledger.summary()["max_total_capital"] == pytest.approx(20.0)
    assert ledger.summary()["cash_balance"] == pytest.approx(980.0)
    assert ledger.summary()["remaining_deployable_capital"] == pytest.approx(0.0)


def test_larger_paper_profile_reaches_trade_cap_only_when_depth_supports_it():
    def ledger():
        return PaperLockedArbitrageLedger(
            initial_balance=5_000.0,
            max_plan_capital=100.0,
            max_pair_capital=250.0,
            max_total_capital=1_000.0,
            required_observations=1,
            slippage_buffer_per_contract=0.02,
            liquidity_fraction=0.20,
            min_effective_edge=0.01,
        )

    deep = _opportunity(pair_id="deep")
    deep.buy_liquidity = deep.sell_liquidity = 1_000.0
    deep.max_size = 1_000.0
    deep.suggested_size = 100.0
    thin = _opportunity(pair_id="thin")
    thin.buy_liquidity = thin.sell_liquidity = 40.0
    thin.max_size = 40.0
    thin.suggested_size = 100.0

    deep_trade = ledger().observe(deep)
    thin_trade = ledger().observe(thin)

    assert deep_trade is not None
    assert deep_trade.contracts == pytest.approx(100.0)
    assert deep_trade.committed_capital == pytest.approx(97.0)
    assert thin_trade is not None
    assert thin_trade.contracts == pytest.approx(8.0)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("suggested_size", float("nan")),
        ("max_size", float("inf")),
        ("buy_liquidity", float("nan")),
        ("sell_liquidity", float("inf")),
    ],
)
def test_shadow_ledger_rejects_invalid_sizing_evidence(field_name, value):
    opportunity = _opportunity()
    setattr(opportunity, field_name, value)
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
        liquidity_fraction=1.0,
    )

    assert ledger.observe(opportunity) is None
    assert ledger.summary()["decision_counts"] == {"invalid_opportunity_sizing": 1}


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("buy_price", float("nan")),
        ("sell_price", float("inf")),
        ("gross_edge", float("nan")),
        ("net_edge", float("inf")),
    ],
)
def test_shadow_ledger_rejects_invalid_economic_evidence(field_name, value):
    opportunity = _opportunity()
    setattr(opportunity, field_name, value)
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
    )

    assert ledger.observe(opportunity) is None
    assert ledger.summary()["decision_counts"] == {"invalid_opportunity_economics": 1}


def test_shadow_ledger_enforces_final_contract_cap():
    opportunity = _opportunity()
    opportunity.buy_liquidity = opportunity.sell_liquidity = 1_000.0
    opportunity.max_size = opportunity.suggested_size = 1_000.0
    ledger = PaperLockedArbitrageLedger(
        initial_balance=5_000.0,
        max_plan_capital=1_000.0,
        max_contracts_per_trade=100.0,
        required_observations=1,
        liquidity_fraction=1.0,
    )

    trade = ledger.observe(opportunity)

    assert trade is not None
    assert trade.contracts == pytest.approx(100.0)


def test_shadow_ledger_rejects_blacklisted_pair_even_when_verified():
    opportunity = _opportunity()
    opportunity.market_pair.auto_approved = True
    opportunity.market_pair.semantic_relation = "equivalent"
    opportunity.market_pair.verification_confidence = 0.99
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
        approved_market_ids=frozenset(),
        blacklisted_market_ids={"KX-1"},
        allow_verified_auto_approval=True,
        auto_approval_confidence=0.90,
    )

    assert ledger.observe(opportunity) is None
    assert ledger.summary()["decision_counts"] == {"pair_blacklisted": 1}


def test_shadow_ledger_limits_number_of_open_pairs():
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        max_open_pairs=1,
        required_observations=1,
    )

    assert ledger.observe(_opportunity(pair_id="poly-1")) is not None
    assert ledger.observe(_opportunity(pair_id="poly-2")) is None
    assert ledger.summary()["decision_counts"]["max_open_pairs_reached"] == 1


def test_shadow_ledger_consumes_quote_depth_until_visible_capacity_increases():
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)

    def clock():
        return now

    opportunity = _opportunity()
    opportunity.buy_liquidity = opportunity.sell_liquidity = 100.0
    ledger = PaperLockedArbitrageLedger(
        initial_balance=5_000.0,
        max_plan_capital=1_000.0,
        max_pair_capital=1_000.0,
        max_total_capital=1_000.0,
        required_observations=1,
        liquidity_fraction=0.20,
        pair_cooldown_seconds=60,
        clock=clock,
    )

    first = ledger.observe(opportunity)
    now += timedelta(seconds=61)
    unchanged = ledger.observe(opportunity)
    opportunity.buy_liquidity = opportunity.sell_liquidity = 150.0
    increased = ledger.observe(opportunity)
    now += timedelta(seconds=61)
    unchanged_again = ledger.observe(opportunity)

    assert first is not None
    assert first.contracts == pytest.approx(20.0)
    assert unchanged is None
    assert increased is not None
    assert increased.contracts == pytest.approx(10.0)
    assert unchanged_again is None
    assert ledger.summary()["decision_counts"]["displayed_depth_already_consumed"] == 2


def test_shadow_ledger_rejects_pairs_until_both_market_ids_are_approved():
    opportunity = _opportunity()
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
        approved_market_ids=frozenset(),
    )

    assert ledger.observe(opportunity) is None
    assert ledger.summary()["unapproved_opportunity_count"] == 1

    approved = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
        approved_market_ids={"poly-1", "KX-1"},
    )

    assert approved.observe(opportunity) is not None


def test_strictly_verified_pair_can_enter_paper_ledger_without_yaml_edit():
    opportunity = _opportunity()
    opportunity.market_pair.auto_approved = True
    opportunity.market_pair.semantic_relation = "equivalent"
    opportunity.market_pair.verification_confidence = 0.97
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
        approved_market_ids=frozenset(),
        allow_verified_auto_approval=True,
        auto_approval_confidence=0.94,
    )

    assert ledger.observe(opportunity) is not None
    assert ledger.summary()["auto_approved_trade_count"] == 1


def test_embedding_similarity_alone_never_authorizes_paper_trade():
    opportunity = _opportunity()
    opportunity.market_pair.similarity_score = 0.999
    opportunity.market_pair.semantic_relation = "unverified"
    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
        approved_market_ids=frozenset(),
        allow_verified_auto_approval=True,
    )

    assert ledger.observe(opportunity) is None
    assert ledger.summary()["unapproved_opportunity_count"] == 1


def test_pair_can_trade_again_after_cooldown_when_visible_capacity_increases():
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    def clock():
        return now

    ledger = PaperLockedArbitrageLedger(
        initial_balance=1_000.0,
        max_plan_capital=100.0,
        required_observations=1,
        pair_cooldown_seconds=60,
        clock=clock,
    )
    opportunity = _opportunity()

    assert ledger.observe(opportunity) is not None
    assert ledger.observe(opportunity) is None
    now += timedelta(seconds=61)
    assert ledger.observe(opportunity) is None
    opportunity.sell_liquidity = 100.0
    assert ledger.observe(opportunity) is not None


def test_shadow_trade_persists_atomic_two_leg_receipt_and_separated_pnl(tmp_path):
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        run = store.start_run(
            starting_equity=5_000.0,
            pnl_source="projected_locked_paper",
        )
        ledger = PaperLockedArbitrageLedger(
            initial_balance=5_000.0,
            max_plan_capital=250.0,
            required_observations=1,
            slippage_buffer_per_contract=0.02,
            liquidity_fraction=0.10,
            min_effective_edge=0.01,
            store=store,
        )

        trade = ledger.observe(_opportunity())
        receipts = store.recent_cross_platform_paper_trades(
            run_id=run.run_id,
            limit=10,
        )
        summary = ledger.summary()

        assert trade is not None
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["trade_id"] == trade.trade_id
        assert receipt["fee_cost_per_contract"] == pytest.approx(0.03)
        assert receipt["slippage_per_contract"] == pytest.approx(0.02)
        assert receipt["effective_edge_per_contract"] == pytest.approx(0.03)
        assert {leg["leg_role"] for leg in receipt["legs"]} == {"buy", "hedge"}
        buy_leg = next(leg for leg in receipt["legs"] if leg["leg_role"] == "buy")
        hedge_leg = next(leg for leg in receipt["legs"] if leg["leg_role"] == "hedge")
        assert buy_leg["simulated_price"] == pytest.approx(0.41)
        assert hedge_leg["simulated_price"] == pytest.approx(0.47)
        assert store.active_run().transaction_count == 1
        assert summary["cash_balance"] == pytest.approx(4_992.24)
        assert summary["reserved_cost_basis"] == pytest.approx(7.76)
        assert summary["unrealized_mark_to_market_pnl"] == 0.0
        assert summary["realized_settlement_pnl"] == 0.0
        assert summary["projected_locked_pnl"] == pytest.approx(0.24)
    finally:
        store.close()
