"""Tests for Polymarket client factory selection."""

from utils.config_loader import BotConfig, ApiConfig, ModeConfig
from polymarket_client.factory import create_polymarket_client
from polymarket_us_client import PolymarketUSClient
from polymarket_client import PolymarketClient


def test_factory_returns_us_client():
    config = BotConfig(
        api=ApiConfig(polymarket_platform="us"),
        mode=ModeConfig(trading_mode="dry_run"),
    )
    client = create_polymarket_client(config)
    assert isinstance(client, PolymarketUSClient)


def test_factory_returns_global_client():
    config = BotConfig(
        api=ApiConfig(polymarket_platform="global"),
        mode=ModeConfig(trading_mode="dry_run"),
    )
    client = create_polymarket_client(config)
    assert isinstance(client, PolymarketClient)
