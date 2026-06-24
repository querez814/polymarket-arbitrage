"""
Configuration Loader
=====================

Loads and validates configuration from YAML files.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional at runtime
    load_dotenv = None


class ConfigError(Exception):
    """Configuration error."""
    pass


VALID_TRADING_MODES = ("scanner", "paper", "live")
LEGACY_TRADING_MODE_ALIASES = {
    "dry_run": "paper",
}
VALID_DATA_MODES = ("real", "simulation")
VALID_POLYMARKET_VENUES = ("unconfirmed", "global", "us", "none")
VALID_KALSHI_ENVIRONMENTS = ("unconfirmed", "demo", "production", "none")
VALID_PAPER_RISK_PROFILES = ("conservative", "aggressive")
LIVE_CONFIRMATION_ENV = "POLYMARKET_ARB_LIVE_CONFIRMATION"
LIVE_CONFIRMATION_VALUE = "I_UNDERSTAND_LIVE_RISK"
PLACEHOLDER_SECRET_VALUES = {
    "YOUR_API_KEY_HERE",
    "YOUR_API_SECRET_HERE",
    "YOUR_PASSPHRASE_HERE",
    "YOUR_PRIVATE_KEY_HERE",
    "YOUR_WALLET_PRIVATE_KEY_HERE",
}


@dataclass
class ApiConfig:
    """API configuration."""
    polymarket_rest_url: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    kalshi_api_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    private_key: str = ""
    kalshi_api_key: str = ""
    kalshi_private_key_path: str = ""
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_delay_seconds: float = 1.0


@dataclass
class TradingConfig:
    """Trading configuration."""
    markets: list[str] = field(default_factory=list)
    min_edge: float = 0.01
    bundle_arb_enabled: bool = True
    bundle_short_enabled: bool = False
    min_spread: float = 0.05
    tick_size: float = 0.01
    mm_enabled: bool = False
    default_order_size: float = 50.0
    min_order_size: float = 5.0
    max_order_size: float = 200.0
    slippage_tolerance: float = 0.02
    order_timeout_seconds: float = 60.0


@dataclass
class RiskConfig:
    """Risk configuration."""
    max_position_per_market: float = 200.0
    max_global_exposure: float = 5000.0
    max_daily_loss: float = 500.0
    max_drawdown_pct: float = 0.10
    trade_only_high_volume: bool = True
    min_24h_volume: float = 10000.0
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    kill_switch_enabled: bool = True
    auto_unwind_on_breach: bool = False


@dataclass
class ModeConfig:
    """Trading mode configuration."""
    trading_mode: str = "scanner"  # "scanner", "paper", or "live"
    data_mode: str = "real"  # "real" or "simulation" - use simulation for demos
    cross_platform_enabled: bool = True  # Enable cross-platform arbitrage (Polymarket + Kalshi)
    kalshi_enabled: bool = True  # Enable Kalshi market monitoring
    min_match_similarity: float = 0.6  # Minimum similarity score for market matching (0-1)
    live_trading_enabled: bool = False
    manual_live_approval: bool = False
    dry_run_initial_balance: float = 10000.0
    simulate_fills: bool = True
    fill_probability: float = 0.8


@dataclass
class PaperRiskConfig:
    """Paper-only risk and strategy controls."""
    profile: str = "conservative"
    paper_enable_bundle_short: bool = False
    paper_enable_market_making: bool = False
    paper_enable_cross_platform_synthetic: bool = False
    aggressive_min_edge: float = 0.0025
    aggressive_default_order_size: float = 100.0
    aggressive_max_order_size: float = 500.0
    aggressive_fill_probability: float = 0.95
    quote_lifetime_seconds: float = 15.0
    one_sided_exposure_cap: float = 250.0


@dataclass
class VenueAccessConfig:
    """Venue access and compliance confirmations."""
    polymarket_venue: str = "unconfirmed"  # "global", "us", "none", or "unconfirmed"
    kalshi_environment: str = "unconfirmed"  # "demo", "production", "none", or "unconfirmed"
    no_geoblock_workarounds: bool = False


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


@dataclass
class BotConfig:
    """Complete bot configuration."""
    api: ApiConfig = field(default_factory=ApiConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    paper_risk: PaperRiskConfig = field(default_factory=PaperRiskConfig)
    venue_access: VenueAccessConfig = field(default_factory=VenueAccessConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    
    @property
    def trading_mode(self) -> str:
        return _normalize_trading_mode(self.mode.trading_mode)

    @property
    def is_scanner(self) -> bool:
        return self.trading_mode == "scanner"

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"

    @property
    def is_dry_run(self) -> bool:
        """Backward-compatible alias: any non-live mode avoids real orders."""
        return not self.is_live
    
    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def allows_execution(self) -> bool:
        """Whether signals may be submitted to an execution engine."""
        return self.is_paper or self.is_live

    @property
    def simulates_orders(self) -> bool:
        """Whether orders should be kept in the paper ledger."""
        return self.is_paper
    
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

    _load_local_env(path)
    
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
    paper_risk_data = raw_config.get("paper_risk", {})
    venue_access_data = raw_config.get("venue_access", {})
    logging_data = raw_config.get("logging", {})
    monitoring_data = raw_config.get("monitoring", {})
    
    # Handle environment variable overrides
    api_data = _sanitize_secret_placeholders(api_data)
    api_data = _apply_env_overrides(api_data, {
        "api_key": "POLYMARKET_API_KEY",
        "api_secret": "POLYMARKET_API_SECRET",
        "passphrase": "POLYMARKET_PASSPHRASE",
        "private_key": "POLYMARKET_PRIVATE_KEY",
        "kalshi_api_key": "KALSHI_API_KEY",
        "kalshi_private_key_path": "KALSHI_PRIVATE_KEY_PATH",
    })
    
    # Build config objects
    config = BotConfig(
        api=_build_dataclass(ApiConfig, api_data),
        trading=_build_dataclass(TradingConfig, trading_data),
        risk=_build_dataclass(RiskConfig, risk_data),
        mode=_build_dataclass(ModeConfig, mode_data),
        paper_risk=_build_dataclass(PaperRiskConfig, paper_risk_data),
        venue_access=_build_dataclass(VenueAccessConfig, venue_access_data),
        logging=_build_dataclass(LoggingConfig, logging_data),
        monitoring=_build_dataclass(MonitoringConfig, monitoring_data),
    )
    config.mode.trading_mode = config.trading_mode
    
    # Validate
    validate_config(config)
    
    return config


def _load_local_env(config_path: Path) -> None:
    """Load local environment files without overriding existing env vars."""
    if load_dotenv is None:
        return

    candidates = [
        config_path.parent / ".env.local",
        config_path.parent / ".env",
        Path.cwd() / ".env.local",
        Path.cwd() / ".env",
    ]

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            load_dotenv(resolved, override=False)


def _normalize_trading_mode(value: str) -> str:
    """Normalize trading mode names and legacy aliases."""
    normalized = str(value or "").strip().lower()
    return LEGACY_TRADING_MODE_ALIASES.get(normalized, normalized)


def _is_placeholder_secret(value: Any) -> bool:
    """Return True for blank or template credential values."""
    if value is None:
        return True
    text = str(value).strip()
    return not text or text in PLACEHOLDER_SECRET_VALUES or text.startswith("YOUR_")


def _sanitize_secret_placeholders(data: dict) -> dict:
    """Drop template credential values so they never act like credentials."""
    result = data.copy()
    for key in (
        "api_key",
        "api_secret",
        "passphrase",
        "private_key",
        "kalshi_api_key",
        "kalshi_private_key_path",
    ):
        if key in result and _is_placeholder_secret(result[key]):
            result[key] = ""
    return result


def _apply_env_overrides(data: dict, env_map: dict[str, str]) -> dict:
    """Apply environment variable overrides to config data."""
    result = data.copy()
    for key, env_var in env_map.items():
        env_value = os.environ.get(env_var)
        if env_value:
            result[key] = env_value
    return result


def _build_dataclass(cls, data: dict):
    """Build a dataclass from a dictionary, ignoring unknown keys."""
    import dataclasses
    
    field_names = {f.name for f in dataclasses.fields(cls)}
    filtered_data = {k: v for k, v in data.items() if k in field_names}
    return cls(**filtered_data)


def validate_config(config: BotConfig) -> None:
    """Validate configuration values."""
    config.mode.trading_mode = config.trading_mode
    errors = []
    
    # Trading validation
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
    
    # Risk validation
    if config.risk.max_position_per_market <= 0:
        errors.append("risk.max_position_per_market must be positive")
    
    if config.risk.max_global_exposure <= 0:
        errors.append("risk.max_global_exposure must be positive")
    
    if config.risk.max_daily_loss < 0:
        errors.append("risk.max_daily_loss must be non-negative")
    
    if config.risk.max_drawdown_pct < 0 or config.risk.max_drawdown_pct > 1:
        errors.append("risk.max_drawdown_pct must be between 0 and 1")
    
    # Mode validation
    if config.trading_mode not in VALID_TRADING_MODES:
        errors.append("mode.trading_mode must be one of: scanner, paper, live")

    if config.mode.data_mode.lower() not in VALID_DATA_MODES:
        errors.append("mode.data_mode must be 'real' or 'simulation'")

    if not 0 <= config.mode.min_match_similarity <= 1:
        errors.append("mode.min_match_similarity must be between 0 and 1")

    if not 0 <= config.mode.fill_probability <= 1:
        errors.append("mode.fill_probability must be between 0 and 1")

    # Paper-risk validation
    config.paper_risk.profile = str(config.paper_risk.profile).lower()
    if config.paper_risk.profile not in VALID_PAPER_RISK_PROFILES:
        errors.append("paper_risk.profile must be 'conservative' or 'aggressive'")

    if not 0 <= config.paper_risk.aggressive_min_edge <= 1:
        errors.append("paper_risk.aggressive_min_edge must be between 0 and 1")

    if config.paper_risk.aggressive_default_order_size <= 0:
        errors.append("paper_risk.aggressive_default_order_size must be positive")

    if config.paper_risk.aggressive_max_order_size <= 0:
        errors.append("paper_risk.aggressive_max_order_size must be positive")

    if not 0 <= config.paper_risk.aggressive_fill_probability <= 1:
        errors.append("paper_risk.aggressive_fill_probability must be between 0 and 1")

    if config.paper_risk.quote_lifetime_seconds <= 0:
        errors.append("paper_risk.quote_lifetime_seconds must be positive")

    if config.paper_risk.one_sided_exposure_cap <= 0:
        errors.append("paper_risk.one_sided_exposure_cap must be positive")

    # Venue access validation
    polymarket_venue = config.venue_access.polymarket_venue.lower()
    kalshi_environment = config.venue_access.kalshi_environment.lower()
    if polymarket_venue not in VALID_POLYMARKET_VENUES:
        errors.append("venue_access.polymarket_venue must be 'global', 'us', 'none', or 'unconfirmed'")

    if kalshi_environment not in VALID_KALSHI_ENVIRONMENTS:
        errors.append("venue_access.kalshi_environment must be 'demo', 'production', 'none', or 'unconfirmed'")
    
    # Live mode checks
    if config.is_live:
        if not config.mode.live_trading_enabled:
            errors.append("mode.live_trading_enabled must be true for live mode")
        if not config.mode.manual_live_approval:
            errors.append("mode.manual_live_approval must be true for live mode")
        if os.environ.get(LIVE_CONFIRMATION_ENV) != LIVE_CONFIRMATION_VALUE:
            errors.append(
                f"{LIVE_CONFIRMATION_ENV} must equal {LIVE_CONFIRMATION_VALUE!r} for live mode"
            )
        if polymarket_venue == "unconfirmed":
            errors.append("venue_access.polymarket_venue must be confirmed before live mode")
        if kalshi_environment == "unconfirmed":
            errors.append("venue_access.kalshi_environment must be confirmed before live mode")
        if not config.venue_access.no_geoblock_workarounds:
            errors.append("venue_access.no_geoblock_workarounds must be true for live mode")
        if _is_placeholder_secret(config.api.api_key):
            errors.append("api.api_key is required for live trading")
        if _is_placeholder_secret(config.api.private_key):
            errors.append("api.private_key is required for live trading")
        errors.append("live order execution is not implemented in this repo stage; use scanner or paper")
    
    if errors:
        raise ConfigError("Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


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
