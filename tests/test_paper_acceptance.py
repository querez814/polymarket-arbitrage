import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.paper_acceptance import run_deterministic_paper_acceptance
from core.live_pair_evaluation import (
    PairApprovalRequired,
    canonical_live_pair,
    evaluate_live_pair_readonly,
)
from core.cross_platform_arb import MarketPair
from core.execution_economics import PairEconomics
from kalshi_client.models import KalshiMarket
from polymarket_client.models import (
    Market,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from utils.paper_trade_store import PaperTradeStore


def _book(market_id, *, yes_bid, yes_ask, no_bid, no_ask, observed_at):
    def token(token_type, bid, ask):
        return TokenOrderBook(
            token_type=token_type,
            bids=OrderBookSide([PriceLevel(bid, 10)]),
            asks=OrderBookSide([PriceLevel(ask, 10)]),
        )

    return OrderBook(
        market_id=market_id,
        yes=token(TokenType.YES, yes_bid, yes_ask),
        no=token(TokenType.NO, no_bid, no_ask),
        timestamp=observed_at,
    )


def test_deterministic_acceptance_proves_detection_and_persisted_shadow_fill(tmp_path):
    result = run_deterministic_paper_acceptance(tmp_path / "acceptance.db")

    assert result["status"] == "passed"
    assert result["venue_mutations"] == 0
    assert result["opportunity_detected"] is True
    assert result["paper_trade_events"] == 1
    assert result["paper_run_status"] == "completed"
    assert result["projected_locked_pnl"] > 0


def test_deterministic_acceptance_marks_run_failed_when_fill_is_not_persisted(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "failed-acceptance.db"
    monkeypatch.setattr(
        PaperTradeStore,
        "record_cross_platform_paper_trade",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(RuntimeError, match="not persisted exactly once"):
        run_deterministic_paper_acceptance(db_path)

    store = PaperTradeStore(str(db_path))
    try:
        assert store.recent_runs(1)[0].status == "failed"
    finally:
        store.close()


def test_live_pair_evaluation_is_read_only_and_reports_no_edge():
    clock = datetime.now(timezone.utc)
    pair = MarketPair(
        polymarket_id="poly-current",
        kalshi_ticker="KX-CURRENT",
        polymarket_question="Same event?",
        kalshi_title="Same event?",
        similarity_score=1.0,
        semantic_relation="equivalent",
        verification_confidence=1.0,
    )
    poly_book = _book(
        pair.polymarket_id,
        yes_bid=0.49,
        yes_ask=0.51,
        no_bid=0.49,
        no_ask=0.51,
        observed_at=clock,
    )
    kalshi_book = _book(
        pair.kalshi_ticker,
        yes_bid=0.49,
        yes_ask=0.51,
        no_bid=0.49,
        no_ask=0.51,
        observed_at=clock,
    )

    class Poly:
        async def get_orderbook(self, market_id):
            return poly_book

    class Kalshi:
        async def get_orderbook_unified(self, ticker):
            return kalshi_book

    class EconomicsProvider:
        async def quote_pair(self, requested_pair):
            assert requested_pair is pair
            return PairEconomics(
                pair_id=pair.pair_id,
                polymarket_market_id=pair.polymarket_execution_id,
                kalshi_ticker=pair.kalshi_ticker,
                polymarket_fee_rate=Decimal("0"),
                polymarket_fee_exponent=Decimal("1"),
                polymarket_taker_only=True,
                polymarket_order_gas_cost=Decimal("0"),
                polymarket_gas_source="offchain_clob_order",
                kalshi_fee_type="quadratic",
                kalshi_fee_multiplier=Decimal("1"),
                observed_at=clock,
            )

    result = asyncio.run(
        evaluate_live_pair_readonly(
            pair,
            Poly(),
            Kalshi(),
            economics_provider=EconomicsProvider(),
            clock=lambda: clock,
        )
    )

    assert result["status"] == "no_after_cost_edge"
    assert result["venue_mutations"] == 0
    assert result["snapshot"]["pair_id"] == pair.pair_id
    assert result["snapshot"]["polymarket"]["yes_bid"] == 0.49
    assert result["snapshot"]["kalshi"]["yes_ask"] == 0.51
    assert result["economics"]["source"] == "authoritative_venue_metadata"
    assert len(result["direction_evaluations"]) == 4


def test_live_pair_evaluation_rejects_dishonest_negative_slippage_assumption():
    pair = MarketPair("poly", "kalshi", "Same?", "Same?", 1.0)

    with pytest.raises(ValueError, match="slippage"):
        asyncio.run(
            evaluate_live_pair_readonly(
                pair,
                object(),
                object(),
                slippage_reserve_per_contract=-0.01,
            )
        )

    with pytest.raises(ValueError, match="finite"):
        asyncio.run(
            evaluate_live_pair_readonly(
                pair,
                object(),
                object(),
                min_edge=float("nan"),
            )
        )


def test_canonical_live_pair_requires_approval_bound_to_venue_metadata():
    polymarket = Market(
        market_id="123",
        condition_id="0xabc",
        question="Will Alice win?",
        description="Resolves yes if Alice wins.",
        yes_token_id="yes-token",
        no_token_id="no-token",
        resolution_source="official result",
    )
    kalshi = KalshiMarket(
        ticker="KX-ALICE",
        event_ticker="KX-EVENT",
        series_ticker="KX",
        title="Will Alice win?",
        rules_primary="Alice must win.",
        settlement_source="official result",
    )

    with pytest.raises(PairApprovalRequired) as approval:
        canonical_live_pair(polymarket, kalshi, approved_pair_hash=None)

    pair, canonical, pair_hash = canonical_live_pair(
        polymarket,
        kalshi,
        approved_pair_hash=approval.value.pair_hash,
    )
    assert pair.polymarket_id == "123"
    assert canonical == approval.value.canonical_pair
    assert pair_hash == approval.value.pair_hash
