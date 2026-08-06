import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.cross_platform_arb import CrossPlatformDirectionEvaluation, MarketPair
from core.pair_monitoring import DuePair
from core.pair_snapshot import PairSnapshot, PairSnapshotError, PairSnapshotSource
from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from polymarket_client.api import OrderBookNormalizationError
from run_with_dashboard import TradingBotWithDashboard
from utils.config_loader import BotConfig
from utils.paper_trade_store import PaperTradeStore


def _book_with_depth(market_id: str, observed_at: datetime) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            TokenType.YES,
            bids=OrderBookSide([PriceLevel(0.49, 10)]),
            asks=OrderBookSide([PriceLevel(0.51, 10)]),
        ),
        timestamp=observed_at,
    )


@pytest.mark.asyncio
async def test_pair_snapshot_fetches_both_venue_books_as_one_fresh_observation():
    both_fetches_started = asyncio.Event()
    fetches_started = 0

    async def venue_book(market_id: str) -> OrderBook:
        nonlocal fetches_started
        fetches_started += 1
        if fetches_started == 2:
            both_fetches_started.set()
        await asyncio.wait_for(both_fetches_started.wait(), timeout=0.1)
        return _book_with_depth(market_id, datetime.now(timezone.utc))

    class PolymarketClient:
        async def get_orderbook(self, market_id: str) -> OrderBook:
            return await venue_book(market_id)

    class KalshiClient:
        async def get_orderbook_unified(self, ticker: str) -> OrderBook:
            return await venue_book(f"kalshi:{ticker}")

    pair = MarketPair(
        polymarket_id="poly-1",
        kalshi_ticker="KX-1",
        polymarket_question="Will Alice win?",
        kalshi_title="Alice wins?",
        similarity_score=0.98,
    )

    snapshot = await PairSnapshotSource(
        PolymarketClient(),
        KalshiClient(),
        max_age_seconds=5,
        timeout_seconds=1,
    ).fetch(pair)

    assert snapshot.pair_id == pair.pair_id
    assert snapshot.polymarket_book.market_id == "poly-1"
    assert snapshot.kalshi_book.market_id == "kalshi:KX-1"
    assert snapshot.max_age_seconds == 5


@pytest.mark.asyncio
async def test_pair_snapshot_rejects_a_stale_venue_with_a_durable_reason_code():
    stale = datetime.now(timezone.utc) - timedelta(seconds=6)

    class PolymarketClient:
        async def get_orderbook(self, market_id: str) -> OrderBook:
            return _book_with_depth(market_id, stale)

    class KalshiClient:
        async def get_orderbook_unified(self, ticker: str) -> OrderBook:
            return OrderBook(
                market_id=f"kalshi:{ticker}",
                timestamp=datetime.now(timezone.utc),
            )

    pair = MarketPair("poly-1", "KX-1", "Alice?", "Alice?", 0.98)
    source = PairSnapshotSource(
        PolymarketClient(),
        KalshiClient(),
        max_age_seconds=5,
        timeout_seconds=1,
    )

    with pytest.raises(PairSnapshotError) as captured:
        await source.fetch(pair)

    assert captured.value.reason_code == "stale_polymarket_orderbook"
    assert captured.value.evidence["max_age_seconds"] == 5
    assert captured.value.evidence["age_seconds"] >= 6


@pytest.mark.asyncio
async def test_pair_snapshot_timeout_is_a_reason_coded_pair_outcome():
    class SlowClient:
        async def get_orderbook(self, market_id: str) -> OrderBook:
            await asyncio.sleep(10)

        async def get_orderbook_unified(self, ticker: str) -> OrderBook:
            await asyncio.sleep(10)

    pair = MarketPair("poly-1", "KX-1", "Alice?", "Alice?", 0.98)
    source = PairSnapshotSource(
        SlowClient(),
        SlowClient(),
        max_age_seconds=5,
        timeout_seconds=0.01,
    )

    with pytest.raises(PairSnapshotError) as captured:
        await source.fetch(pair)

    assert captured.value.reason_code == "paired_snapshot_timeout"
    assert captured.value.evidence == {"timeout_seconds": 0.01}


@pytest.mark.asyncio
async def test_pair_snapshot_rejects_an_empty_book_as_unusable_evidence():
    now = datetime.now(timezone.utc)

    class PolymarketClient:
        async def get_orderbook(self, market_id: str) -> OrderBook:
            return OrderBook(market_id=market_id, timestamp=now)

    class KalshiClient:
        async def get_orderbook_unified(self, ticker: str) -> OrderBook:
            return _book_with_depth(ticker, now)

    pair = MarketPair("poly-1", "KX-1", "Alice?", "Alice?", 0.98)
    with pytest.raises(PairSnapshotError) as captured:
        await PairSnapshotSource(
            PolymarketClient(),
            KalshiClient(),
            max_age_seconds=5,
            timeout_seconds=1,
        ).fetch(pair)

    assert captured.value.reason_code == "empty_polymarket_orderbook"


