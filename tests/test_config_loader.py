"""
Tests for safe configuration loading and trading-mode gates.
"""

import pytest

from utils.config_loader import (
    LIVE_CONFIRMATION_ENV,
    LIVE_CONFIRMATION_VALUE,
    ConfigError,
    get_default_config,
    load_config,
    validate_config,
)
from core.paper_risk import build_paper_runtime_settings


def write_config(tmp_path, content: str):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    return path


def test_default_config_is_scanner_and_non_executing():
    config = get_default_config()
    validate_config(config)

    assert config.trading_mode == "scanner"
    assert config.is_scanner is True
    assert config.is_dry_run is True
    assert config.allows_execution is False
    assert config.simulates_orders is False
    assert config.trading.bundle_short_enabled is False
    assert config.trading.mm_enabled is False


def test_legacy_dry_run_mode_maps_to_paper(tmp_path):
    path = write_config(
        tmp_path,
        """
mode:
  trading_mode: dry_run
""",
    )

    config = load_config(str(path))

    assert config.trading_mode == "paper"
    assert config.mode.trading_mode == "paper"
    assert config.is_paper is True
    assert config.allows_execution is True


def test_placeholder_secrets_are_scrubbed(tmp_path):
    path = write_config(
        tmp_path,
        """
api:
  api_key: YOUR_API_KEY_HERE
  private_key: YOUR_PRIVATE_KEY_HERE
mode:
  trading_mode: scanner
""",
    )

    config = load_config(str(path))

    assert config.api.api_key == ""
    assert config.api.private_key == ""


def test_env_local_loads_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
    (tmp_path / ".env.local").write_text("POLYMARKET_API_KEY=local-key\n")
    path = write_config(
        tmp_path,
        """
mode:
  trading_mode: scanner
""",
    )

    config = load_config(str(path))

    assert config.api.api_key == "local-key"


def test_live_mode_refuses_until_execution_layer_exists(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_CONFIRMATION_ENV, LIVE_CONFIRMATION_VALUE)
    path = write_config(
        tmp_path,
        """
api:
  api_key: test-key
  private_key: test-private-key
mode:
  trading_mode: live
  live_trading_enabled: true
  manual_live_approval: true
venue_access:
  polymarket_venue: global
  kalshi_environment: production
  no_geoblock_workarounds: true
""",
    )

    with pytest.raises(ConfigError, match="live order execution is not implemented"):
        load_config(str(path))


def test_aggressive_paper_profile_changes_only_paper_runtime(tmp_path):
    path = write_config(
        tmp_path,
        """
trading:
  min_edge: 0.01
  default_order_size: 5
  max_order_size: 10
mode:
  trading_mode: scanner
paper_risk:
  profile: aggressive
  paper_enable_bundle_short: true
  paper_enable_market_making: true
  paper_enable_cross_platform_synthetic: true
  aggressive_min_edge: 0.0025
  aggressive_default_order_size: 100
  aggressive_max_order_size: 500
  aggressive_fill_probability: 0.95
""",
    )

    scanner_config = load_config(str(path))
    scanner_settings = build_paper_runtime_settings(scanner_config)

    assert scanner_settings.min_edge == 0.01
    assert scanner_settings.default_order_size == 5
    assert scanner_settings.max_order_size == 10
    assert scanner_settings.bundle_short_enabled is False
    assert scanner_settings.market_making_enabled is False
    assert scanner_settings.cross_platform_synthetic_enabled is False

    scanner_config.mode.trading_mode = "paper"
    validate_config(scanner_config)
    paper_settings = build_paper_runtime_settings(scanner_config)

    assert paper_settings.min_edge == 0.0025
    assert paper_settings.default_order_size == 100
    assert paper_settings.max_order_size == 500
    assert paper_settings.bundle_short_enabled is True
    assert paper_settings.market_making_enabled is True
    assert paper_settings.cross_platform_synthetic_enabled is True
