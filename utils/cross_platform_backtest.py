"""
Historical replay helpers for cross-platform arbitrage research.

The replay consumes normalized historical JSONL records. When a source has only
price history, it builds an approximate one-level book with a configurable
assumed spread so the result is explicitly sensitivity-based.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from core.cross_platform_arb import CrossPlatformArbEngine, MarketPair
from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from utils.historical_data import parse_timestamp


@dataclass
class HistoricalRecord:
    timestamp: datetime
    platform: str
    market_id: str
    token: str
    price: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    volume: Optional[float]
    source: str
    raw: dict


@dataclass
class ReplayOpportunity:
    timestamp: datetime
    polymarket_id: str
    kalshi_ticker: str
    token: str
    buy_platform: str
    sell_platform: str
    buy_price: float
    sell_price: float
    gross_edge: float
    net_edge: float
    edge_pct: float
    max_size: float


@dataclass
class ReplayResult:
    pairs_requested: int
    pairs_evaluated: int
    evaluations: int
    opportunities: list[ReplayOpportunity]
    skipped_pairs: dict[str, str]

    @property
    def opportunity_count(self) -> int:
        return len(self.opportunities)

    @property
    def best_net_edge(self) -> float:
        return max((opp.net_edge for opp in self.opportunities), default=0.0)

    @property
    def avg_net_edge(self) -> float:
        if not self.opportunities:
            return 0.0
        return sum(opp.net_edge for opp in self.opportunities) / len(self.opportunities)


def load_historical_records(path: str | Path) -> list[HistoricalRecord]:
    """Load normalized historical JSONL records."""
    records = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            records.append(HistoricalRecord(
                timestamp=parse_timestamp(item["timestamp"]),
                platform=item["platform"],
                market_id=str(item["market_id"]),
                token=str(item.get("token") or "YES").upper(),
                price=_float_or_none(item.get("price")),
                bid=_float_or_none(item.get("bid")),
                ask=_float_or_none(item.get("ask")),
                volume=_float_or_none(item.get("volume")),
                source=item.get("source", ""),
                raw=item.get("raw", {}),
            ))
    return records


def run_cross_platform_replay(
    *,
    polymarket_records: Iterable[HistoricalRecord],
    kalshi_records: Iterable[HistoricalRecord],
    pairs: list[tuple[str, str]],
    min_edge: float,
    max_time_delta: timedelta,
    assumed_spread: float,
    default_liquidity: float,
    polymarket_taker_fee: float,
    kalshi_taker_fee: float,
    gas_cost: float,
) -> ReplayResult:
    """Replay historical records through the existing cross-platform engine."""
    poly_by_market = _group_records(polymarket_records, platform="polymarket")
    kalshi_by_market = _group_records(kalshi_records, platform="kalshi")
    engine = CrossPlatformArbEngine(
        min_edge=min_edge,
        polymarket_taker_fee=polymarket_taker_fee,
        kalshi_taker_fee=kalshi_taker_fee,
        gas_cost=gas_cost,
        # Historical timestamps are bounded against one another by
        # max_time_delta below, not against the current wall clock.
        max_observation_age=None,
    )

    skipped_pairs: dict[str, str] = {}
    opportunities: list[ReplayOpportunity] = []
    evaluations = 0
    pairs_evaluated = 0

    for polymarket_id, kalshi_ticker in pairs:
        pair_key = f"{polymarket_id}:{kalshi_ticker}"
        poly_market_records = poly_by_market.get(polymarket_id)
        kalshi_market_records = kalshi_by_market.get(kalshi_ticker)
        if not poly_market_records:
            skipped_pairs[pair_key] = "missing polymarket records"
            continue
        if not kalshi_market_records:
            skipped_pairs[pair_key] = "missing kalshi records"
            continue

        poly_snapshots = _snapshots_from_records(poly_market_records)
        kalshi_snapshots = _snapshots_from_records(kalshi_market_records)
        if not poly_snapshots:
            skipped_pairs[pair_key] = "no polymarket snapshots"
            continue
        if not kalshi_snapshots:
            skipped_pairs[pair_key] = "no kalshi snapshots"
            continue

        matched_any = False
        pair = MarketPair(
            polymarket_id=polymarket_id,
            kalshi_ticker=kalshi_ticker,
            polymarket_question=polymarket_id,
            kalshi_title=kalshi_ticker,
            similarity_score=1.0,
            category="historical",
        )

        for timestamp, poly_snapshot in poly_snapshots.items():
            kalshi_timestamp = _nearest_timestamp(timestamp, kalshi_snapshots.keys(), max_time_delta)
            if kalshi_timestamp is None:
                continue

            poly_ob = _orderbook_from_snapshot(
                market_id=polymarket_id,
                snapshot=poly_snapshot,
                assumed_spread=assumed_spread,
                default_liquidity=default_liquidity,
            )
            kalshi_ob = _orderbook_from_snapshot(
                market_id=f"kalshi:{kalshi_ticker}",
                snapshot=kalshi_snapshots[kalshi_timestamp],
                assumed_spread=assumed_spread,
                default_liquidity=default_liquidity,
            )
            evaluations += 1
            matched_any = True

            opportunity = engine.check_arbitrage(pair, poly_ob, kalshi_ob)
            if opportunity:
                opportunities.append(ReplayOpportunity(
                    timestamp=timestamp,
                    polymarket_id=polymarket_id,
                    kalshi_ticker=kalshi_ticker,
                    token=opportunity.token,
                    buy_platform=opportunity.buy_platform,
                    sell_platform=opportunity.sell_platform,
                    buy_price=opportunity.buy_price,
                    sell_price=opportunity.sell_price,
                    gross_edge=opportunity.gross_edge,
                    net_edge=opportunity.net_edge,
                    edge_pct=opportunity.edge_pct,
                    max_size=opportunity.max_size,
                ))

        if matched_any:
            pairs_evaluated += 1
        else:
            skipped_pairs[pair_key] = "no overlapping timestamps"

    return ReplayResult(
        pairs_requested=len(pairs),
        pairs_evaluated=pairs_evaluated,
        evaluations=evaluations,
        opportunities=opportunities,
        skipped_pairs=skipped_pairs,
    )


def make_all_pairs(polymarket_records: Iterable[HistoricalRecord], kalshi_records: Iterable[HistoricalRecord]) -> list[tuple[str, str]]:
    """Create all market-id combinations represented in the two datasets."""
    poly_ids = sorted({record.market_id for record in polymarket_records if record.platform == "polymarket"})
    kalshi_ids = sorted({record.market_id for record in kalshi_records if record.platform == "kalshi"})
    return [(poly_id, kalshi_id) for poly_id in poly_ids for kalshi_id in kalshi_ids]


def _group_records(records: Iterable[HistoricalRecord], platform: str) -> dict[str, list[HistoricalRecord]]:
    grouped: dict[str, list[HistoricalRecord]] = {}
    for record in records:
        if record.platform != platform:
            continue
        grouped.setdefault(record.market_id, []).append(record)
    return grouped


def _snapshots_from_records(records: Iterable[HistoricalRecord]) -> dict[datetime, dict[str, HistoricalRecord]]:
    snapshots: dict[datetime, dict[str, HistoricalRecord]] = {}
    for record in records:
        snapshots.setdefault(record.timestamp, {})[record.token] = record
    return snapshots


def _nearest_timestamp(
    target: datetime,
    candidates: Iterable[datetime],
    max_delta: timedelta,
) -> Optional[datetime]:
    best_timestamp = None
    best_delta = None
    for candidate in candidates:
        delta = abs(candidate - target)
        if delta <= max_delta and (best_delta is None or delta < best_delta):
            best_timestamp = candidate
            best_delta = delta
    return best_timestamp


def _orderbook_from_snapshot(
    *,
    market_id: str,
    snapshot: dict[str, HistoricalRecord],
    assumed_spread: float,
    default_liquidity: float,
) -> OrderBook:
    yes = _token_book_from_record(
        token_type=TokenType.YES,
        record=snapshot.get("YES"),
        fallback_price=None,
        assumed_spread=assumed_spread,
        default_liquidity=default_liquidity,
    )

    no_record = snapshot.get("NO")
    if no_record:
        no = _token_book_from_record(
            token_type=TokenType.NO,
            record=no_record,
            fallback_price=None,
            assumed_spread=assumed_spread,
            default_liquidity=default_liquidity,
        )
    else:
        no = _derive_no_book_from_yes(yes, default_liquidity)

    timestamps = [record.timestamp for record in snapshot.values()]
    return OrderBook(
        market_id=market_id,
        yes=yes,
        no=no,
        timestamp=min(timestamps) if timestamps else datetime.now(timezone.utc),
    )


def _token_book_from_record(
    *,
    token_type: TokenType,
    record: Optional[HistoricalRecord],
    fallback_price: Optional[float],
    assumed_spread: float,
    default_liquidity: float,
) -> TokenOrderBook:
    price = record.price if record and record.price is not None else fallback_price
    bid = record.bid if record and record.bid is not None else None
    ask = record.ask if record and record.ask is not None else None
    liquidity = record.volume if record and record.volume and record.volume > 0 else default_liquidity

    if bid is None or ask is None:
        if price is None:
            bid = None
            ask = None
        else:
            half_spread = max(0.0, assumed_spread) / 2
            bid = _clamp_price(price - half_spread)
            ask = _clamp_price(price + half_spread)

    return TokenOrderBook(
        token_type=token_type,
        bids=OrderBookSide(levels=[] if bid is None else [PriceLevel(price=bid, size=liquidity)]),
        asks=OrderBookSide(levels=[] if ask is None else [PriceLevel(price=ask, size=liquidity)]),
    )


def _derive_no_book_from_yes(yes: TokenOrderBook, default_liquidity: float) -> TokenOrderBook:
    no_bid = _clamp_price(1.0 - yes.best_ask) if yes.best_ask is not None else None
    no_ask = _clamp_price(1.0 - yes.best_bid) if yes.best_bid is not None else None
    return TokenOrderBook(
        token_type=TokenType.NO,
        bids=OrderBookSide(levels=[] if no_bid is None else [PriceLevel(price=no_bid, size=yes.best_ask_size or default_liquidity)]),
        asks=OrderBookSide(levels=[] if no_ask is None else [PriceLevel(price=no_ask, size=yes.best_bid_size or default_liquidity)]),
    )


def _clamp_price(price: float) -> float:
    return max(0.001, min(0.999, float(price)))


def _float_or_none(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
