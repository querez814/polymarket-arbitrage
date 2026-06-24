"""
Paper Risk Runtime Settings
===========================

Builds effective paper-only strategy settings without mutating the base config.
"""

from dataclasses import dataclass

from utils.config_loader import BotConfig


@dataclass(frozen=True)
class PaperRuntimeSettings:
    """Effective strategy settings for the current mode."""
    min_edge: float
    default_order_size: float
    min_order_size: float
    max_order_size: float
    fill_probability: float
    bundle_short_enabled: bool
    market_making_enabled: bool
    cross_platform_synthetic_enabled: bool
    one_sided_exposure_cap: float
    quote_lifetime_seconds: float


def build_paper_runtime_settings(config: BotConfig) -> PaperRuntimeSettings:
    """Return effective settings, with paper-risk toggles ignored outside paper mode."""
    min_edge = config.trading.min_edge
    default_order_size = config.trading.default_order_size
    max_order_size = config.trading.max_order_size
    fill_probability = config.mode.fill_probability

    if config.is_paper and config.paper_risk.profile == "aggressive":
        min_edge = min(min_edge, config.paper_risk.aggressive_min_edge)
        default_order_size = max(default_order_size, config.paper_risk.aggressive_default_order_size)
        max_order_size = max(max_order_size, config.paper_risk.aggressive_max_order_size)
        fill_probability = max(fill_probability, config.paper_risk.aggressive_fill_probability)

    return PaperRuntimeSettings(
        min_edge=min_edge,
        default_order_size=default_order_size,
        min_order_size=config.trading.min_order_size,
        max_order_size=max_order_size,
        fill_probability=fill_probability,
        bundle_short_enabled=config.is_paper and config.paper_risk.paper_enable_bundle_short,
        market_making_enabled=config.is_paper and config.paper_risk.paper_enable_market_making,
        cross_platform_synthetic_enabled=(
            config.is_paper and config.paper_risk.paper_enable_cross_platform_synthetic
        ),
        one_sided_exposure_cap=config.paper_risk.one_sided_exposure_cap,
        quote_lifetime_seconds=config.paper_risk.quote_lifetime_seconds,
    )
