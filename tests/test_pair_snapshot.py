import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core.cross_platform_arb import CrossPlatformDirectionEvaluation, MarketPair
from core.pair_monitoring import DuePair
from core.pair_snapshot import (
    PairSnapshot,
    PairSnapshotError,
    PairSnapshotSource,
    executable_top_capacity,
)
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
from utils.http_resilience import CircuitOpenError
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


def test_preflight_capacity_requires_one_complete_executable_direction():
    now = datetime.now(timezone.utc)
    poly = _book_with_depth("poly", now)
    kalshi = _book_with_depth("kalshi", now)
    snapshot = PairSnapshot("pair", poly, kalshi, 5)

    assert executable_top_capacity(snapshot) == pytest.approx(10)

    kalshi.yes.bids.levels.clear()
    poly.yes.bids.levels.clear()
    assert executable_top_capacity(snapshot) == pytest.approx(0)


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
async def test_pair_snapshot_converts_open_circuit_to_pair_local_retry():
    class PolymarketClient:
        async def get_orderbook(self, market_id):
            raise CircuitOpenError("endpoint circuit open for 12.0s")

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

    assert captured.value.reason_code == "paired_snapshot_upstream_unavailable"
    assert captured.value.evidence == {"error_type": "CircuitOpenError"}


@pytest.mark.asyncio
async def test_pair_snapshot_converts_http_status_to_pair_local_retry():
    request = httpx.Request("GET", "https://venue.example/book")
    response = httpx.Response(503, request=request)

    class PolymarketClient:
        async def get_orderbook(self, market_id):
            raise httpx.HTTPStatusError(
                "service unavailable", request=request, response=response
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

    assert captured.value.reason_code == "paired_snapshot_upstream_unavailable"
    assert captured.value.evidence == {"error_type": "HTTPStatusError"}


@pytest.mark.asyncio
async def test_discovery_preflight_keeps_only_fresh_pairs_with_executable_capacity():
    now = datetime.now(timezone.utc)
    usable = MarketPair(
        "poly-usable",
        "KX-USABLE",
        "Usable?",
        "Usable?",
        0.98,
        event_family="finance:cpi:2026-07",
    )
    thin = MarketPair(
        "poly-thin",
        "KX-THIN",
        "Thin?",
        "Thin?",
        0.98,
        event_family="politics:nominee:2028",
    )

    class SnapshotSource:
        async def fetch(self, pair):
            poly = _book_with_depth(pair.polymarket_id, now)
            kalshi = _book_with_depth(pair.kalshi_ticker, now)
            if pair is thin:
                kalshi.yes.bids.levels[0].size = 0.5
                poly.yes.bids.levels[0].size = 0.5
            return PairSnapshot(pair.pair_id, poly, kalshi, 5)

    config = BotConfig()
    config.trading.cross_platform_min_executable_size = 1
    bot = TradingBotWithDashboard(config)
    bot.pair_snapshot_source = SnapshotSource()

    passed, evidence = await bot._preflight_verified_pairs([thin, usable])

    assert passed == [usable]
    assert evidence[usable.pair_id]["result"] == "usable"
    assert evidence[thin.pair_id]["result"] == "insufficient_executable_liquidity"
    assert bot._preflight_snapshots[usable.pair_id].pair_id == usable.pair_id
    assert usable.discovery_priority > thin.discovery_priority


@pytest.mark.asyncio
async def test_discovery_preflight_isolates_one_unexpected_pair_failure():
    now = datetime.now(timezone.utc)
    broken = MarketPair("poly-broken", "KX-BROKEN", "Broken?", "Broken?", 0.98)
    usable = MarketPair("poly-good", "KX-GOOD", "Good?", "Good?", 0.98)

    class SnapshotSource:
        async def fetch(self, pair):
            if pair is broken:
                raise ValueError("malformed remote payload")
            return PairSnapshot(
                pair.pair_id,
                _book_with_depth(pair.polymarket_id, now),
                _book_with_depth(pair.kalshi_ticker, now),
                5,
            )

    bot = TradingBotWithDashboard(BotConfig())
    bot.config.trading.cross_platform_min_executable_size = 1
    bot.pair_snapshot_source = SnapshotSource()

    passed, evidence = await bot._preflight_verified_pairs([broken, usable])

    assert passed == [usable]
    assert evidence[broken.pair_id]["result"] == "preflight_unexpected_error"


@pytest.mark.asyncio
async def test_transient_preflight_failure_stays_on_scanner_retry_cadence():
    pair = MarketPair("poly-timeout", "KX-TIMEOUT", "Timeout?", "Timeout?", 0.98)

    class SnapshotSource:
        async def fetch(self, requested):
            raise PairSnapshotError("paired_snapshot_timeout")

    bot = TradingBotWithDashboard(BotConfig())
    bot.pair_snapshot_source = SnapshotSource()

    passed, evidence = await bot._preflight_verified_pairs([pair])

    assert passed == [pair]
    assert evidence[pair.pair_id]["result"] == "retry_pending"
    assert pair.pair_id not in bot._preflight_snapshots


@pytest.mark.asyncio
async def test_upstream_preflight_failure_stays_on_scanner_retry_cadence():
    pair = MarketPair("poly-circuit", "KX-CIRCUIT", "Circuit?", "Circuit?", 0.98)

    class SnapshotSource:
        async def fetch(self, requested):
            raise PairSnapshotError("paired_snapshot_upstream_unavailable")

    bot = TradingBotWithDashboard(BotConfig())
    bot.pair_snapshot_source = SnapshotSource()

    passed, evidence = await bot._preflight_verified_pairs([pair])

    assert passed == [pair]
    assert evidence[pair.pair_id]["result"] == "retry_pending"


@pytest.mark.asyncio
async def test_empty_preflight_book_stays_on_scanner_retry_cadence():
    pair = MarketPair("poly-empty", "KX-EMPTY", "Empty?", "Empty?", 0.98)

    class SnapshotSource:
        async def fetch(self, requested):
            raise PairSnapshotError("empty_polymarket_orderbook")

    bot = TradingBotWithDashboard(BotConfig())
    bot.pair_snapshot_source = SnapshotSource()

    passed, evidence = await bot._preflight_verified_pairs([pair])

    assert passed == [pair]
    assert evidence[pair.pair_id]["result"] == "retry_pending"


@pytest.mark.asyncio
async def test_preflight_applies_same_liquidity_fraction_as_execution():
    now = datetime.now(timezone.utc)
    pair = MarketPair("poly-thin", "KX-THIN", "Thin?", "Thin?", 0.98)

    class SnapshotSource:
        async def fetch(self, requested):
            poly = _book_with_depth(requested.polymarket_id, now)
            kalshi = _book_with_depth(requested.kalshi_ticker, now)
            for book in (poly, kalshi):
                book.yes.bids.levels[0].size = 2
                book.yes.asks.levels[0].size = 2
            return PairSnapshot(requested.pair_id, poly, kalshi, 5)

    config = BotConfig()
    config.trading.cross_platform_min_executable_size = 1
    config.trading.cross_platform_max_liquidity_fraction = 0.25
    bot = TradingBotWithDashboard(config)
    bot.pair_snapshot_source = SnapshotSource()

    passed, evidence = await bot._preflight_verified_pairs([pair])

    assert passed == []
    assert evidence[pair.pair_id]["executable_capacity"] == pytest.approx(0.5)


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


@pytest.mark.asyncio
async def test_scanner_isolates_unexpected_snapshot_failure_and_evaluates_next_pair():
    from dashboard.server import dashboard_state

    broken = MarketPair("poly-broken", "KX-BROKEN", "Broken?", "Broken?", 0.98)
    usable = MarketPair("poly-good", "KX-GOOD", "Good?", "Good?", 0.98)
    now = datetime.now(timezone.utc)

    class SnapshotSource:
        async def fetch(self, pair):
            if pair is broken:
                raise ValueError("malformed payload")
            return PairSnapshot(
                pair.pair_id,
                _book_with_depth(pair.polymarket_id, now),
                _book_with_depth(pair.kalshi_ticker, now),
                5,
            )

    class PairMonitor:
        def due_pairs(self, pairs):
            return [DuePair(pair=pair, tier="cold") for pair in pairs]

        def mark_evaluated(self, *args, **kwargs):
            pass

    class Detector:
        min_edge = 0.02

        def get_last_direction_evaluations(self, pair_id):
            return ()

    bot = TradingBotWithDashboard(BotConfig())
    bot._running = True
    bot._matched_pairs = [broken, usable]
    bot.pair_monitor = PairMonitor()
    bot.cross_platform_engine = Detector()
    bot.kalshi_client = type("KalshiMetrics", (), {"request_metrics": {}})()
    bot.pair_snapshot_source = SnapshotSource()
    evaluated = []

    async def evaluate(pair, polymarket_book, kalshi_book):
        evaluated.append(pair.pair_id)
        bot._running = False
        return None, None

    bot.evaluate_cross_platform_pair = evaluate

    await bot._scan_cross_platform_pairs()

    assert evaluated == [usable.pair_id]
    assert dashboard_state.cross_platform["scan_status"] == "degraded"
    assert dashboard_state.cross_platform["last_pair_snapshot_unexpected_error"] == {
        "error_type": "ValueError",
        "pair_id": broken.pair_id,
    }


@pytest.mark.asyncio
async def test_scanner_rejects_cached_preflight_snapshot_that_aged_out():
    stale_pair = MarketPair("poly-stale", "KX-STALE", "Stale?", "Stale?", 0.98)
    fresh_pair = MarketPair("poly-fresh", "KX-FRESH", "Fresh?", "Fresh?", 0.98)
    now = datetime.now(timezone.utc)

    class SnapshotSource:
        async def fetch(self, pair):
            return PairSnapshot(
                pair.pair_id,
                _book_with_depth(pair.polymarket_id, now),
                _book_with_depth(pair.kalshi_ticker, now),
                5,
            )

    class PairMonitor:
        def due_pairs(self, pairs):
            return [DuePair(pair=pair, tier="cold") for pair in pairs]

        def mark_evaluated(self, *args, **kwargs):
            pass

    class Detector:
        min_edge = 0.02

        def get_last_direction_evaluations(self, pair_id):
            return ()

    bot = TradingBotWithDashboard(BotConfig())
    bot._running = True
    bot._matched_pairs = [stale_pair, fresh_pair]
    bot._preflight_snapshots[stale_pair.pair_id] = PairSnapshot(
        stale_pair.pair_id,
        _book_with_depth(stale_pair.polymarket_id, now - timedelta(seconds=10)),
        _book_with_depth(stale_pair.kalshi_ticker, now - timedelta(seconds=10)),
        5,
    )
    bot.pair_monitor = PairMonitor()
    bot.cross_platform_engine = Detector()
    bot.kalshi_client = type("KalshiMetrics", (), {"request_metrics": {}})()
    bot.pair_snapshot_source = SnapshotSource()
    evaluated = []

    async def evaluate(pair, polymarket_book, kalshi_book):
        evaluated.append(pair.pair_id)
        bot._running = False
        return None, None

    bot.evaluate_cross_platform_pair = evaluate

    await bot._scan_cross_platform_pairs()

    assert evaluated == [fresh_pair.pair_id]
