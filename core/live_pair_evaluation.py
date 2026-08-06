"""One-shot, public-data evaluation of a manually verified venue pair."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable

from core.cross_platform_arb import CrossPlatformArbEngine, MarketPair
from core.execution_economics import AuthoritativeEconomicsProvider
from core.pair_snapshot import PairSnapshotSource


class PairApprovalRequired(ValueError):
    """The canonical venue metadata has not been explicitly approved."""

    def __init__(self, pair_hash: str, canonical_pair: dict[str, Any]):
        super().__init__("canonical pair approval is required")
        self.pair_hash = pair_hash
        self.canonical_pair = canonical_pair


def canonical_live_pair(
    polymarket_market: Any,
    kalshi_market: Any,
    *,
    approved_pair_hash: str | None,
) -> tuple[MarketPair, dict[str, Any], str]:
    """Bind operator approval to current authoritative venue metadata."""
    if (
        not polymarket_market.active
        or polymarket_market.closed
        or polymarket_market.resolved
    ):
        raise ValueError("Polymarket market is not active and unresolved")
    if not polymarket_market.yes_token_id or not polymarket_market.no_token_id:
        raise ValueError("Polymarket market is missing executable token identities")
    if not kalshi_market.is_active or kalshi_market.result not in (None, ""):
        raise ValueError("Kalshi market is not active and unresolved")
    canonical = {
        "polymarket": {
            "market_id": polymarket_market.market_id,
            "condition_id": polymarket_market.condition_id,
            "question": polymarket_market.question,
            "description": polymarket_market.description,
            "resolution_source": polymarket_market.resolution_source,
        },
        "kalshi": {
            "ticker": kalshi_market.ticker,
            "title": kalshi_market.title,
            "subtitle": kalshi_market.subtitle,
            "rules_primary": kalshi_market.rules_primary,
            "rules_secondary": kalshi_market.rules_secondary,
            "settlement_source": kalshi_market.settlement_source,
        },
    }
    pair_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if approved_pair_hash != pair_hash:
        raise PairApprovalRequired(pair_hash, canonical)
    pair = MarketPair(
        polymarket_id=polymarket_market.market_id,
        polymarket_condition_id=polymarket_market.condition_id,
        kalshi_ticker=kalshi_market.ticker,
        polymarket_question=polymarket_market.question,
        kalshi_title=kalshi_market.matching_text,
        similarity_score=1.0,
        semantic_relation="equivalent",
        verification_confidence=1.0,
        auto_approved=False,
    )
    return pair, canonical, pair_hash


async def evaluate_live_pair_readonly(
    pair: MarketPair,
    polymarket_client: Any,
    kalshi_client: Any,
    *,
    max_age_seconds: float = 5.0,
    timeout_seconds: float = 5.0,
    min_edge: float = 0.02,
    slippage_reserve_per_contract: float = 0.02,
    economics_provider: Any | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Evaluate fresh books without constructing or submitting any order."""
    assumptions = (min_edge, slippage_reserve_per_contract)
    if not all(math.isfinite(value) for value in assumptions):
        raise ValueError("economic assumptions must be finite")
    if not 0 <= min_edge <= 1:
        raise ValueError("minimum edge must be in [0, 1]")
    if not 0 <= slippage_reserve_per_contract <= 1:
        raise ValueError("slippage reserve must be in [0, 1]")
    provider = economics_provider or AuthoritativeEconomicsProvider(
        polymarket_client,
        kalshi_client,
    )
    # Fetch fee metadata before the paired books. This avoids overlapping
    # multiple Cloudflare-fronted CLOB reads on one short-lived canary client.
    pair_economics = await provider.quote_pair(pair)
    snapshot = await PairSnapshotSource(
        polymarket_client,
        kalshi_client,
        max_age_seconds=max_age_seconds,
        timeout_seconds=timeout_seconds,
        clock=clock,
    ).fetch(pair)
    detector = CrossPlatformArbEngine(
        min_edge=min_edge,
        slippage_reserve_per_contract=slippage_reserve_per_contract,
        max_observation_age=None,
        require_authoritative_economics=True,
    )
    opportunity = detector.check_arbitrage(
        pair,
        snapshot.polymarket_book,
        snapshot.kalshi_book,
        economics=pair_economics,
    )
    direction_evaluations = [
        asdict(evaluation)
        for evaluation in detector.get_last_direction_evaluations(pair.pair_id)
    ]
    result: dict[str, Any] = {
        "status": "after_cost_edge" if opportunity else "no_after_cost_edge",
        "proof_mode": "live_public_books_read_only",
        "venue_mutations": 0,
        "snapshot": {
            "pair_id": snapshot.pair_id,
            "polymarket_observed_at": snapshot.polymarket_book.timestamp.isoformat(),
            "kalshi_observed_at": snapshot.kalshi_book.timestamp.isoformat(),
            "max_age_seconds": snapshot.max_age_seconds,
            "polymarket": {
                "yes_bid": snapshot.polymarket_book.best_bid_yes,
                "yes_ask": snapshot.polymarket_book.best_ask_yes,
                "no_bid": snapshot.polymarket_book.best_bid_no,
                "no_ask": snapshot.polymarket_book.best_ask_no,
            },
            "kalshi": {
                "yes_bid": snapshot.kalshi_book.best_bid_yes,
                "yes_ask": snapshot.kalshi_book.best_ask_yes,
                "no_bid": snapshot.kalshi_book.best_bid_no,
                "no_ask": snapshot.kalshi_book.best_ask_no,
            },
        },
        "economics": {
            "source": "authoritative_venue_metadata",
            "observed_at": pair_economics.observed_at.isoformat(),
            "polymarket_fee_rate": str(pair_economics.polymarket_fee_rate),
            "polymarket_fee_exponent": str(pair_economics.polymarket_fee_exponent),
            "polymarket_gas_cost": str(pair_economics.polymarket_order_gas_cost),
            "polymarket_gas_source": pair_economics.polymarket_gas_source,
            "kalshi_fee_type": pair_economics.kalshi_fee_type,
            "kalshi_fee_multiplier": str(pair_economics.kalshi_fee_multiplier),
            "minimum_net_edge": min_edge,
            "slippage_reserve_per_contract": slippage_reserve_per_contract,
        },
        "direction_evaluations": direction_evaluations,
        "opportunity": None,
    }
    if opportunity:
        result["opportunity"] = {
            "buy_platform": opportunity.buy_platform,
            "sell_platform": opportunity.sell_platform,
            "token": opportunity.token,
            "buy_price": opportunity.buy_price,
            "sell_price": opportunity.sell_price,
            "gross_edge": opportunity.gross_edge,
            "net_edge": opportunity.net_edge,
            "suggested_size": opportunity.suggested_size,
        }
    return result
