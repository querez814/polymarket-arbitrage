from types import SimpleNamespace

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
            raise AssertionError("live execution must not use the detached detector path")

    class Runtime:
        def status(self):
            raise AssertionError("the owned runtime performs its own admission checks")

        async def evaluate_pair(self, pair, polymarket_book, kalshi_book):
            assert pair == "pair"
            assert polymarket_book == "poly-book"
            assert kalshi_book == "kalshi-book"
            return SimpleNamespace(opportunity="owned-opportunity", execution="execution")

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
