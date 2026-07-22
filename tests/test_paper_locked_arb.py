from core.cross_platform_arb import CrossPlatformOpportunity, MarketPair
from core.paper_locked_arb import PaperLockedArbitrageLedger
import pytest


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
