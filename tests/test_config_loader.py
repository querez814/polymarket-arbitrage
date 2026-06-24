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
