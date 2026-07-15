"""Factory for selecting the correct Polymarket client implementation."""

from __future__ import annotations

from polymarket_client.api import BasePolymarketClient, PolymarketClient
from utils.config_loader import BotConfig


def create_polymarket_client(config: BotConfig) -> BasePolymarketClient:
    """Return a Polymarket client for the configured platform."""
    if config.is_polymarket_us:
        from polymarket_us_client import PolymarketUSClient

        return PolymarketUSClient(
            key_id=config.api.polymarket_us_key_id,
            secret_key=config.api.polymarket_us_secret_key,
            api_base_url=config.api.polymarket_us_api_url,
            gateway_base_url=config.api.polymarket_us_gateway_url,
            timeout=config.api.timeout_seconds,
            dry_run=config.is_dry_run,
        )

    return PolymarketClient(
        rest_url=config.api.polymarket_rest_url,
        ws_url=config.api.polymarket_ws_url,
        gamma_url=config.api.gamma_api_url,
        api_key=config.api.api_key,
        api_secret=config.api.api_secret,
        passphrase=config.api.passphrase,
        private_key=config.api.private_key,
        chain_id=config.api.chain_id,
        timeout=config.api.timeout_seconds,
        max_retries=config.api.max_retries,
        retry_delay=config.api.retry_delay_seconds,
        dry_run=config.is_dry_run,
    )
