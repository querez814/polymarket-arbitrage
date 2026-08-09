import asyncio
import logging
import sys
from pathlib import Path

import pytest

from utils.logging_utils import setup_logging


@pytest.fixture(autouse=True)
def reset_logging_handlers():
    """Keep process-global logging configuration out of other tests."""
    yield
    for name in (None, "trades", "opportunities"):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


@pytest.mark.parametrize("module_name", ["main", "run_with_dashboard"])
def test_entrypoint_configures_profile_log_paths_before_runtime(
    tmp_path, monkeypatch, module_name
):
    module = __import__(module_name)
    configured_dir = tmp_path / "isolated-logs"
    config_path = tmp_path / "profile.yaml"
    config_path.write_text(
        "\n".join(
            [
                "mode:",
                "  trading_mode: dry_run",
                "logging:",
                f"  log_dir: {configured_dir}",
                "  main_log_file: selected-main.log",
                "  trades_log_file: selected-trades.log",
                "  opportunities_log_file: selected-opportunities.log",
            ]
        ),
        encoding="utf-8",
    )
    captured = {}

    def capture_logging(**kwargs):
        captured.update(kwargs)

    def stop_before_runtime(coroutine):
        coroutine.close()
        return True

    monkeypatch.setattr(module, "setup_logging", capture_logging)
    monkeypatch.setattr(module.asyncio, "run", stop_before_runtime)
    monkeypatch.setattr(
        sys, "argv", [module_name, "--config", str(config_path)]
    )

    module.main()

    assert captured == {
        "log_dir": str(configured_dir),
        "console_level": "INFO",
        "main_log_file": "selected-main.log",
        "trades_log_file": "selected-trades.log",
        "opportunities_log_file": "selected-opportunities.log",
    }


def test_setup_logging_writes_only_to_current_configured_directory(tmp_path):
    old_dir = tmp_path / "old"
    configured_dir = tmp_path / "configured"

    setup_logging(
        log_dir=str(old_dir),
        main_log_file="old-main.log",
        trades_log_file="old-trades.log",
        opportunities_log_file="old-opportunities.log",
    )
    setup_logging(
        log_dir=str(configured_dir),
        main_log_file="selected-main.log",
        trades_log_file="selected-trades.log",
        opportunities_log_file="selected-opportunities.log",
    )

    logging.getLogger("trades").info("configured trade")
    logging.getLogger("opportunities").info("configured opportunity")

    assert (configured_dir / "selected-main.log").exists()
    assert (configured_dir / "selected-trades.log").read_text(encoding="utf-8")
    assert (configured_dir / "selected-opportunities.log").read_text(encoding="utf-8")
    assert not (old_dir / "old-trades.log").read_text(encoding="utf-8")
    assert not (old_dir / "old-opportunities.log").read_text(encoding="utf-8")
