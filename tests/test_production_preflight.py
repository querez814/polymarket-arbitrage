import os
import json
from pathlib import Path

from core.production_preflight import PreflightPhase, evaluate_production_preflight
from utils.config_loader import BotConfig
from scripts.production_preflight import main


def _candidate(tmp_path: Path) -> BotConfig:
    key = tmp_path / "kalshi.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    config = BotConfig()
    config.mode.trading_mode = "live"
    config.mode.data_mode = "real"
    config.mode.cross_platform_enabled = True
    config.mode.cross_platform_execution_enabled = True
    config.mode.kalshi_enabled = True
    config.mode.simulate_fills = False
    config.trading.bundle_arb_enabled = False
    config.trading.mm_enabled = False
    config.api.kalshi_private_key_path = str(key)
    config.risk.whitelist = ["condition-1", "TICKER-1"]
    config.trading.cross_platform_max_order_size = 1.0
    config.risk.max_order_notional = 10.0
    config.risk.max_position_per_market = 1.0
    config.risk.max_global_exposure = 2.0
    config.risk.strategy_exposure_limits["cross_platform_arb"] = 2.0
    config.risk.max_order_attempts_per_minute = 2
    config.risk.max_daily_order_attempts = 2
    config.production.execution_journal_path = "state/executions.sqlite3"
    config.production.operator_state_path = "state/operator.sqlite3"
    config.production.operator_token = "o" * 32
    config.production.alert_webhook_url = "https://alerts.example.test/nightwatch"
    config.production.alert_webhook_token = "a" * 32
    (tmp_path / "state").mkdir(mode=0o700)
    return config


def test_canary_preflight_accepts_one_explicit_pair_and_private_state(tmp_path):
    report = evaluate_production_preflight(
        _candidate(tmp_path),
        phase=PreflightPhase.CANARY,
        working_directory=tmp_path,
        canary_max_contracts=1.0,
        canary_max_order_notional=10.0,
    )

    assert report.ok is True
    assert report.failed == ()
    assert {check.name for check in report.checks} >= {
        "live_real_mode",
        "single_pair_whitelist",
        "state_paths",
        "kalshi_key_permissions",
        "separate_control_secrets",
    }


def test_preflight_fails_closed_on_broad_pair_scope_and_insecure_key(tmp_path):
    config = _candidate(tmp_path)
    config.risk.whitelist.append("another-market")
    os.chmod(config.api.kalshi_private_key_path, 0o644)

    report = evaluate_production_preflight(
        config,
        phase=PreflightPhase.CANARY,
        working_directory=tmp_path,
        canary_max_contracts=1.0,
        canary_max_order_notional=10.0,
    )

    assert report.ok is False
    assert "single_pair_whitelist" in report.failed
    assert "kalshi_key_permissions" in report.failed


def test_canary_preflight_rejects_limits_above_separately_approved_caps(tmp_path):
    config = _candidate(tmp_path)
    config.trading.cross_platform_max_order_size = 1_000_000
    config.risk.max_order_notional = 1_000_000
    config.risk.max_position_per_market = 1_000_000
    config.risk.max_global_exposure = 2_000_000
    config.risk.strategy_exposure_limits["cross_platform_arb"] = 1_000_000

    report = evaluate_production_preflight(
        config,
        phase=PreflightPhase.CANARY,
        working_directory=tmp_path,
        canary_max_contracts=1.0,
        canary_max_order_notional=10.0,
    )

    assert report.ok is False
    assert "canary_contract_limit" in report.failed
    assert "canary_notional_limits" in report.failed
    assert "canary_contract_exposure_limits" in report.failed


def test_preflight_rejects_intermediate_state_symlink(tmp_path):
    config = _candidate(tmp_path)
    config.mode.cross_platform_execution_enabled = False
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    nested = tmp_path / "state" / "nested"
    nested.symlink_to(outside, target_is_directory=True)
    config.production.execution_journal_path = "state/nested/executions.sqlite3"

    report = evaluate_production_preflight(
        config,
        phase=PreflightPhase.DEPLOYMENT,
        working_directory=tmp_path,
        state_directory=tmp_path / "state",
    )

    assert report.ok is False
    assert "state_paths" in report.failed


