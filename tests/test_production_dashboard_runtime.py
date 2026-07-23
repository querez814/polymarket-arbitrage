from types import SimpleNamespace
import asyncio

import httpx
import pytest

from run_with_dashboard import TradingBotWithDashboard
from core.combinatorial_arb import SamePlatformArbitrageDetector
from utils.config_loader import BotConfig
from utils.task_supervision import RestartingTaskSupervisor
from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)


@pytest.mark.asyncio
async def test_live_cross_platform_pair_uses_owned_production_runtime():
    config = BotConfig()
    config.mode.trading_mode = "live"
    config.mode.cross_platform_execution_enabled = True
    bot = TradingBotWithDashboard(config)

    class Detector:
        def check_arbitrage(self, *args, **kwargs):
            raise AssertionError(
                "live execution must not use the detached detector path"
            )

    class Runtime:
        def status(self):
            raise AssertionError("the owned runtime performs its own admission checks")

        async def evaluate_pair(self, pair, polymarket_book, kalshi_book):
            assert pair == "pair"
            assert polymarket_book == "poly-book"
            assert kalshi_book == "kalshi-book"
            return SimpleNamespace(
                opportunity="owned-opportunity", execution="execution"
            )

    bot.cross_platform_engine = Detector()
    bot.production_runtime = Runtime()

    opportunity, evaluation = await bot.evaluate_cross_platform_pair(
        "pair", "poly-book", "kalshi-book"
    )

    assert opportunity == "owned-opportunity"
    assert evaluation.execution == "execution"


@pytest.mark.asyncio
async def test_live_cross_platform_pair_fails_closed_without_runtime_owner():
    config = BotConfig()
    config.mode.trading_mode = "live"
    config.mode.cross_platform_execution_enabled = True
    bot = TradingBotWithDashboard(config)
    bot.cross_platform_engine = object()

    with pytest.raises(RuntimeError, match="ownership is missing"):
        await bot.evaluate_cross_platform_pair("pair", "poly-book", "kalshi-book")


def test_live_dashboard_binds_operator_routes_to_loopback_only():
    live = BotConfig()
    live.mode.trading_mode = "live"
    dry = BotConfig()

    assert TradingBotWithDashboard(live).dashboard_host == "127.0.0.1"
    assert TradingBotWithDashboard(dry).dashboard_host == "0.0.0.0"


def test_cross_platform_readiness_requires_live_monitor_and_scanner_tasks():
    config = BotConfig()
    config.mode.cross_platform_enabled = True
    config.mode.kalshi_enabled = True
    bot = TradingBotWithDashboard(config)
    pending = SimpleNamespace(done=lambda: False)
    finished = SimpleNamespace(done=lambda: True)

    assert bot._critical_dependencies_ready() is False

    bot._kalshi_monitor_task = pending
    bot._xplat_scan_task = pending
    bot._matched_pairs = [object()]
    from dashboard.server import dashboard_state

    dashboard_state.cross_platform["matching_status"] = "complete"
    assert bot._critical_dependencies_ready() is True

    bot._xplat_scan_task = finished
    assert bot._critical_dependencies_ready() is False


@pytest.mark.asyncio
async def test_critical_background_task_completion_durably_panics_runtime():
    config = BotConfig()
    config.production.operator_token = "operator-token"
    bot = TradingBotWithDashboard(config)
    calls = []

    class Runtime:
        async def panic(self, token, *, reason):
            calls.append((token, reason))

    async def completed():
        return None

    bot.production_runtime = Runtime()
    bot._running = True
    task = asyncio.create_task(completed())
    await task

    bot._critical_task_done(task)
    await bot._critical_failure_task

    assert bot._run_failed is True
    assert calls == [
        ("operator-token", "critical cross-platform task stopped unexpectedly")
    ]


