import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from utils.config_loader import (
    BotConfig,
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
    assert config.risk.max_open_orders == 8
    assert config.risk.max_open_positions == 8
    assert config.risk.max_order_attempts_per_minute == 30
    assert config.risk.max_daily_order_attempts == 500
    assert config.risk.strategy_exposure_limits["market_making"] == 35.0


def test_platform_opportunity_config_loads_bounded_shadow_lane(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode:
  trading_mode: dry_run
platform_opportunity:
  enabled: true
  catalog_path: data/research.db
  lookahead_days: 7
  max_hot_contracts: 40
  min_liquidity: 250
  min_volume: 500
  queue_capacity: 2000
  hot_poll_seconds: 2
  political_max_events: 2
  political_max_contracts_per_event: 3
  political_lookahead_days: 5
  political_warm_before_hours: 12
  political_hot_before_minutes: 30
  political_warm_poll_seconds: 45
  political_hot_poll_seconds: 3
  political_event_poll_seconds: 1
  political_cooldown_poll_seconds: 15
  political_cooldown_after_hours: 4
  reviewed_pinned_event_ids: [kalshi:KXTRUMPMENTION-26AUG10]
""",
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert config.platform_opportunity.enabled is True
    assert config.platform_opportunity.catalog_path == "data/research.db"
    assert config.platform_opportunity.max_hot_contracts == 40
    assert config.platform_opportunity.queue_capacity == 2000
    assert config.platform_opportunity.political_max_events == 2
    assert config.platform_opportunity.political_hot_before_minutes == 30
    assert config.platform_opportunity.reviewed_pinned_event_ids == [
        "kalshi:KXTRUMPMENTION-26AUG10"
    ]


def test_political_v2_profile_is_isolated_real_data_shadow_collection():
    config = load_config(
        str(Path(__file__).parents[1] / "config.paper.political-v2.yaml")
    )

    assert config.is_dry_run is True
    assert config.mode.data_mode == "real"
    assert config.mode.simulate_fills is False
    assert config.mode.cross_platform_enabled is False
    assert config.mode.cross_platform_execution_enabled is False
    assert config.mode.semantic_matching_enabled is False
    assert config.mode.semantic_cache_path == "data/political_v2_semantic_cache.db"
    assert config.platform_opportunity.experiment_id == (
        "political-event-reaction-v2-2026-08-09"
    )
    assert config.platform_opportunity.catalog_path == (
        "data/political_v2_platform_opportunities.db"
    )
    assert config.monitoring.paper_trade_db_path == "data/political_v2_paper_trades.db"
    assert (
        config.production.execution_journal_path
        == "data/political_v2_execution_journal.sqlite3"
    )
    assert config.production.operator_state_path == "data/political_v2_operator_state.sqlite3"
    assert config.platform_opportunity.reviewed_pinned_event_ids == [
        "kalshi:KXTRUMPMENTION-26AUG10",
        "kalshi:KXTRUMPSAY-26AUG10",
    ]


def test_political_v2_profile_disables_legacy_discovery_and_semantic_runtime():
    config = load_config(
        str(Path(__file__).parents[1] / "config.paper.political-v2.yaml")
    )

    from run_with_dashboard import TradingBotWithDashboard

    bot = TradingBotWithDashboard(config)

    assert bot.cross_platform_discovery_enabled is False
    assert bot._semantic_embedder is None
    assert bot.cross_platform_engine is None
    assert bot.kalshi_client is None


@pytest.mark.parametrize(
    "collision_field",
    ["paper", "execution", "operator", "semantic"],
)
def test_platform_opportunity_store_rejects_critical_database_collision(
    tmp_path, collision_field
):
    shared = tmp_path / "shared.db"
    values = {
        "paper": tmp_path / "paper.db",
        "execution": tmp_path / "execution.db",
        "operator": tmp_path / "operator.db",
        "semantic": tmp_path / "semantic.db",
    }
    values[collision_field] = shared
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
mode:
  trading_mode: dry_run
  semantic_cache_path: {values['semantic']}
monitoring:
  paper_trade_db_path: {values['paper']}
production:
  execution_journal_path: {values['execution']}
  operator_state_path: {values['operator']}
platform_opportunity:
  enabled: true
  catalog_path: {shared}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must not share a path"):
        load_config(str(config_path))


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


@pytest.mark.parametrize("strategy_limit", [".nan", ".inf", "-1"])
def test_rejects_invalid_strategy_exposure_limit(tmp_path, strategy_limit):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
risk:
  strategy_exposure_limits:
    cross_platform_arb: {strategy_limit}
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be finite and non-negative"):
        load_config(str(config_path))


@pytest.mark.parametrize("observations", [".nan", "1.5", "true"])
def test_paper_confirmation_observations_must_be_an_integer(tmp_path, observations):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
trading:
  mm_enabled: false
mode:
  trading_mode: dry_run
  data_mode: real
  cross_platform_enabled: true
  kalshi_enabled: true
  paper_locked_arb_enabled: true
  paper_confirmation_observations: {observations}
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="paper_confirmation_observations must be an integer"
    ):
        load_config(str(config_path))


@pytest.mark.parametrize("max_open_orders", ["0", "-1", "1.5", "true"])
def test_rejects_invalid_open_order_cap(tmp_path, max_open_orders):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
risk:
  max_open_orders: {max_open_orders}
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="risk.max_open_orders must be a positive integer"
    ):
        load_config(str(config_path))


@pytest.mark.parametrize("max_open_positions", ["0", "-1", "1.5", "true"])
def test_rejects_invalid_open_position_cap(tmp_path, max_open_positions):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
risk:
  max_open_positions: {max_open_positions}
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="risk.max_open_positions must be a positive integer"
    ):
        load_config(str(config_path))


@pytest.mark.parametrize(
    "field_name", ["max_order_attempts_per_minute", "max_daily_order_attempts"]
)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "true"])
def test_rejects_invalid_order_attempt_caps(tmp_path, field_name, value):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
risk:
  {field_name}: {value}
mode:
  trading_mode: dry_run
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match=rf"risk.{field_name} must be a positive integer"
    ):
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
    assert config.production.execution_journal_path == "data/execution_journal.sqlite3"
    assert config.production.operator_state_path == "data/operator_state.sqlite3"


def test_production_runtime_secrets_are_loaded_from_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mode:\n  trading_mode: dry_run\n", encoding="utf-8")
    monkeypatch.setenv("NIGHTWATCH_OPERATOR_TOKEN", "x" * 32)
    monkeypatch.setenv(
        "NIGHTWATCH_ALERT_WEBHOOK_URL", "https://alerts.example.test/nightwatch"
    )
    monkeypatch.setenv("NIGHTWATCH_ALERT_WEBHOOK_TOKEN", "alert-secret")

    config = load_config(str(config_path))

    assert config.production.operator_token == "x" * 32
    assert (
        config.production.alert_webhook_url == "https://alerts.example.test/nightwatch"
    )
    assert config.production.alert_webhook_token == "alert-secret"


def test_live_cross_platform_requires_fail_closed_production_controls(tmp_path):
    config = BotConfig()
    config.mode.trading_mode = "live"
    config.mode.data_mode = "real"
    config.mode.cross_platform_enabled = True
    config.mode.cross_platform_execution_enabled = True
    config.mode.kalshi_enabled = True
    config.mode.simulate_fills = False
    config.trading.bundle_arb_enabled = False
    config.trading.mm_enabled = False
    config.api.api_key = "test-key"
    config.api.api_secret = "test-secret"
    config.api.passphrase = "test-passphrase"
    config.api.private_key = "test-private-key"
    config.api.kalshi_api_key_id = "test-kalshi-key"
    config.api.kalshi_private_key_path = str(tmp_path / "kalshi.pem")
    (tmp_path / "kalshi.pem").write_text("test", encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        validate_config(config)

    message = str(error.value)
    assert "production.operator_token" in message
    assert "production.alert_webhook_url" in message
    assert "risk.strategy_exposure_limits.cross_platform_arb" in message
    assert "risk.whitelist" in message
    assert "production.alert_webhook_token" in message


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


@pytest.mark.parametrize(
    "enabled_strategy",
    ["bundle_arb_enabled", "mm_enabled"],
)
def test_live_mode_rejects_strategies_outside_recovery_admission(
    tmp_path, enabled_strategy
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-private-key
trading:
  bundle_arb_enabled: false
  mm_enabled: false
  {enabled_strategy}: true
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: false
  kalshi_enabled: false
  simulate_fills: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="crash-safe recovery admission path"):
        load_config(str(config_path))


@pytest.mark.parametrize(
    ("template_name", "environment"),
    [
        (
            "config.live.yaml.example",
            {
                "POLYMARKET_API_KEY": "test-key",
                "POLYMARKET_API_SECRET": "test-secret",
                "POLYMARKET_PASSPHRASE": "test-passphrase",
                "POLYMARKET_PRIVATE_KEY": "test-private-key",
            },
        ),
        (
            "config.live.us.yaml.example",
            {
                "POLYMARKET_PLATFORM": "us",
                "POLYMARKET_US_KEY_ID": "test-key-id",
                "POLYMARKET_US_SECRET_KEY": "test-secret-key",
            },
        ),
    ],
)
def test_tracked_live_templates_disable_unrecovered_execution(
    monkeypatch, template_name, environment
):
    for variable in (
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_PLATFORM",
        "POLYMARKET_US_KEY_ID",
        "POLYMARKET_US_SECRET_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)

    template = Path(__file__).parents[1] / template_name
    config = load_config(str(template))

    assert config.is_live
    assert config.trading.bundle_arb_enabled is False
    assert config.trading.mm_enabled is False
    assert config.mode.cross_platform_execution_enabled is False


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
trading:
  bundle_arb_enabled: false
  mm_enabled: false
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


def test_live_mode_rejects_secrets_embedded_in_tracked_config(tmp_path, monkeypatch):
    config_path = tmp_path / "tracked-live.yaml"
    config_path.write_text(
        """
api:
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
    monkeypatch.setattr("utils.config_loader._is_git_tracked", lambda path: True)

    with pytest.raises(ConfigError, match="tracked by Git must not contain secret"):
        load_config(str(config_path))


def test_live_mode_accepts_runtime_secrets_with_tracked_config(tmp_path, monkeypatch):
    config_path = tmp_path / "tracked-live.yaml"
    config_path.write_text(
        """
trading:
  bundle_arb_enabled: false
  mm_enabled: false
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: false
  kalshi_enabled: false
  simulate_fills: false
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("utils.config_loader._is_git_tracked", lambda path: True)
    monkeypatch.setenv("POLYMARKET_API_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "test-secret")
    monkeypatch.setenv("POLYMARKET_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-wallet-key")

    config = load_config(str(config_path))

    assert config.is_live


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


def test_live_kalshi_monitoring_rejects_legacy_elections_endpoint(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
api:
  api_key: test-key
  api_secret: test-secret
  passphrase: test-passphrase
  private_key: test-wallet-key
  kalshi_api_url: https://api.elections.kalshi.com/trade-api/v2
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: true
  kalshi_enabled: true
  simulate_fills: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="external-api.kalshi.com"):
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


def test_production_shaped_paper_config_is_real_data_and_fixed_bankroll():
    config = load_config(
        str(Path(__file__).parents[1] / "config.paper.production.yaml")
    )

    assert config.is_dry_run is True
    assert config.use_simulation is False
    assert config.mode.dry_run_initial_balance == 5_000
    assert config.mode.cross_platform_execution_enabled is False
    assert config.mode.polymarket_broad_orderbook_stream_enabled is False
    assert config.mode.paper_locked_arb_enabled is True
    assert config.mode.simulate_fills is False
    assert config.trading.bundle_arb_enabled is False
    assert config.trading.mm_enabled is False
    assert config.mode.semantic_matching_enabled is True
    assert config.mode.semantic_top_k == 20
    assert config.mode.semantic_auto_approve_confidence == pytest.approx(0.90)
    assert config.mode.semantic_verification_model == "gpt-5.6-terra"
    assert config.mode.semantic_family_cap_share == pytest.approx(0.40)
    assert config.mode.semantic_category_cap_share == pytest.approx(0.60)
    assert config.mode.semantic_exploration_share == pytest.approx(0.10)
    assert config.mode.semantic_book_preflight_enabled is True
    assert config.mode.semantic_min_polymarket_liquidity == pytest.approx(1.0)
    assert config.mode.semantic_min_polymarket_volume_24h == pytest.approx(1.0)
    assert config.mode.semantic_min_kalshi_volume == 1
    assert config.mode.semantic_min_kalshi_open_interest == 1
    assert config.trading.cross_platform_min_executable_size == pytest.approx(1.0)
    assert config.trading.cross_platform_max_order_size == pytest.approx(100.0)
    assert config.mode.paper_liquidity_fraction == pytest.approx(0.20)
    assert config.risk.max_order_notional == pytest.approx(100.0)
    assert config.risk.max_position_per_market == pytest.approx(250.0)
    assert config.risk.max_global_exposure == pytest.approx(1_000.0)
    assert config.news_catalyst.enabled is False
    assert config.news_catalyst.apply_priority_boost is False
    assert config.news_catalyst.scan_interval_seconds == pytest.approx(1800)
    assert config.news_catalyst.max_daily_api_calls == 60
    assert config.news_catalyst.mispricing_detector_enabled is False
    assert config.event_week.enabled is True
    assert config.event_week.lookahead_days == pytest.approx(7)
    assert config.event_week.calendar_refresh_seconds == pytest.approx(21600)
    assert config.event_week.burst_interval_seconds == pytest.approx(1.0)
    assert config.event_week.max_verification_candidates_per_cycle == 500


def test_broad_orderbook_stream_flag_requires_real_boolean():
    config = BotConfig()
    config.mode.polymarket_broad_orderbook_stream_enabled = "false"

    with pytest.raises(ConfigError, match="must be a boolean"):
        validate_config(config)


def test_event_week_limits_reject_invalid_types_without_crashing_validation():
    config = BotConfig()
    config.event_week.max_events_per_refresh = "many"
    config.event_week.max_verification_candidates_per_event = "all"
    config.event_week.max_verification_candidates_per_cycle = "unlimited"

    with pytest.raises(ConfigError) as error:
        validate_config(config)

    assert "event_week.max_events_per_refresh must be a positive integer" in str(
        error.value
    )
    assert (
        "event_week.max_verification_candidates_per_event must be a positive integer"
        in str(error.value)
    )
    assert (
        "event_week.max_verification_candidates_per_cycle must be a positive integer"
        in str(error.value)
    )


def test_paper_locked_arb_rejects_random_fill_mode():
    config = BotConfig()
    config.mode.paper_locked_arb_enabled = True
    config.mode.simulate_fills = True
    config.trading.bundle_arb_enabled = False
    config.trading.mm_enabled = False

    with pytest.raises(ConfigError, match="cannot use random simulated fills"):
        validate_config(config)


def test_paper_locked_arb_requires_positive_cross_platform_strategy_limit():
    config = BotConfig()
    config.mode.paper_locked_arb_enabled = True
    config.trading.bundle_arb_enabled = False
    config.trading.mm_enabled = False
    config.risk.strategy_exposure_limits["cross_platform_arb"] = 0

    with pytest.raises(
        ConfigError,
        match="requires a positive cross_platform_arb strategy exposure limit",
    ):
        validate_config(config)


def test_paper_locked_arb_requires_explicit_cross_platform_strategy_limit():
    config = BotConfig()
    config.mode.paper_locked_arb_enabled = True
    config.trading.bundle_arb_enabled = False
    config.trading.mm_enabled = False
    config.risk.strategy_exposure_limits = {}

    with pytest.raises(
        ConfigError,
        match="requires a positive cross_platform_arb strategy exposure limit",
    ):
        validate_config(config)
