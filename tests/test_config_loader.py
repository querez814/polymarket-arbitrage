import pytest

from utils.config_loader import ConfigError, load_config, validate_config


def test_aggressive_profile_applies_riskier_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
trading:
  risk_profile: aggressive
risk:
  kill_switch_enabled: true
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert config.trading.risk_profile == "aggressive"
    assert config.trading.min_edge == 0.005
    assert config.trading.mm_enabled is True
    assert config.trading.bundle_cooldown_seconds == 0.5
    assert config.risk.max_position_per_market == 35.0
    assert config.risk.strategy_exposure_limits["market_making"] == 35.0


def test_explicit_values_override_aggressive_profile_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
trading:
  risk_profile: aggressive
  min_edge: 0.02
risk:
  max_global_exposure: 250
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert config.trading.min_edge == 0.02
    assert config.risk.max_global_exposure == 250


def test_dry_run_does_not_simulate_fills_by_default(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert config.mode.simulate_fills is False
    assert config.mode.fill_probability == 0.0


def test_monitoring_defaults_include_paper_trade_db_and_timezone(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert config.monitoring.paper_trade_db_path == "data/paper_trades.db"
    assert config.monitoring.display_timezone == "America/New_York"


def test_live_cli_override_must_be_revalidated(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )
    config = load_config(str(config_path))

    config.mode.trading_mode = "live"

    with pytest.raises(ConfigError, match="api.api_key is required"):
        validate_config(config)


def test_live_mode_rejects_simulation_data_even_with_credentials(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-private-key
mode:
  trading_mode: live
  data_mode: simulation
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="mode.data_mode must be 'real' in live mode"):
        load_config(str(config_path))


def test_live_mode_rejects_hypothetical_fills(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-private-key
mode:
  trading_mode: live
  data_mode: real
  simulate_fills: true
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="mode.simulate_fills must be false in live mode"
    ):
        load_config(str(config_path))
