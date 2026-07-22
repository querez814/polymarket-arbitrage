"""Side-effect-free deployment and canary readiness evaluation."""

from __future__ import annotations

import os
import stat
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from utils.config_loader import BotConfig


class PreflightPhase(str, Enum):
    DEPLOYMENT = "deployment"
    CANARY = "canary"


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    phase: PreflightPhase
    checks: tuple[PreflightCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


def evaluate_production_preflight(
    config: BotConfig,
    *,
    phase: PreflightPhase,
    working_directory: Path,
    state_directory: Path | None = None,
    canary_max_contracts: float | None = None,
    canary_max_order_notional: float | None = None,
) -> PreflightReport:
    """Evaluate local production invariants without network or venue mutation."""
    if not isinstance(phase, PreflightPhase):
        raise TypeError("phase must be a PreflightPhase")
    root = Path(working_directory)
    if not root.is_dir():
        raise ValueError("working_directory must be an existing directory")

    approved_state_root = _absolute(root, str(state_directory or root / "state"))
    checks: list[PreflightCheck] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(PreflightCheck(name, bool(passed), detail))

    add(
        "live_real_mode",
        config.is_live and not config.use_simulation and not config.mode.simulate_fills,
        "live mode uses real observations and venue-reported fills",
    )
    add(
        "locked_strategy_only",
        not config.trading.bundle_arb_enabled and not config.trading.mm_enabled,
        "legacy mutation strategies remain disabled",
    )
    add(
        "global_cross_platform",
        (
            not config.is_polymarket_us
            and config.mode.cross_platform_enabled
            and config.mode.kalshi_enabled
        ),
        "locked execution uses Polymarket Global and Kalshi",
    )

    state_paths = (
        _absolute(root, config.production.execution_journal_path),
        _absolute(root, config.production.operator_state_path),
    )
    state_ok = state_paths[0] != state_paths[1] and all(
        _is_beneath(path, approved_state_root)
        and _state_destination_safe(path, approved_state_root)
        for path in state_paths
    )
    add(
        "state_paths",
        state_ok,
        "journal and operator state are distinct owner-private regular destinations",
    )

    key_path = Path(config.api.kalshi_private_key_path)
    key_ok = _owner_private_regular_file(key_path)
    add(
        "kalshi_key_permissions",
        key_ok,
        "Kalshi signing key is a regular owner-private file",
    )

    operator_token = config.production.operator_token
    alert_token = config.production.alert_webhook_token
    add(
        "separate_control_secrets",
        (
            len(operator_token) >= 32
            and len(alert_token) >= 16
            and operator_token != alert_token
        ),
        "operator and alert credentials are strong and independent",
    )
    add(
        "authenticated_alert_transport",
        config.production.alert_webhook_url.startswith("https://")
        and len(alert_token) >= 16,
        "critical alerts use authenticated HTTPS delivery",
    )

    exposure_limit = config.risk.strategy_exposure_limits.get("cross_platform_arb", 0)
    add(
        "bounded_locked_exposure",
        0 < exposure_limit <= config.risk.max_global_exposure,
        "cross-platform exposure is positive and bounded by the global cap",
    )

    if phase is PreflightPhase.DEPLOYMENT:
        add(
            "deployment_execution_disabled",
            not config.mode.cross_platform_execution_enabled,
            "ordinary deployment must start with venue execution disabled",
        )
    else:
        add(
            "canary_execution_enabled",
            config.mode.cross_platform_execution_enabled,
            "the canary config explicitly enables the owned execution path",
        )
        add(
            "single_pair_whitelist",
            len(set(config.risk.whitelist)) == 2 and len(config.risk.whitelist) == 2,
            "exactly one Polymarket condition id and one Kalshi ticker are approved",
        )
        add(
            "single_plan_attempt_budget",
            config.risk.max_order_attempts_per_minute == 2
            and config.risk.max_daily_order_attempts == 2,
            "the canary admits at most one possible two-leg plan",
        )
        size_limits = (
            config.trading.cross_platform_max_order_size,
            canary_max_contracts,
        )
        add(
            "canary_contract_limit",
            _positive_bounded(*size_limits),
            "cross-platform quantity is bounded by the separately approved canary cap",
        )
        add(
            "canary_notional_limits",
            canary_max_order_notional is not None
            and canary_max_order_notional > 0
            and math.isclose(config.risk.max_order_notional, canary_max_order_notional),
            "the dollar order limit exactly matches the approved canary notional",
        )
        add(
            "canary_contract_exposure_limits",
            canary_max_contracts is not None
            and canary_max_contracts > 0
            and math.isclose(config.risk.max_position_per_market, canary_max_contracts)
            and math.isclose(config.risk.max_global_exposure, 2 * canary_max_contracts)
            and math.isclose(exposure_limit, 2 * canary_max_contracts),
            "contract exposure limits exactly match one approved two-leg lifecycle",
        )

    return PreflightReport(phase=phase, checks=tuple(checks))


def _absolute(root: Path, configured: str) -> Path:
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    return Path(os.path.abspath(path))


def _state_destination_safe(path: Path, approved_root: Path) -> bool:
    parent = path.parent
    if not _owner_private_directory(approved_root):
        return False
    if (
        not _owner_private_directory(parent)
        or not _is_beneath(parent, approved_root)
        or not os.access(parent, os.W_OK | os.X_OK)
    ):
        return False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return True
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
        and metadata.st_uid == os.geteuid()
        and os.access(path, os.R_OK | os.W_OK)
    )


def _owner_private_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
        and metadata.st_uid == os.geteuid()
        and os.access(path, os.R_OK)
    )


def owner_private_regular_file(
    path: Path, *, expected_uid: int | None = None, require_read: bool = True
) -> bool:
    """Validate a regular file's owner and private mode without reading contents."""
    try:
        metadata = Path(path).lstat()
    except (FileNotFoundError, OSError):
        return False
    owner = os.geteuid() if expected_uid is None else expected_uid
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
        and metadata.st_uid == owner
        and (not require_read or os.access(path, os.R_OK))
    )


def _owner_private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
        and metadata.st_uid == os.geteuid()
        and os.access(path, os.R_OK | os.W_OK | os.X_OK)
    )


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (ValueError, FileNotFoundError, OSError):
        return False
    current = root
    try:
        for part in path.relative_to(root).parts[:-1]:
            current = current / part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _positive_bounded(value: float, maximum: float | None) -> bool:
    return maximum is not None and maximum > 0 and 0 < value <= maximum


def protected_regular_file(path: Path, *, expected_uid: int) -> bool:
    """Require a readable non-symlink file that only its owner may modify."""
    try:
        metadata = Path(path).lstat()
    except (FileNotFoundError, OSError):
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == expected_uid
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
        and os.access(path, os.R_OK)
    )
