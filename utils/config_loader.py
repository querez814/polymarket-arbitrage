"""
Configuration Loader
=====================

Loads and validates configuration from YAML files.
"""

import math
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


POLYMARKET_GLOBAL_PRODUCTION_URLS = {
    "api.polymarket_rest_url": "https://clob.polymarket.com",
    "api.polymarket_ws_url": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    "api.gamma_api_url": "https://gamma-api.polymarket.com",
}
POLYMARKET_US_PRODUCTION_URLS = {
    "api.polymarket_us_api_url": "https://api.polymarket.us",
    "api.polymarket_us_gateway_url": "https://gateway.polymarket.us",
}
KALSHI_PRODUCTION_URL = "https://external-api.kalshi.com/trade-api/v2"


class ConfigError(Exception):
    """Configuration error."""
    pass


@dataclass
class ApiConfig:
    """API configuration."""
    polymarket_rest_url: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    kalshi_api_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    private_key: str = ""
    polymarket_private_key_keychain_label: str = ""
    chain_id: int = 137
    polymarket_platform: str = "global"  # "global" or "us"
    polymarket_us_api_url: str = "https://api.polymarket.us"
    polymarket_us_gateway_url: str = "https://gateway.polymarket.us"
    polymarket_us_key_id: str = ""
    polymarket_us_secret_key: str = ""
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_delay_seconds: float = 1.0


@dataclass
class TradingConfig:
    """Trading configuration."""
    risk_profile: str = "conservative"
    markets: list[str] = field(default_factory=list)
    min_edge: float = 0.01
    bundle_arb_enabled: bool = True
    bundle_cooldown_seconds: float = 2.0
    min_spread: float = 0.05
    tick_size: float = 0.01
    mm_enabled: bool = True
    mm_cooldown_seconds: float = 5.0
    mm_one_sided_enabled: bool = False
    default_order_size: float = 50.0
    min_order_size: float = 5.0
    max_order_size: float = 200.0
    edge_size_multiplier: float = 4.0
    max_liquidity_fraction: float = 1.0
    slippage_tolerance: float = 0.02
    arb_slippage_tolerance: float = 0.02
    market_making_slippage_tolerance: float = 0.01
    high_edge_slippage_multiplier: float = 1.5
    order_timeout_seconds: float = 60.0
    arb_order_timeout_seconds: float = 15.0
    market_making_order_timeout_seconds: float = 20.0
    cross_platform_max_order_size: float = 100.0
    cross_platform_edge_size_multiplier: float = 4.0
    cross_platform_max_liquidity_fraction: float = 1.0


@dataclass
class RiskConfig:
    """Risk configuration."""
    max_order_notional: float = 15.0
    max_open_orders: int = 4
    max_open_positions: int = 4
    max_order_attempts_per_minute: int = 10
    max_daily_order_attempts: int = 100
    max_position_per_market: float = 200.0
    max_global_exposure: float = 5000.0
    max_daily_loss: float = 500.0
    max_drawdown_pct: float = 0.10
    trade_only_high_volume: bool = True
    min_24h_volume: float = 10000.0
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    strategy_exposure_limits: dict[str, float] = field(default_factory=dict)
    kill_switch_enabled: bool = True
    auto_unwind_on_breach: bool = False


@dataclass
class ModeConfig:
    """Trading mode configuration."""
    trading_mode: str = "dry_run"  # "live" or "dry_run"
    data_mode: str = "real"  # "real" or "simulation" - use simulation for demos
    cross_platform_enabled: bool = True  # Enable cross-platform arbitrage (Polymarket + Kalshi)
    kalshi_enabled: bool = True  # Enable Kalshi market monitoring
    min_match_similarity: float = 0.6  # Minimum similarity score for market matching (0-1)
    dry_run_initial_balance: float = 10000.0
    simulate_fills: bool = False
    fill_probability: float = 0.0


@dataclass
class LoggingConfig:
    """Logging configuration."""
    console_level: str = "INFO"
    file_level: str = "DEBUG"
    log_dir: str = "logs"
    main_log_file: str = "bot.log"
    trades_log_file: str = "trades.log"
    opportunities_log_file: str = "opportunities.log"
    max_log_size_mb: int = 50
    backup_count: int = 5


