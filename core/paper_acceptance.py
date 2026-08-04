"""Deterministic, non-mutating acceptance proof for the paper trade lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cross_platform_arb import CrossPlatformArbEngine, MarketPair
from core.paper_locked_arb import PaperLockedArbitrageLedger
from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from utils.paper_trade_store import PaperTradeStore


def _token_book(
    token_type: TokenType,
    *,
    bid: float,
    ask: float,
    size: float,
) -> TokenOrderBook:
    return TokenOrderBook(
        token_type=token_type,
        bids=OrderBookSide([PriceLevel(bid, size)]),
        asks=OrderBookSide([PriceLevel(ask, size)]),
    )


def _book(
    market_id: str,
    *,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    no_ask: float,
    observed_at: datetime,
) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        yes=_token_book(TokenType.YES, bid=yes_bid, ask=yes_ask, size=20),
        no=_token_book(TokenType.NO, bid=no_bid, ask=no_ask, size=20),
        timestamp=observed_at,
    )


def run_deterministic_paper_acceptance(db_path: str | Path) -> dict[str, Any]:
    """Prove detector-to-ledger persistence without creating a venue client."""
    store = PaperTradeStore(str(db_path))
    try:
        store.start_run(
            starting_equity=1000.0,
            pnl_source="projected_locked_paper",
        )
        pair = MarketPair(
            polymarket_id="fixture-poly-market",
            polymarket_condition_id="fixture-poly-condition",
            kalshi_ticker="FIXTURE-KX",
            polymarket_question="Will the fixture event occur?",
            kalshi_title="Fixture event occurs?",
            similarity_score=1.0,
            semantic_relation="equivalent",
            verification_confidence=1.0,
            auto_approved=False,
        )
        observed_at = datetime.now(timezone.utc)
        polymarket_book = _book(
            pair.polymarket_id,
            yes_bid=0.39,
            yes_ask=0.40,
            no_bid=0.59,
            no_ask=0.60,
            observed_at=observed_at,
        )
        kalshi_book = _book(
            f"kalshi:{pair.kalshi_ticker}",
            yes_bid=0.55,
            yes_ask=0.56,
            no_bid=0.44,
            no_ask=0.45,
            observed_at=observed_at,
        )
        detector = CrossPlatformArbEngine(
            min_edge=0.02,
            polymarket_taker_fee=0.015,
            kalshi_taker_fee=0.01,
            gas_cost=0.0,
            max_order_size=10,
            max_liquidity_fraction=0.25,
            min_executable_size=1,
        )
        first_opportunity = detector.check_arbitrage(
            pair,
            polymarket_book,
            kalshi_book,
        )
        if first_opportunity is None:
            raise RuntimeError("deterministic acceptance fixture produced no opportunity")
        ledger = PaperLockedArbitrageLedger(
            initial_balance=1000.0,
            max_plan_capital=100.0,
            required_observations=2,
            slippage_buffer_per_contract=0.02,
            liquidity_fraction=0.25,
            min_effective_edge=0.01,
            approved_market_ids={
                pair.polymarket_execution_id,
                pair.kalshi_ticker,
            },
            store=store,
        )
        first = ledger.observe(first_opportunity)
        second_observed_at = datetime.now(timezone.utc)
        second_opportunity = detector.check_arbitrage(
            pair,
            _book(
                pair.polymarket_id,
                yes_bid=0.39,
                yes_ask=0.40,
                no_bid=0.59,
                no_ask=0.60,
                observed_at=second_observed_at,
            ),
            _book(
                f"kalshi:{pair.kalshi_ticker}",
                yes_bid=0.55,
                yes_ask=0.56,
                no_bid=0.44,
                no_ask=0.45,
                observed_at=second_observed_at,
            ),
        )
        if second_opportunity is None:
            raise RuntimeError("second acceptance observation produced no opportunity")
        trade = ledger.observe(second_opportunity)
        if first is not None or trade is None:
            raise RuntimeError("paper confirmation lifecycle did not complete")
        active = store.active_run()
        if active is None or active.transaction_count != 1:
            raise RuntimeError("paper shadow fill was not persisted exactly once")
        store.record_cross_platform_evaluation_counts(
            {
                "pair_due": 2,
                "paired_snapshot_fresh": 2,
                "opportunity_detected": 2,
                "paper_shadow_trade": 1,
            }
        )
        summary = ledger.summary()
        finished = store.finish_run(
            ending_equity=float(summary["projected_equity_at_settlement"]),
            pnl=float(summary["projected_locked_pnl"]),
            status="completed",
        )
        return {
            "status": "passed",
            "proof_mode": "deterministic_venue_shaped_fixture",
            "venue_mutations": 0,
            "opportunity_detected": True,
            "paper_trade_events": finished.transaction_count,
            "paper_run_status": finished.status,
            "projected_locked_pnl": finished.pnl,
            "evaluation_funnel": store.cross_platform_evaluation_funnel(
                finished.run_id
            ),
        }
    except BaseException:
        active = store.active_run()
        if active is not None:
            try:
                store.finish_run(
                    ending_equity=active.ending_equity,
                    pnl=active.pnl,
                    status="failed",
                )
            except BaseException:
                pass
        raise
    finally:
        store.close()
