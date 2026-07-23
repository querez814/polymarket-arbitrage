from types import SimpleNamespace
import asyncio

import pytest

from run_with_dashboard import TradingBotWithDashboard
from utils.config_loader import BotConfig


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
