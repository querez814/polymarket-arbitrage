import subprocess
from unittest.mock import Mock

import pytest

from utils.config_loader import (
    ConfigError,
    load_config,
    resolve_runtime_secrets,
    validate_config,
)


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
    assert config.risk.max_order_notional == 30.0
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


@pytest.mark.parametrize("max_order_notional", ["0", "-1", ".nan", ".inf"])
def test_rejects_non_positive_order_notional_cap(tmp_path, max_order_notional):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
risk:
  max_order_notional: {max_order_notional}
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="risk.max_order_notional must be finite and positive"
    ):
        load_config(str(config_path))


def test_rejects_order_notional_cap_above_global_exposure(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
risk:
  max_order_notional: 51
  max_global_exposure: 50
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be <= risk.max_global_exposure"):
        load_config(str(config_path))


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


def test_live_override_resolves_wallet_key_from_explicit_keychain_label(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  polymarket_private_key_keychain_label: test-wallet-label
mode:
  trading_mode: dry_run
  data_mode: real
  simulate_fills: false
""",
        encoding="utf-8",
    )
    config = load_config(str(config_path))
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout="test-wallet-key\n")
    )
    monkeypatch.setattr("utils.config_loader.Path.exists", lambda self: True)
    monkeypatch.setattr("utils.config_loader.subprocess.run", run)

    config.mode.trading_mode = "live"
    resolve_runtime_secrets(config)
    validate_config(config)
    resolve_runtime_secrets(config)

    assert config.api.private_key == "test-wallet-key"
    run.assert_called_once()
    assert run.call_args.args[0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-w",
        "-l",
        "test-wallet-label",
    ]


def test_live_mode_rejects_ambiguous_wallet_key_sources(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-wallet-key
  polymarket_private_key_keychain_label: test-wallet-label
mode:
  trading_mode: live
  data_mode: real
  simulate_fills: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="exactly one Polymarket wallet-key source"):
        load_config(str(config_path))


def test_keychain_failure_is_actionable_without_exposing_subprocess_output(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  polymarket_private_key_keychain_label: test-wallet-label
mode:
  trading_mode: dry_run
  data_mode: real
  simulate_fills: false
""",
        encoding="utf-8",
    )
    config = load_config(str(config_path))
    monkeypatch.setattr("utils.config_loader.Path.exists", lambda self: True)
    monkeypatch.setattr(
        "utils.config_loader.subprocess.run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                44, ["security"], stderr="secret-looking-subprocess-output"
            )
        ),
    )

    config.mode.trading_mode = "live"
    with pytest.raises(ConfigError) as error:
        resolve_runtime_secrets(config)

    assert "verify the item exists and access is approved" in str(error.value)
    assert "secret-looking-subprocess-output" not in str(error.value)


@pytest.mark.parametrize(
    ("field_name", "override"),
    [
        ("polymarket_rest_url", "https://clob.example.test"),
        ("polymarket_ws_url", "wss://ws.example.test/market"),
        ("gamma_api_url", "https://gamma.example.test"),
    ],
)
def test_live_global_rejects_non_production_polymarket_endpoints(
    tmp_path, field_name, override
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  {field_name}: {override}
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-wallet-key
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: false
  kalshi_enabled: false
  simulate_fills: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=field_name):
        load_config(str(config_path))


def test_live_global_rejects_non_polygon_chain(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-wallet-key
  chain_id: 1
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: false
  kalshi_enabled: false
  simulate_fills: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="chain_id must be 137"):
        load_config(str(config_path))


@pytest.mark.parametrize(
    ("field_name", "override"),
    [
        ("polymarket_us_api_url", "https://api.example.test"),
        ("polymarket_us_gateway_url", "https://gateway.example.test"),
    ],
)
def test_live_us_rejects_non_production_polymarket_endpoints(
    tmp_path, field_name, override
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  polymarket_platform: us
  {field_name}: {override}
  polymarket_us_key_id: test-key-id
  polymarket_us_secret_key: test-secret-key
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: false
  kalshi_enabled: false
  simulate_fills: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=field_name):
        load_config(str(config_path))


def test_live_kalshi_monitoring_rejects_non_production_endpoint(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-wallet-key
  kalshi_api_url: https://demo-api.kalshi.co/trade-api/v2
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: true
  kalshi_enabled: true
  simulate_fills: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="kalshi_api_url"):
        load_config(str(config_path))


def test_cross_platform_mode_requires_kalshi_monitoring(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode:
  trading_mode: dry_run
  cross_platform_enabled: true
  kalshi_enabled: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="kalshi_enabled must be true"):
        load_config(str(config_path))


@pytest.mark.parametrize(
    "api_config",
    [
        "kalshi_api_key_id: test-key-id",
        "kalshi_private_key_path: /tmp/test-kalshi-key.pem",
    ],
)
def test_kalshi_credentials_must_be_configured_as_a_pair(tmp_path, api_config):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  {api_config}
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be configured together"):
        load_config(str(config_path))


def test_kalshi_private_key_path_must_exist(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  kalshi_api_key_id: test-key-id
  kalshi_private_key_path: missing.pem
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="existing regular file"):
        load_config(str(config_path))
