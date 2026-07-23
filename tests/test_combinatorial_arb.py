from datetime import datetime, timezone

import pytest

from core.combinatorial_arb import SamePlatformArbitrageDetector
from polymarket_client.models import (
    Market,
    MarketState,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)


def _state(market_id: str, yes_ask: float, no_ask: float, *, negative_risk=True):
    market = Market(
        market_id=market_id,
        condition_id=f"condition-{market_id}",
        question=f"Will candidate {market_id} win?",
        event_id="election-2026",
        event_title="2026 mayoral election winner",
        outcome_label=market_id,
        negative_risk=negative_risk,
        end_date=datetime(2026, 11, 4, tzinfo=timezone.utc),
    )
    book = OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            TokenType.YES,
            bids=OrderBookSide([PriceLevel(max(0.01, yes_ask - 0.02), 50)]),
            asks=OrderBookSide([PriceLevel(yes_ask, 50)]),
        ),
        no=TokenOrderBook(
            TokenType.NO,
            bids=OrderBookSide([PriceLevel(max(0.01, no_ask - 0.02), 50)]),
            asks=OrderBookSide([PriceLevel(no_ask, 50)]),
        ),
    )
    return MarketState(market=market, order_book=book)


def test_detects_multi_market_negative_risk_yes_bundle():
    detector = SamePlatformArbitrageDetector(min_edge=0.02, taker_fee_rate=0)
    states = {
        "alice": _state("alice", 0.25, 0.76),
        "bob": _state("bob", 0.25, 0.76),
        "carol": _state("carol", 0.25, 0.76),
    }

    opportunities = detector.detect(states)

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.kind == "negative_risk_yes_bundle"
    assert opportunity.total_price == pytest.approx(0.75)
    assert opportunity.net_edge == pytest.approx(0.25)
    assert len(opportunity.legs) == 3


def test_does_not_infer_exhaustiveness_from_similar_titles_alone():
    detector = SamePlatformArbitrageDetector(min_edge=0.02, taker_fee_rate=0)
    states = {
        "alice": _state("alice", 0.25, 0.76, negative_risk=False),
        "bob": _state("bob", 0.25, 0.76, negative_risk=False),
    }

    assert detector.detect(states) == []
    assert detector.last_metrics.states == 2
    assert detector.last_metrics.negative_risk_states == 0
    assert detector.last_metrics.event_groups == 0
    assert detector.last_metrics.eligible_groups == 0
    assert detector.last_metrics.opportunities == 0


def test_detector_deduplicates_same_snapshot_until_cooldown_expires():
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    detector = SamePlatformArbitrageDetector(
        min_edge=0.02,
        taker_fee_rate=0,
        cooldown_seconds=60,
        clock=lambda: now,
    )
    states = {
        "alice": _state("alice", 0.25, 0.76),
        "bob": _state("bob", 0.25, 0.76),
        "carol": _state("carol", 0.25, 0.76),
    }

    assert len(detector.detect(states)) == 1
    assert detector.detect(states) == []