@pytest.mark.asyncio
async def test_pair_snapshot_preserves_reason_coded_ingestion_failure():
    class PolymarketClient:
        async def get_orderbook(self, market_id):
            raise OrderBookNormalizationError(
                "polymarket_orderbook_normalization_failed",
                evidence={"token_id": "bad-token", "detail": "crossed"},
            )

    class KalshiClient:
        async def get_orderbook_unified(self, ticker):
            return _book_with_depth(ticker, datetime.now(timezone.utc))

    pair = MarketPair("poly-1", "KX-1", "Alice?", "Alice?", 0.98)
    with pytest.raises(PairSnapshotError) as captured:
        await PairSnapshotSource(
            PolymarketClient(),
            KalshiClient(),
            max_age_seconds=5,
            timeout_seconds=1,
        ).fetch(pair)

    assert captured.value.reason_code == "polymarket_orderbook_normalization_failed"
    assert captured.value.evidence["token_id"] == "bad-token"


@pytest.mark.asyncio
async def test_cross_platform_scanner_evaluates_the_pair_snapshot_not_global_cache(
    tmp_path,
):
    pair = MarketPair("poly-1", "KX-1", "Alice?", "Alice?", 0.98)
    now = datetime.now(timezone.utc)
    poly_book = _book_with_depth("poly-1", now)
    kalshi_book = _book_with_depth("kalshi:KX-1", now)

    class SnapshotSource:
        calls = 0

        async def fetch(self, requested_pair):
            self.calls += 1
            assert requested_pair is pair
            return PairSnapshot(
                pair_id=pair.pair_id,
                polymarket_book=poly_book,
                kalshi_book=kalshi_book,
                max_age_seconds=5,
            )

    class PairMonitor:
        def due_pairs(self, pairs):
            return [DuePair(pair=pair, tier="hot")]

        def mark_evaluated(self, *args, **kwargs):
            pass

    class Detector:
        min_edge = 0.02

        def estimate_best_net_edge(self, *args):
            return 0.0

        def get_last_direction_evaluations(self, pair_id):
            assert pair_id == pair.pair_id
            return (
                CrossPlatformDirectionEvaluation(
                    pair_id=pair_id,
                    token="YES",
                    buy_platform="polymarket",
                    sell_platform="kalshi",
                    buy_price=0.51,
                    sell_price=0.49,
                    buy_liquidity=10.0,
                    sell_liquidity=10.0,
                    gross_edge=-0.02,
                    fee_cost=0.0,
                    net_edge=-0.02,
                    slippage_reserve=0.02,
                    executable_net_edge=-0.04,
                    required_net_edge=0.02,
                    suggested_size=10.0,
                    outcome="skipped",
                    reason_code="edge_below_threshold",
                ),
            )

    bot = TradingBotWithDashboard(BotConfig())
    bot._running = True
    bot._matched_pairs = [pair]
    bot.pair_monitor = PairMonitor()
    bot.cross_platform_engine = Detector()
    bot.kalshi_client = type("KalshiMetrics", (), {"request_metrics": {}})()
    bot.pair_snapshot_source = SnapshotSource()
    bot.data_feed = type(
        "LegacyCache",
        (),
        {"get_order_book": lambda *_: pytest.fail("global cache must not be read")},
    )()
    bot.paper_trade_store = PaperTradeStore(str(tmp_path / "scanner.db"))
    bot.paper_trade_store.start_run(
        starting_equity=1000.0,
        pnl_source="projected_locked_paper",
    )

    async def evaluate(requested_pair, requested_poly, requested_kalshi):
        assert (requested_pair, requested_poly, requested_kalshi) == (
            pair,
            poly_book,
            kalshi_book,
        )
        bot._running = False
        return None, None

    bot.evaluate_cross_platform_pair = evaluate

    try:
        await bot._scan_cross_platform_pairs()

        assert bot.pair_snapshot_source.calls == 1
        assert bot._kalshi_orderbooks[pair.kalshi_ticker] is kalshi_book
        assert bot.paper_trade_store.cross_platform_evaluation_funnel() == {
            "edge_below_threshold": 1,
            "pair_due": 1,
            "paired_snapshot_fresh": 1,
        }
    finally:
        bot.paper_trade_store.close()