@dataclass
class MonitoringConfig:
    """Monitoring configuration."""
    snapshot_interval: float = 60.0
    heartbeat_interval: float = 30.0
    track_latency: bool = True
    track_fill_rates: bool = True
    paper_trade_db_path: str = "data/paper_trades.db"
    display_timezone: str = "America/New_York"


@dataclass
class BotConfig:
    """Complete bot configuration."""
    api: ApiConfig = field(default_factory=ApiConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    
    @property
    def is_polymarket_us(self) -> bool:
        return self.api.polymarket_platform.lower() == "us"

    @property
    def is_dry_run(self) -> bool:
        return self.mode.trading_mode.lower() == "dry_run"
    
    @property
    def is_live(self) -> bool:
        return self.mode.trading_mode.lower() == "live"
    
    @property
    def use_simulation(self) -> bool:
        """Use simulated data (for demos/screenshots)."""
        return self.mode.data_mode.lower() == "simulation"


def load_config(config_path: str = "config.yaml") -> BotConfig:
    """
    Load configuration from a YAML file.
    
    Args:
        config_path: Path to the configuration file
        
    Returns:
        BotConfig instance with loaded values
        
    Raises:
        ConfigError: If the config file cannot be loaded or is invalid
    """
    path = Path(config_path)
    
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")
    
    try:
        with open(path, "r") as f:
            raw_config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in config file: {e}")
    
    if raw_config is None:
        raw_config = {}
    
    # Parse sections
    api_data = raw_config.get("api", {})
    trading_data = raw_config.get("trading", {})
    risk_data = raw_config.get("risk", {})
    mode_data = raw_config.get("mode", {})
    logging_data = raw_config.get("logging", {})
    monitoring_data = raw_config.get("monitoring", {})
    
    # Handle environment variable overrides
    api_data = _apply_env_overrides(api_data, {
        "api_key": "POLYMARKET_API_KEY",
        "api_secret": "POLYMARKET_API_SECRET",
        "passphrase": "POLYMARKET_PASSPHRASE",
        "private_key": "POLYMARKET_PRIVATE_KEY",
        "polymarket_private_key_keychain_label": "POLYMARKET_PRIVATE_KEY_KEYCHAIN_LABEL",
        "chain_id": "POLYMARKET_CHAIN_ID",
        "polymarket_platform": "POLYMARKET_PLATFORM",
        "polymarket_us_key_id": "POLYMARKET_US_KEY_ID",
        "polymarket_us_secret_key": "POLYMARKET_US_SECRET_KEY",
        "polymarket_us_api_url": "POLYMARKET_US_API_URL",
        "polymarket_us_gateway_url": "POLYMARKET_US_GATEWAY_URL",
        "kalshi_api_url": "KALSHI_API_URL",
        "kalshi_api_key_id": "KALSHI_API_KEY_ID",
        "kalshi_private_key_path": "KALSHI_PRIVATE_KEY_PATH",
    })
    
    _apply_risk_profile_defaults(trading_data, risk_data)

    # Build config objects
    config = BotConfig(
        api=_build_dataclass(ApiConfig, api_data),
        trading=_build_dataclass(TradingConfig, trading_data),
        risk=_build_dataclass(RiskConfig, risk_data),
        mode=_build_dataclass(ModeConfig, mode_data),
        logging=_build_dataclass(LoggingConfig, logging_data),
        monitoring=_build_dataclass(MonitoringConfig, monitoring_data),
    )
    
    # Validate
    resolve_runtime_secrets(config)
    validate_config(config)
    
    return config


def _apply_env_overrides(data: dict, env_map: dict[str, str]) -> dict:
    """Apply environment variable overrides to config data."""
    result = data.copy()
    for key, env_var in env_map.items():
        env_value = os.environ.get(env_var)
        if env_value:
            if key == "chain_id":
                result[key] = int(env_value)
            else:
                result[key] = env_value
    return result


def resolve_runtime_secrets(config: BotConfig) -> None:
    """Resolve explicitly configured runtime-only secrets for live startup."""
    if not config.is_live or config.is_polymarket_us:
        return

    private_key = config.api.private_key.strip()
    keychain_label = config.api.polymarket_private_key_keychain_label.strip()
    resolved_from_keychain = getattr(
        config.api, "_private_key_resolved_from_keychain", False
    )

    if private_key and keychain_label:
        if resolved_from_keychain:
            return
        raise ConfigError(
            "Configure exactly one Polymarket wallet-key source: "
            "POLYMARKET_PRIVATE_KEY or POLYMARKET_PRIVATE_KEY_KEYCHAIN_LABEL"
        )

    if not keychain_label:
        return

    security_path = Path("/usr/bin/security")
    if not security_path.exists():
        raise ConfigError(
            "Polymarket wallet Keychain loading requires macOS /usr/bin/security; "
            "use POLYMARKET_PRIVATE_KEY on this platform"
        )

    try:
        result = subprocess.run(
            [
                str(security_path),
                "find-generic-password",
                "-w",
                "-l",
                keychain_label,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ConfigError(
            f"Unable to read Polymarket wallet key from Keychain label "
            f"{keychain_label!r}; verify the item exists and access is approved"
        ) from exc

    resolved_key = result.stdout.strip()
    if not resolved_key:
        raise ConfigError(
            f"Polymarket wallet Keychain label {keychain_label!r} returned an empty value"
        )

    config.api.private_key = resolved_key
    # Keep this runtime-only marker out of dataclass serialization while making
    # repeated startup validation idempotent.
    setattr(config.api, "_private_key_resolved_from_keychain", True)


def _build_dataclass(cls, data: dict):
    """Build a dataclass from a dictionary, ignoring unknown keys."""
    import dataclasses
    
    field_names = {f.name for f in dataclasses.fields(cls)}
    filtered_data = {k: v for k, v in data.items() if k in field_names}
    return cls(**filtered_data)


def _apply_risk_profile_defaults(trading_data: dict, risk_data: dict) -> None:
    """Apply profile defaults only where the config did not set explicit values."""
    profile = str(trading_data.get("risk_profile", "conservative")).lower()
    profiles: dict[str, dict[str, dict[str, Any]]] = {
        "conservative": {
            "trading": {},
            "risk": {},
        },
        "balanced": {
            "trading": {
                "min_edge": 0.0075,
                "min_spread": 0.035,
                "bundle_cooldown_seconds": 1.0,
                "mm_cooldown_seconds": 3.0,
                "default_order_size": 10.0,
                "max_order_size": 20.0,
                "edge_size_multiplier": 5.0,
                "max_liquidity_fraction": 0.75,
            },
            "risk": {
                "max_order_notional": 20.0,
                "max_open_orders": 6,
                "max_open_positions": 6,
                "max_order_attempts_per_minute": 20,
                "max_daily_order_attempts": 250,
                "max_position_per_market": 25.0,
                "max_global_exposure": 75.0,
                "max_daily_loss": 15.0,
                "strategy_exposure_limits": {
                    "bundle_arb": 50.0,
                    "market_making": 25.0,
                    "cross_platform_arb": 50.0,
                },
            },
        },
        "aggressive": {
            "trading": {
                "min_edge": 0.005,
                "min_spread": 0.025,
                "mm_enabled": True,
                "mm_one_sided_enabled": True,
                "bundle_cooldown_seconds": 0.5,
                "mm_cooldown_seconds": 1.0,
                "default_order_size": 15.0,
                "max_order_size": 30.0,
                "edge_size_multiplier": 7.5,
                "max_liquidity_fraction": 0.85,
                "arb_slippage_tolerance": 0.03,
                "market_making_slippage_tolerance": 0.015,
                "arb_order_timeout_seconds": 8.0,
                "market_making_order_timeout_seconds": 12.0,
                "cross_platform_max_order_size": 30.0,
                "cross_platform_edge_size_multiplier": 7.5,
                "cross_platform_max_liquidity_fraction": 0.85,
            },
            "risk": {
                "max_order_notional": 30.0,
                "max_open_orders": 8,
                "max_open_positions": 8,
                "max_order_attempts_per_minute": 30,
                "max_daily_order_attempts": 500,
                "max_position_per_market": 35.0,
                "max_global_exposure": 100.0,
                "max_daily_loss": 20.0,
                "strategy_exposure_limits": {
                    "bundle_arb": 70.0,
                    "market_making": 35.0,
                    "cross_platform_arb": 70.0,
                },
            },
        },
    }

    if profile not in profiles:
        return

    for key, value in profiles[profile]["trading"].items():
        trading_data.setdefault(key, value)
    for key, value in profiles[profile]["risk"].items():
        risk_data.setdefault(key, value)


def validate_config(config: BotConfig) -> None:
    """Validate a configuration, including mutations applied after loading."""
    errors = []
    
    # Trading validation
    if config.trading.risk_profile.lower() not in ("conservative", "balanced", "aggressive"):
        errors.append("trading.risk_profile must be 'conservative', 'balanced', or 'aggressive'")

    if config.trading.min_edge < 0 or config.trading.min_edge > 1:
        errors.append("trading.min_edge must be between 0 and 1")
    
    if config.trading.min_spread < 0 or config.trading.min_spread > 1:
        errors.append("trading.min_spread must be between 0 and 1")
    
    if config.trading.tick_size <= 0:
        errors.append("trading.tick_size must be positive")
    
    if config.trading.default_order_size <= 0:
        errors.append("trading.default_order_size must be positive")

    if config.trading.min_order_size <= 0:
        errors.append("trading.min_order_size must be positive")

    if config.trading.max_order_size < config.trading.min_order_size:
        errors.append("trading.max_order_size must be >= trading.min_order_size")

    if config.trading.bundle_cooldown_seconds < 0:
        errors.append("trading.bundle_cooldown_seconds must be non-negative")

    if config.trading.mm_cooldown_seconds < 0:
        errors.append("trading.mm_cooldown_seconds must be non-negative")

    if config.trading.max_liquidity_fraction <= 0 or config.trading.max_liquidity_fraction > 1:
        errors.append("trading.max_liquidity_fraction must be between 0 and 1")

    if config.trading.cross_platform_max_liquidity_fraction <= 0 or config.trading.cross_platform_max_liquidity_fraction > 1:
        errors.append("trading.cross_platform_max_liquidity_fraction must be between 0 and 1")
    
    # Risk validation
    if not math.isfinite(config.risk.max_order_notional) or config.risk.max_order_notional <= 0:
        errors.append("risk.max_order_notional must be finite and positive")

    if config.risk.max_order_notional > config.risk.max_global_exposure:
        errors.append("risk.max_order_notional must be <= risk.max_global_exposure")

    if (
        not isinstance(config.risk.max_open_orders, int)
        or isinstance(config.risk.max_open_orders, bool)
        or config.risk.max_open_orders <= 0
    ):
        errors.append("risk.max_open_orders must be a positive integer")

    if (
        not isinstance(config.risk.max_open_positions, int)
        or isinstance(config.risk.max_open_positions, bool)
        or config.risk.max_open_positions <= 0
    ):
        errors.append("risk.max_open_positions must be a positive integer")

    for field_name in ("max_order_attempts_per_minute", "max_daily_order_attempts"):
        value = getattr(config.risk, field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"risk.{field_name} must be a positive integer")

    if config.risk.max_position_per_market <= 0:
        errors.append("risk.max_position_per_market must be positive")
    
    if config.risk.max_global_exposure <= 0:
        errors.append("risk.max_global_exposure must be positive")
    
    if config.risk.max_daily_loss < 0:
        errors.append("risk.max_daily_loss must be non-negative")
    
    if config.risk.max_drawdown_pct < 0 or config.risk.max_drawdown_pct > 1:
        errors.append("risk.max_drawdown_pct must be between 0 and 1")

    for strategy, limit in config.risk.strategy_exposure_limits.items():
        if limit < 0:
            errors.append(f"risk.strategy_exposure_limits.{strategy} must be non-negative")
    
    # Mode validation
    if config.mode.trading_mode.lower() not in ("live", "dry_run"):
        errors.append("mode.trading_mode must be 'live' or 'dry_run'")

    if config.mode.data_mode.lower() not in ("real", "simulation"):
        errors.append("mode.data_mode must be 'real' or 'simulation'")

    if config.mode.cross_platform_enabled and not config.mode.kalshi_enabled:
        errors.append(
            "mode.kalshi_enabled must be true when mode.cross_platform_enabled is true"
        )

    kalshi_key_id = config.api.kalshi_api_key_id.strip()
    kalshi_private_key_path = config.api.kalshi_private_key_path.strip()
    if bool(kalshi_key_id) != bool(kalshi_private_key_path):
        errors.append(
            "api.kalshi_api_key_id and api.kalshi_private_key_path must be configured together"
        )
    elif kalshi_private_key_path and not Path(kalshi_private_key_path).expanduser().is_file():
        errors.append(
            "api.kalshi_private_key_path must reference an existing regular file"
        )
    
    # Live mode checks
    if config.is_live:
        if config.use_simulation:
            errors.append("mode.data_mode must be 'real' in live mode")
        if config.mode.simulate_fills:
            errors.append("mode.simulate_fills must be false in live mode")
        if config.is_polymarket_us:
            _validate_production_urls(config, POLYMARKET_US_PRODUCTION_URLS, errors)
            if not config.api.polymarket_us_key_id:
                errors.append("api.polymarket_us_key_id is required for live Polymarket US trading")
            if not config.api.polymarket_us_secret_key:
                errors.append("api.polymarket_us_secret_key is required for live Polymarket US trading")
        else:
            _validate_production_urls(config, POLYMARKET_GLOBAL_PRODUCTION_URLS, errors)
            if config.api.chain_id != 137:
                errors.append(
                    "api.chain_id must be 137 for live Polymarket Global trading"
                )
            if not config.api.api_key or config.api.api_key == "YOUR_API_KEY_HERE":
                errors.append("api.api_key is required for live Polymarket Global trading")
            if not config.api.api_secret or config.api.api_secret == "YOUR_API_SECRET_HERE":
                errors.append("api.api_secret is required for live Polymarket Global trading")
            if not config.api.passphrase or config.api.passphrase == "YOUR_PASSPHRASE_HERE":
                errors.append("api.passphrase is required for live Polymarket Global trading")
            if not config.api.private_key or config.api.private_key == "YOUR_PRIVATE_KEY_HERE":
                errors.append("api.private_key is required for live Polymarket Global trading")

        if config.mode.kalshi_enabled:
            actual_kalshi_url = config.api.kalshi_api_url.rstrip("/")
            if actual_kalshi_url != KALSHI_PRODUCTION_URL:
                errors.append(
                    "api.kalshi_api_url must be "
                    f"{KALSHI_PRODUCTION_URL!r} when Kalshi is enabled in live mode"
                )

    if config.api.polymarket_platform.lower() not in ("global", "us"):
        errors.append("api.polymarket_platform must be 'global' or 'us'")
    
    if errors:
        raise ConfigError("Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


def _validate_production_urls(
    config: BotConfig, expected_urls: dict[str, str], errors: list[str]
) -> None:
    """Reject accidental test, proxy, or cross-venue endpoints in live mode."""
    for field_name, expected_url in expected_urls.items():
        attribute = field_name.removeprefix("api.")
        actual_url = str(getattr(config.api, attribute)).rstrip("/")
        if actual_url != expected_url:
            errors.append(
                f"{field_name} must be {expected_url!r} in live mode"
            )


def save_config(config: BotConfig, config_path: str = "config.yaml") -> None:
    """Save configuration to a YAML file."""
    import dataclasses
    
    def to_dict(obj):
        if dataclasses.is_dataclass(obj):
            return {k: to_dict(v) for k, v in dataclasses.asdict(obj).items()}
        return obj
    
    data = to_dict(config)
    
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def get_default_config() -> BotConfig:
    """Get a default configuration."""
    return BotConfig()