@pytest.mark.asyncio
async def test_zero_matches_is_an_honest_wait_state_not_a_started_scan():
    bot = TradingBotWithDashboard(BotConfig())
    bot._kalshi_markets = [object()]

    class Matcher:
        async def find_matches(self, polymarket_markets, kalshi_markets, on_progress):
            assert polymarket_markets == ["poly-market"]
            assert kalshi_markets == bot._kalshi_markets
            return []

        def get_cached_pairs(self):
            return []

        def get_review_candidates(self):
            return []

    bot.market_matcher = Matcher()

    await bot._run_matching_background(["poly-market"])

    from dashboard.server import dashboard_state

    assert dashboard_state.cross_platform["matching_status"] == "no_matches"
    decision = bot.decision_journal.recent()[-1]
    assert decision.reason_code == "no_equivalent_pairs"
    assert "price scan" not in decision.explanation
    assert bot._xplat_scan_task is None


@pytest.mark.asyncio
async def test_market_discovery_stays_alive_after_a_matching_cycle():
    config = BotConfig()
    config.mode.cross_platform_refresh_seconds = 3_600
    bot = TradingBotWithDashboard(config)
    bot._running = True
    bot.data_feed = SimpleNamespace(
        _markets={f"poly-{index}": object() for index in range(50)}
    )

    class KalshiClient:
        async def list_all_event_markets(self, **kwargs):
            return [object()]

    matching_completed = asyncio.Event()

    async def complete_one_cycle(polymarket_markets):
        matching_completed.set()

    bot.kalshi_client = KalshiClient()
    bot._run_matching_background = complete_one_cycle

    task = asyncio.create_task(bot._start_kalshi_monitoring())
    await asyncio.wait_for(matching_completed.wait(), timeout=2)

    assert task.done() is False

    bot._running = False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_market_discovery_recovers_after_transient_upstream_503():
    config = BotConfig()
    config.mode.cross_platform_refresh_seconds = 0
    bot = TradingBotWithDashboard(config)
    bot._running = True
    bot.data_feed = SimpleNamespace(
        _markets={f"poly-{index}": object() for index in range(50)}
    )
    requests = 0
    retry_delays = []

    class KalshiClient:
        async def list_all_event_markets(self, **kwargs):
            nonlocal requests
            requests += 1
            if requests == 1:
                request = httpx.Request(
                    "GET", "https://trading-api.kalshi.com/events"
                )
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError(
                    "temporary upstream failure",
                    request=request,
                    response=response,
                )
            return [object()]

    async def matching_completed(_polymarket_markets):
        bot._running = False

    async def fake_sleep(delay):
        retry_delays.append(delay)

    bot.kalshi_client = KalshiClient()
    bot._run_matching_background = matching_completed
    bot._discovery_supervisor = RestartingTaskSupervisor(
        "cross-platform discovery",
        base_delay=2.0,
        sleep=fake_sleep,
        on_retry=bot._on_cross_platform_task_retry,
    )

    await bot._discovery_supervisor.run(
        bot._start_kalshi_monitoring,
        should_run=lambda: bot._running,
    )

    assert requests == 2
    assert retry_delays == [2.0]
    assert bot._run_failed is False

    from dashboard.server import dashboard_state

    assert (
        dashboard_state.cross_platform["task_restarts"][
            "cross-platform discovery"
        ]
        >= 1
    )
    assert dashboard_state.cross_platform["last_transient_error"]["attempt"] == 1


def test_bundle_scan_activity_is_visible_when_no_candidates_exist(caplog):
    caplog.set_level("INFO")
    config = BotConfig()
    bot = TradingBotWithDashboard(config)
    bot.same_platform_detector = SamePlatformArbitrageDetector(min_edge=0.02)
    bot.data_feed = SimpleNamespace(get_all_market_states=lambda: {})

    bot._run_combinatorial_scan()

    from dashboard.server import dashboard_state

    metrics = dashboard_state.operational["bundle_arb"]
    assert metrics["scans"] == 1
    assert metrics["states"] == 0
    assert metrics["eligible_groups"] == 0
    assert metrics["opportunities"] == 0
    assert "Bundle arb scan active" in caplog.text