def test_deployment_preflight_allows_execution_disabled_but_rejects_symlinked_state(
    tmp_path,
):
    config = _candidate(tmp_path)
    config.mode.cross_platform_execution_enabled = False
    target = tmp_path / "target.sqlite3"
    target.touch()
    (tmp_path / "state" / "operator.sqlite3").symlink_to(target)

    report = evaluate_production_preflight(
        config, phase=PreflightPhase.DEPLOYMENT, working_directory=tmp_path
    )

    assert report.ok is False
    assert "state_paths" in report.failed
    assert "canary_execution_enabled" not in report.failed


def test_deployment_preflight_rejects_enabled_execution(tmp_path):
    report = evaluate_production_preflight(
        _candidate(tmp_path),
        phase=PreflightPhase.DEPLOYMENT,
        working_directory=tmp_path,
    )

    assert report.ok is False
    assert "deployment_execution_disabled" in report.failed


def test_preflight_rejects_state_outside_private_approved_directory(tmp_path):
    config = _candidate(tmp_path)
    config.mode.cross_platform_execution_enabled = False
    outside = tmp_path / "other"
    outside.mkdir(mode=0o700)
    config.production.execution_journal_path = str(outside / "executions.sqlite3")

    report = evaluate_production_preflight(
        config,
        phase=PreflightPhase.DEPLOYMENT,
        working_directory=tmp_path,
        state_directory=tmp_path / "state",
    )

    assert report.ok is False
    assert "state_paths" in report.failed


def test_preflight_rejects_group_readable_state_directory(tmp_path):
    config = _candidate(tmp_path)
    config.mode.cross_platform_execution_enabled = False
    (tmp_path / "state").chmod(0o750)

    report = evaluate_production_preflight(
        config,
        phase=PreflightPhase.DEPLOYMENT,
        working_directory=tmp_path,
        state_directory=tmp_path / "state",
    )

    assert report.ok is False
    assert "state_paths" in report.failed


def test_preflight_cli_reports_named_checks_without_printing_secrets(
    tmp_path, monkeypatch, capsys
):
    key = tmp_path / "kalshi.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    (tmp_path / "state").mkdir(mode=0o700)
    config_path = tmp_path / "config.live.yaml"
    config_path.write_text(
        """
api:
  polymarket_platform: global
  kalshi_api_key_id: kalshi-key
  kalshi_private_key_path: KALSHI_PATH
trading:
  bundle_arb_enabled: false
  mm_enabled: false
risk:
  max_global_exposure: 25
  strategy_exposure_limits:
    cross_platform_arb: 10
mode:
  trading_mode: live
  data_mode: real
  cross_platform_enabled: true
  cross_platform_execution_enabled: false
  kalshi_enabled: true
  simulate_fills: false
production:
  execution_journal_path: state/executions.sqlite3
  operator_state_path: state/operator.sqlite3
""".replace("KALSHI_PATH", str(key)),
        encoding="utf-8",
    )
    secrets = {
        "POLYMARKET_API_KEY": "poly-key",
        "POLYMARKET_API_SECRET": "poly-secret",
        "POLYMARKET_PASSPHRASE": "poly-passphrase",
        "POLYMARKET_PRIVATE_KEY": "poly-private-key",
        "NIGHTWATCH_OPERATOR_TOKEN": "o" * 32,
        "NIGHTWATCH_ALERT_WEBHOOK_URL": "https://alerts.example.test/nightwatch",
        "NIGHTWATCH_ALERT_WEBHOOK_TOKEN": "a" * 32,
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    result = main(
        [
            "--config",
            str(config_path),
            "--phase",
            "deployment",
            "--working-directory",
            str(tmp_path),
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert result == 0
    assert payload["ok"] is True
    assert payload["failed"] == []
    assert all(value not in output for value in secrets.values())


def test_preflight_cli_does_not_echo_invalid_config_source(tmp_path, capsys):
    secret = "do-not-print-this-secret"
    config_path = tmp_path / "config.live.yaml"
    config_path.write_text(f"production: [{secret}\n", encoding="utf-8")

    result = main(["--config", str(config_path)])
    output = capsys.readouterr().out

    assert result == 2
    assert secret not in output
    assert json.loads(output)["error"] == "configuration_invalid"