def _priced_book(market_id, *, yes_bid, yes_ask, no_bid, no_ask):
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            TokenType.YES,
            bids=OrderBookSide([PriceLevel(yes_bid, 10)]),
            asks=OrderBookSide([PriceLevel(yes_ask, 10)]),
        ),
        no=TokenOrderBook(
            TokenType.NO,
            bids=OrderBookSide([PriceLevel(no_bid, 10)]),
            asks=OrderBookSide([PriceLevel(no_ask, 10)]),
        ),
    )


def test_matched_pair_dashboard_snapshot_uses_live_books():
    bot = TradingBotWithDashboard(BotConfig())
    pair = SimpleNamespace(
        polymarket_id="poly-1",
        kalshi_ticker="KX-1",
        polymarket_question="Will Alice win?",
        kalshi_title="Alice wins?",
        similarity_score=0.94,
        category="politics",
    )
    poly_book = _priced_book(
        "poly-1", yes_bid=0.42, yes_ask=0.46, no_bid=0.54, no_ask=0.58
    )
    kalshi_book = _priced_book(
        "KX-1", yes_bid=0.51, yes_ask=0.53, no_bid=0.47, no_ask=0.49
    )
    bot.data_feed = SimpleNamespace(get_order_book=lambda _market_id: poly_book)
    bot._kalshi_orderbooks["KX-1"] = kalshi_book

    row = bot._matched_pair_dashboard_row(pair)

    assert row["poly_yes"] == pytest.approx(0.42)
    assert row["poly_no"] == pytest.approx(0.54)
    assert row["kalshi_yes"] == pytest.approx(0.51)
    assert row["kalshi_no"] == pytest.approx(0.47)


def test_matched_pair_dashboard_snapshot_preserves_missing_prices_as_null():
    bot = TradingBotWithDashboard(BotConfig())
    pair = SimpleNamespace(
        polymarket_id="poly-1",
        kalshi_ticker="KX-1",
        polymarket_question="Will Alice win?",
        kalshi_title="Alice wins?",
        similarity_score=0.94,
        category="politics",
    )
    bot.data_feed = SimpleNamespace(get_order_book=lambda _market_id: None)

    row = bot._matched_pair_dashboard_row(pair)

    assert row["poly_yes"] is None
    assert row["kalshi_yes"] is None


@pytest.mark.asyncio
async def test_bot_shutdown_waits_for_dashboard_server_to_exit():
    bot = TradingBotWithDashboard(BotConfig())
    server = SimpleNamespace(should_exit=False)

    async def serve_until_stopped():
        while not server.should_exit:
            await asyncio.sleep(0)

    bot._server = server
    task = asyncio.create_task(serve_until_stopped())
    bot._server_task = task

    await bot.stop()

    assert server.should_exit is True
    assert task.done()


@pytest.mark.asyncio
async def test_bot_shutdown_continues_when_no_active_paper_run_exists(tmp_path):
    from utils.paper_trade_store import PaperTradeStore

    bot = TradingBotWithDashboard(BotConfig())
    bot.paper_trade_store = PaperTradeStore(str(tmp_path / "paper.db"))

    await bot.stop()

    assert bot.paper_trade_store is None


@pytest.mark.asyncio
async def test_critical_failure_marks_persisted_run_failed(tmp_path):
    from utils.paper_trade_store import PaperTradeStore

    db_path = tmp_path / "paper.db"
    bot = TradingBotWithDashboard(BotConfig())
    bot.paper_trade_store = PaperTradeStore(str(db_path))
    bot.paper_trade_store.start_run(
        starting_equity=1000.0,
        pnl_source="projected_locked_paper",
    )
    bot._startup_complete = True
    bot._run_failed = True

    await bot.stop()

    reopened = PaperTradeStore(str(db_path))
    try:
        assert reopened.recent_runs(limit=1)[0].status == "failed"
    finally:
        reopened.close()
