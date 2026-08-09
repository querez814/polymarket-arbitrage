#!/usr/bin/env python3
"""
Run Trading Bot with Dashboard
===============================

Starts the trading bot and web dashboard together.
Supports cross-platform arbitrage between Polymarket and Kalshi.

Usage:
    python run_with_dashboard.py              # Dry run mode
    python run_with_dashboard.py --live       # Live mode
    python run_with_dashboard.py --port 8080  # Custom port
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from polymarket_client import (
    OrderBook,
    PolymarketClient,
    PolymarketVenueAdapter,
    create_polymarket_client,
)
from kalshi_client import KalshiClient, KalshiPrivateStream, KalshiVenueAdapter
from core.data_feed import DataFeed
from core.combinatorial_arb import SamePlatformArbitrageDetector
from core.arb_engine import ArbEngine, ArbConfig
from core.execution import ExecutionEngine, ExecutionConfig
from core.risk_manager import RiskManager, RiskConfig
from core.portfolio import Portfolio
from core.cross_platform_arb import CrossPlatformArbEngine, MarketMatcher
from core.decision_journal import DecisionJournal, DecisionOutcome
from core.execution_economics import (
    AuthoritativeEconomicsProvider,
    EconomicsUnavailableError,
)
from core.execution_journal import ExecutionJournal
from core.two_leg_execution import ExecutionPhase
from core.operations import PersistentOperatorControls, WebhookAlertSink
from core.production_runtime import ProductionArbitrageRuntime, RuntimeNotReadyError
from core.paper_locked_arb import PaperLockedArbitrageLedger
from core.pair_monitoring import (
    PairScheduleOverride,
    PairTierMonitor,
    discovery_priority_score,
)
from core.pair_snapshot import (
    PairSnapshot,
    PairSnapshotError,
    PairSnapshotSource,
    executable_top_capacity,
    require_pair_snapshot_fresh,
)
from core.semantic_market_matching import (
    OpenAIEmbeddingClient,
    OpenAIResolutionVerifier,
    SemanticMarketPipeline,
    kalshi_document,
    polymarket_document,
)
from core.news_catalyst import (
    NewsCatalystMapper,
    NewsCatalystService,
    OpenAINewsProvider,
    PairRankingInput,
    TrackedMarket,
    rank_pairs,
)
from core.event_calendar import (
    BEACalendarSource,
    CensusCalendarSource,
    FREDEconomicCalendarSource,
    FederalReserveCalendarSource,
    HttpTextFetcher,
    OfficialEventCalendar,
)
from core.event_contracts import EventContractDiscovery, EventPairLink
from core.event_lane import EventLanePolicy, EventLaneScheduler
from core.platform_opportunities import (
    AcceptancePolicy,
    CatalystReference,
    MonitoringPolicy,
    PoliticalWatchPolicy,
    PlatformOpportunitySystem,
    VenueFeeSchedule,
    is_political_contract,
    kalshi_fee_schedule_from_metadata,
    normalize_kalshi,
    polymarket_fee_schedule_from_market_info,
)
from core.platform_opportunity_runtime import PlatformOpportunityWorker
from utils.config_loader import (
    BotConfig,
    load_config,
    resolve_runtime_secrets,
    validate_config,
)
from utils.logging_utils import opportunity_logger, setup_logging
from utils.paper_trade_store import PaperTradeStore
from utils.platform_opportunity_store import PlatformOpportunityStore
from utils.task_supervision import (
    RestartEvent,
    RestartingTaskSupervisor,
    is_transient_upstream_error,
)
from dashboard.server import app, dashboard_state, configure_dashboard_runtime
from dashboard.integration import DashboardIntegration

logger = logging.getLogger(__name__)


class TradingBotWithDashboard:
    """Trading bot with integrated dashboard."""

    def __init__(self, config: BotConfig, port: int = 8888):
        self.config = config
        self.port = port
        self._running = False

        # Components - Polymarket
        self.client = None
        self.data_feed = None
        self.arb_engine = None
        self.execution_engine = None
        self.risk_manager = None
        self.portfolio = None
        self.dashboard_integration = None
        self.decision_journal = DecisionJournal(max_records=1000)
        self.paper_trade_store = None
        self.paper_locked_arb = None
        self.same_platform_detector = None
        self._last_combinatorial_scan = 0.0
        self._last_combinatorial_status_log = 0.0
        self._combinatorial_scans = 0
        self._combinatorial_opportunities = 0
        self._startup_complete = False
        self._run_failed = False
        self._failure_event = asyncio.Event()

        # Components - Kalshi (cross-platform)
        self.kalshi_client = None
        self.cross_platform_engine = None
        self.market_matcher = None
        self._kalshi_markets = []
        self._matched_pairs = []
        self._polymarket_orderbooks: dict[str, OrderBook] = {}
        self._kalshi_orderbooks: dict[str, OrderBook] = {}
        self._preflight_snapshots: dict[str, PairSnapshot] = {}
        self._xplat_scan_task = None
        self._kalshi_monitor_task = None
        self._critical_failure_task = None
        self._news_catalyst_task = None
        self._news_catalyst_service = None
        self._news_market_scores: dict[tuple[str, str], float] = {}
        self._news_market_scores_updated_at: datetime | None = None
        self.event_calendar = None
        self.event_contract_discovery = None
        self.event_lane_scheduler = None
        self._event_calendar_snapshot = None
        self._event_calendar_refreshed_at: datetime | None = None
        self._event_pair_links: list[EventPairLink] = []
        self.platform_opportunity_store = None
        self.platform_opportunity_system = None
        self.platform_opportunity_worker = None
        self._platform_poly_client = None
        self._platform_kalshi_client = None
        self._platform_hot_task = None
        self._platform_catalog_task = None
        self._platform_hot_assignments = ()
        self._platform_catalog_refreshed_at: datetime | None = None
        self._platform_fee_cache: dict[str, tuple[VenueFeeSchedule, float]] = {}
        self._platform_book_read_failures = 0
        self._platform_fee_failures = 0
        self._platform_milestone_cache: dict[str, tuple[float, tuple]] = {}
        self._semantic_embedder = None
        self._discovery_supervisor = RestartingTaskSupervisor(
            "cross-platform discovery",
            on_retry=self._on_cross_platform_task_retry,
        )
        self._scanner_supervisor = RestartingTaskSupervisor(
            "cross-platform scanner",
            on_retry=self._on_cross_platform_task_retry,
        )
        self.production_runtime = None
        self.economics_provider = None
        self.pair_monitor = None
        self.pair_snapshot_source = None
        self.execution_journal = None
        self.operator_controls = None

        # Server
        self._server = None
        self._server_task = None

    @property
    def dashboard_host(self) -> str:
        # Bearer-protected operator mutations must not cross a plaintext LAN.
        # Remote production access belongs behind an authenticated TLS proxy.
        return "127.0.0.1" if self.config.is_live else "0.0.0.0"

    @property
    def failure_event(self) -> asyncio.Event:
        """Signal that a critical trading dependency ended terminally."""
        return self._failure_event

    @property
    def cross_platform_discovery_enabled(self) -> bool:
        """Whether the legacy Polymarket/Kalshi discovery runtime may start."""
        return bool(
            self.config.mode.cross_platform_enabled and self.config.mode.kalshi_enabled
        )

    def _build_semantic_pipeline(
        self,
        *,
        api_key: str,
        max_verification_candidates: int,
    ) -> SemanticMarketPipeline:
        """Build one independent matcher pipeline over the shared embedding cache."""
        if self._semantic_embedder is None:
            raise RuntimeError("semantic embedder must be configured first")
        return SemanticMarketPipeline(
            embedder=self._semantic_embedder,
            verifier=OpenAIResolutionVerifier(
                api_key=api_key,
                model=self.config.mode.semantic_verification_model,
            ),
            top_k=self.config.mode.semantic_top_k,
            retrieval_floor=self.config.mode.semantic_retrieval_floor,
            auto_approve_confidence=self.config.mode.semantic_auto_approve_confidence,
            max_verification_candidates=max_verification_candidates,
            category_cap_share=self.config.mode.semantic_category_cap_share,
            family_cap_share=self.config.mode.semantic_family_cap_share,
            exploration_share=self.config.mode.semantic_exploration_share,
            min_polymarket_liquidity=(
                self.config.mode.semantic_min_polymarket_liquidity
            ),
            min_polymarket_volume_24h=(
                self.config.mode.semantic_min_polymarket_volume_24h
            ),
            min_kalshi_volume=self.config.mode.semantic_min_kalshi_volume,
            min_kalshi_open_interest=(
                self.config.mode.semantic_min_kalshi_open_interest
            ),
        )

    def _configure_event_week(self, *, api_key: str) -> None:
        """Configure authoritative calendars without authorizing exchange mutation."""
        event_config = self.config.event_week
        dashboard_state.event_week = {
            "enabled": event_config.enabled,
            "status": "waiting" if event_config.enabled else "disabled",
            "last_calendar_refresh_at": None,
            "calendar_sources": [],
            "upcoming_events": [],
            "coverage": [],
            "verified_event_pairs": 0,
            "active_lanes": [],
            "scorecard": {},
        }
        if not event_config.enabled:
            return
        if self._semantic_embedder is None:
            raise RuntimeError("event-week discovery requires semantic matching")
        fetcher = HttpTextFetcher(timeout_seconds=self.config.api.timeout_seconds)
        self.event_calendar = OfficialEventCalendar(
            sources=(
                FREDEconomicCalendarSource(fetcher=fetcher),
                BEACalendarSource(fetcher=fetcher),
                FederalReserveCalendarSource(fetcher=fetcher),
                CensusCalendarSource(fetcher=fetcher),
            ),
            max_staleness=timedelta(seconds=event_config.source_max_staleness_seconds),
        )
        event_matcher = MarketMatcher(
            min_similarity=self.config.mode.min_match_similarity,
            semantic_pipeline=self._build_semantic_pipeline(
                api_key=api_key,
                max_verification_candidates=(
                    event_config.max_verification_candidates_per_event
                ),
            ),
        )
        self.event_contract_discovery = EventContractDiscovery(
            matcher=event_matcher,
            max_matcher_calls_per_cycle=max(
                1,
                event_config.max_verification_candidates_per_cycle
                // event_config.max_verification_candidates_per_event,
            ),
        )
        self.event_lane_scheduler = EventLaneScheduler(
            policy=EventLanePolicy(
                lookahead=timedelta(days=event_config.lookahead_days),
                warm_before=timedelta(hours=event_config.warm_before_hours),
                hot_before=timedelta(minutes=event_config.hot_before_minutes),
                burst_before=timedelta(minutes=event_config.burst_before_minutes),
                burst_after=timedelta(minutes=event_config.burst_after_minutes),
                cooldown_after=timedelta(hours=event_config.cooldown_after_hours),
                scheduled_interval_seconds=(event_config.scheduled_interval_seconds),
                warm_interval_seconds=event_config.warm_interval_seconds,
                hot_interval_seconds=event_config.hot_interval_seconds,
                burst_interval_seconds=event_config.burst_interval_seconds,
                cooldown_interval_seconds=event_config.cooldown_interval_seconds,
            )
        )
        logger.info(
            "Scheduled event-week lane configured | lookahead=%.1fd refresh=%.0fs "
            "execution_authority=locked_edge_only",
            event_config.lookahead_days,
            event_config.calendar_refresh_seconds,
        )

    async def start(self) -> None:
        """Start the bot and dashboard."""
        logger.info("=" * 60)
        logger.info("Polymarket + Kalshi Arbitrage Bot")
        logger.info("=" * 60)
        logger.info(f"Mode: {'DRY RUN' if self.config.is_dry_run else 'LIVE'}")
        logger.info(
            f"Cross-Platform: {'ENABLED' if self.config.mode.cross_platform_enabled else 'DISABLED'}"
        )
        logger.info(f"Dashboard: http://localhost:{self.port}")
        logger.info("=" * 60)

        self._running = True

        configure_dashboard_runtime(
            store=None,
            timezone=self.config.monitoring.display_timezone,
            production_required=(
                self.config.is_live
                and self.config.mode.cross_platform_execution_enabled
            ),
            readiness_check=self._critical_dependencies_ready,
        )
        if self.config.is_dry_run:
            self.paper_trade_store = PaperTradeStore(
                self.config.monitoring.paper_trade_db_path
            )
            active_run = self.paper_trade_store.start_run(
                starting_equity=self.config.mode.dry_run_initial_balance,
                pnl_source="projected_locked_paper",
            )
            dashboard_state.run_session = active_run.to_dict()
            dashboard_state.run_sessions = [
                run.to_dict() for run in self.paper_trade_store.recent_runs(limit=50)
            ]
            logger.info(
                "Paper run #%s started | run_id=%s | starting equity=$%.2f",
                active_run.run_number,
                active_run.run_id,
                active_run.starting_equity,
            )
            configure_dashboard_runtime(
                store=self.paper_trade_store,
                timezone=self.config.monitoring.display_timezone,
                readiness_check=self._critical_dependencies_ready,
            )
            dashboard_state.paper_history = [
                event.to_dict()
                for event in self.paper_trade_store.recent_events(limit=200)
            ]
            if (
                self.config.mode.paper_locked_arb_enabled
                and self.config.mode.cross_platform_enabled
            ):
                self.paper_locked_arb = PaperLockedArbitrageLedger(
                    initial_balance=self.config.mode.dry_run_initial_balance,
                    max_plan_capital=self.config.risk.max_order_notional,
                    max_contracts_per_trade=(
                        self.config.trading.cross_platform_max_order_size
                    ),
                    max_pair_capital=self.config.risk.max_position_per_market,
                    max_total_capital=min(
                        self.config.risk.max_global_exposure,
                        self.config.risk.strategy_exposure_limits.get(
                            "cross_platform_arb",
                            self.config.risk.max_global_exposure,
                        ),
                    ),
                    max_open_pairs=self.config.risk.max_open_positions,
                    required_observations=(
                        self.config.mode.paper_confirmation_observations
                    ),
                    slippage_buffer_per_contract=(
                        self.config.mode.paper_slippage_buffer_per_contract
                    ),
                    liquidity_fraction=self.config.mode.paper_liquidity_fraction,
                    min_effective_edge=self.config.trading.min_edge,
                    approved_market_ids=set(self.config.risk.whitelist),
                    blacklisted_market_ids=set(self.config.risk.blacklist),
                    allow_verified_auto_approval=(
                        self.config.mode.semantic_matching_enabled
                    ),
                    auto_approval_confidence=(
                        self.config.mode.semantic_auto_approve_confidence
                    ),
                    pair_cooldown_seconds=(
                        self.config.mode.paper_pair_cooldown_seconds
                    ),
                    store=self.paper_trade_store,
                )
                dashboard_state.cross_platform["paper_performance"] = (
                    self.paper_locked_arb.summary()
                )

        # Initialize Polymarket API client
        self.client = create_polymarket_client(self.config)
        await self.client.connect()

        # Initialize Kalshi client (if cross-platform enabled)
        if self.cross_platform_discovery_enabled:
            logger.info("Initializing Kalshi client for cross-platform arbitrage...")
            self.kalshi_client = KalshiClient(
                base_url=self.config.api.kalshi_api_url,
                api_key_id=self.config.api.kalshi_api_key_id or None,
                private_key_path=self.config.api.kalshi_private_key_path or None,
                timeout=self.config.api.timeout_seconds,
                max_retries=self.config.api.max_retries,
                dry_run=self.config.is_dry_run,
            )
            await self.kalshi_client.__aenter__()

            # Initialize cross-platform arbitrage engine
            self.cross_platform_engine = CrossPlatformArbEngine(
                min_edge=self.config.trading.min_edge,
                slippage_reserve_per_contract=(
                    self.config.mode.paper_slippage_buffer_per_contract
                    if self.config.is_dry_run
                    else 0.0
                ),
                max_order_size=self.config.trading.cross_platform_max_order_size,
                edge_size_multiplier=self.config.trading.cross_platform_edge_size_multiplier,
                max_liquidity_fraction=self.config.trading.cross_platform_max_liquidity_fraction,
                min_executable_size=(
                    self.config.trading.cross_platform_min_executable_size
                ),
                require_authoritative_economics=(self.config.mode.data_mode == "real"),
                economics_max_age=timedelta(
                    seconds=self.config.production.economics_max_age_seconds
                ),
            )
            if self.config.mode.data_mode == "real":
                self.economics_provider = AuthoritativeEconomicsProvider(
                    self.client, self.kalshi_client
                )
            if self.config.mode.semantic_matching_enabled:
                api_key = os.environ.get("OPENAI_API_KEY", "").strip()
                if not api_key:
                    raise RuntimeError(
                        "OPENAI_API_KEY is required when semantic matching is enabled"
                    )
                self._semantic_embedder = OpenAIEmbeddingClient(
                    api_key=api_key,
                    cache_path=self.config.mode.semantic_cache_path,
                    model=self.config.mode.semantic_embedding_model,
                    dimensions=self.config.mode.semantic_embedding_dimensions,
                )
                semantic_pipeline = self._build_semantic_pipeline(
                    api_key=api_key,
                    max_verification_candidates=(
                        self.config.mode.semantic_max_verification_candidates
                    ),
                )
                self.market_matcher = MarketMatcher(
                    min_similarity=self.config.mode.min_match_similarity,
                    semantic_pipeline=semantic_pipeline,
                )
                self.cross_platform_engine.matcher = self.market_matcher
                self._configure_event_week(api_key=api_key)
            else:
                self.market_matcher = self.cross_platform_engine.matcher
                self.market_matcher.min_similarity = (
                    self.config.mode.min_match_similarity
                )
            self.pair_monitor = PairTierMonitor(
                hot_limit=self.config.mode.hot_pair_limit,
                hot_interval=self.config.mode.hot_pair_scan_seconds,
                cold_interval=self.config.mode.cold_pair_scan_seconds,
            )
            max_snapshot_age = (
                self.cross_platform_engine.max_observation_age or timedelta(seconds=5)
            ).total_seconds()
            self.pair_snapshot_source = PairSnapshotSource(
                self.client,
                self.kalshi_client,
                max_age_seconds=max_snapshot_age,
                timeout_seconds=max_snapshot_age,
            )

            if (
                self.config.is_live
                and self.config.mode.cross_platform_execution_enabled
            ):
                await self._start_production_runtime()

            # Start Kalshi monitoring in background
            logger.info(
                "Cross-platform task supervision active "
                "(transient 429/5xx/connection failures restart with backoff)"
            )
            self._kalshi_monitor_task = asyncio.create_task(
                self._discovery_supervisor.run(
                    self._start_kalshi_monitoring,
                    should_run=lambda: self._running,
                )
            )
            self._kalshi_monitor_task.add_done_callback(self._critical_task_done)

        # Initialize portfolio
        initial_balance = (
            self.config.mode.dry_run_initial_balance if self.config.is_dry_run else 0.0
        )
        self.portfolio = Portfolio(initial_balance=initial_balance)
        if self.config.is_live:
            live_balance = await self.client.get_usdc_balance()
            if live_balance is not None:
                self.portfolio.cash_balance = live_balance
                self.portfolio.initial_balance = live_balance
                logger.info(f"Live USDC balance synced: ${live_balance:.2f}")

        # Initialize risk manager
        self.risk_manager = RiskManager(
            RiskConfig(
                max_order_notional=self.config.risk.max_order_notional,
                max_open_orders=self.config.risk.max_open_orders,
                max_open_positions=self.config.risk.max_open_positions,
                max_order_attempts_per_minute=self.config.risk.max_order_attempts_per_minute,
                max_daily_order_attempts=self.config.risk.max_daily_order_attempts,
                max_position_per_market=self.config.risk.max_position_per_market,
                max_global_exposure=self.config.risk.max_global_exposure,
                max_daily_loss=self.config.risk.max_daily_loss,
                max_drawdown_pct=self.config.risk.max_drawdown_pct,
                trade_only_high_volume=self.config.risk.trade_only_high_volume,
                min_24h_volume=self.config.risk.min_24h_volume,
                whitelist=self.config.risk.whitelist,
                blacklist=self.config.risk.blacklist,
                kill_switch_enabled=self.config.risk.kill_switch_enabled,
                strategy_exposure_limits=self.config.risk.strategy_exposure_limits,
            )
        )

        # Initialize execution engine
        self.execution_engine = ExecutionEngine(
            client=self.client,
            risk_manager=self.risk_manager,
            portfolio=self.portfolio,
            config=ExecutionConfig(
                slippage_tolerance=self.config.trading.slippage_tolerance,
                order_timeout_seconds=self.config.trading.order_timeout_seconds,
                strategy_slippage_tolerances={
                    "bundle_arb": self.config.trading.arb_slippage_tolerance,
                    "market_making": self.config.trading.market_making_slippage_tolerance,
                    "cross_platform_arb": self.config.trading.arb_slippage_tolerance,
                },
                strategy_order_timeouts={
                    "bundle_arb": self.config.trading.arb_order_timeout_seconds,
                    "market_making": self.config.trading.market_making_order_timeout_seconds,
                    "cross_platform_arb": self.config.trading.arb_order_timeout_seconds,
                },
                high_edge_slippage_multiplier=self.config.trading.high_edge_slippage_multiplier,
                dry_run=self.config.is_dry_run,
            ),
            decision_journal=self.decision_journal,
            paper_trade_store=self.paper_trade_store,
        )
        await self.execution_engine.start()

        # Initialize arb engine
        self.arb_engine = ArbEngine(
            ArbConfig(
                min_edge=self.config.trading.min_edge,
                bundle_arb_enabled=self.config.trading.bundle_arb_enabled,
                min_spread=self.config.trading.min_spread,
                mm_enabled=self.config.trading.mm_enabled,
                tick_size=self.config.trading.tick_size,
                mm_one_sided_enabled=self.config.trading.mm_one_sided_enabled,
                default_order_size=self.config.trading.default_order_size,
                min_order_size=self.config.trading.min_order_size,
                max_order_size=self.config.trading.max_order_size,
                edge_size_multiplier=self.config.trading.edge_size_multiplier,
                max_liquidity_fraction=self.config.trading.max_liquidity_fraction,
                bundle_cooldown_seconds=self.config.trading.bundle_cooldown_seconds,
                mm_cooldown_seconds=self.config.trading.mm_cooldown_seconds,
            ),
            decision_journal=self.decision_journal,
        )
        if self.config.trading.bundle_arb_enabled:
            self.same_platform_detector = SamePlatformArbitrageDetector(
                min_edge=self.config.trading.min_edge,
                taker_fee_rate=150 / 10_000,
                cooldown_seconds=max(5.0, self.config.trading.bundle_cooldown_seconds),
            )

        # Initialize data feed
        market_ids = self.config.trading.markets.copy()
        self.data_feed = DataFeed(
            client=self.client,
            market_ids=market_ids,
            position_refresh_interval=5.0,
            on_update=self._on_market_update,
            config=self.config,
        )
        await self.data_feed.start()

        await self._start_platform_opportunity_safely()

        self._configure_news_catalyst_scanner()

        # Initialize dashboard integration
        self.dashboard_integration = DashboardIntegration(
            data_feed=self.data_feed,
            arb_engine=self.arb_engine,
            execution_engine=self.execution_engine,
            risk_manager=self.risk_manager,
            portfolio=self.portfolio,
            mode="dry_run" if self.config.is_dry_run else "live",
            decision_journal=self.decision_journal,
            paper_trade_store=self.paper_trade_store,
            paper_performance_provider=self._paper_run_performance,
            paper_strategy_summary_provider=(
                self.paper_locked_arb.summary if self.paper_locked_arb else None
            ),
        )
        await self.dashboard_integration.start()

        # Start fill simulation for dry run
        if self.config.is_dry_run and self.config.mode.simulate_fills:
            asyncio.create_task(self._simulate_fills())

        # Start the web server
        await self._start_server()
        if self._news_catalyst_service is not None:
            self._news_catalyst_task = asyncio.create_task(
                self._news_catalyst_loop(),
                name="news_catalyst_scanner",
            )
        self._startup_complete = True
        logger.info("Bot and dashboard started successfully!")

    def _paper_run_performance(self) -> tuple[float, float]:
        """Return current paper equity and PnL using the active strategy ledger."""
        if self.paper_locked_arb is not None:
            summary = self.paper_locked_arb.summary()
            return (
                float(summary["projected_equity_at_settlement"]),
                float(summary["projected_locked_pnl"]),
            )
        if self.portfolio is not None:
            summary = self.portfolio.get_summary()
            pnl = float(summary["pnl"]["total_pnl"])
            return float(summary["initial_balance"]) + pnl, pnl
        return float(self.config.mode.dry_run_initial_balance), 0.0

    async def _configure_platform_opportunity_system(self) -> None:
        """Start isolated, read-only research resources and persistence."""
        policy = self.config.platform_opportunity
        dashboard_state.platform_opportunity = {
            "enabled": policy.enabled,
            "mode": "shadow_only" if policy.enabled else "disabled",
            "status": "starting" if policy.enabled else "disabled",
            "execution_authority": "none",
            "main_paper_fills_pnl": "disabled",
            "strategy_lanes": {
                "directional_reaction": {
                    "authority": policy.directional_reaction_authority
                },
                "relative_value": {"authority": policy.relative_value_authority},
            },
            "catalog": {"contracts": 0, "revisions": 0},
            "monitoring": {"hot": [], "warm_count": 0, "budget_excluded": []},
            "relations": 0,
            "intents": {},
            "marks": 0,
            "political_event_locks": [],
            "sampled_contract_ids": [],
            "research_pnl": {
                "authority": "shadow_research_only",
                "label": "shadow_research_marks",
                "actual_exit": {"marks": 0, "scored_marks": 0, "capacity_pnl": 0.0},
                "horizons": {},
            },
            "acceptance": {},
            "book_read_failures": 0,
            "fee_metadata_failures": 0,
            "worker": {"queued": 0, "processed": 0, "dropped": 0, "failures": 0},
        }
        if not policy.enabled:
            return
        self.platform_opportunity_store = PlatformOpportunityStore(
            policy.catalog_path,
            replay_byte_cap=policy.replay_evidence_byte_cap,
        )
        self.platform_opportunity_system = PlatformOpportunitySystem(
            store=self.platform_opportunity_store,
            monitoring_policy=MonitoringPolicy(
                lookahead=timedelta(days=policy.lookahead_days),
                max_hot_contracts=policy.max_hot_contracts,
                min_liquidity=policy.min_liquidity,
                min_volume=policy.min_volume,
            ),
            political_watch_policy=PoliticalWatchPolicy(
                max_events=policy.political_max_events,
                max_contracts_per_event=policy.political_max_contracts_per_event,
                lookahead=timedelta(days=policy.political_lookahead_days),
                warm_before=timedelta(hours=policy.political_warm_before_hours),
                hot_before=timedelta(minutes=policy.political_hot_before_minutes),
                warm_poll_seconds=policy.political_warm_poll_seconds,
                hot_poll_seconds=policy.political_hot_poll_seconds,
                event_poll_seconds=policy.political_event_poll_seconds,
                cooldown_poll_seconds=policy.political_cooldown_poll_seconds,
                cooldown_after=timedelta(hours=policy.political_cooldown_after_hours),
                max_event_duration=timedelta(
                    hours=policy.political_max_event_duration_hours
                ),
                reviewed_pinned_event_ids=tuple(policy.reviewed_pinned_event_ids),
            ),
            acceptance_policy=AcceptancePolicy(
                min_event_clusters=policy.min_event_clusters,
                min_intents=policy.min_intents,
                max_drawdown=policy.max_research_drawdown,
            ),
            additional_fee_buffer_per_contract=(
                policy.additional_fee_buffer_per_contract
            ),
            slippage_per_contract=policy.slippage_per_contract,
            max_shadow_notional=policy.max_shadow_notional,
            experiment_id=policy.experiment_id,
            lane_authorities={
                "directional_reaction": policy.directional_reaction_authority,
                "relative_value": policy.relative_value_authority,
            },
        )

        def publish(payload: dict) -> None:
            catalog = payload.pop("catalog", None)
            dashboard_state.platform_opportunity.update(payload)
            if isinstance(catalog, dict):
                dashboard_state.platform_opportunity.setdefault("catalog", {}).update(
                    catalog
                )

        self.platform_opportunity_worker = PlatformOpportunityWorker(
            self.platform_opportunity_system,
            queue_capacity=policy.queue_capacity,
            on_update=publish,
        )
        await self.platform_opportunity_worker.start()

        # Dedicated public read pools prevent research persistence or sampling
        # from queueing behind the locked-arbitrage execution clients.
        if not self.config.is_polymarket_us:
            self._platform_poly_client = PolymarketClient(
                rest_url=self.config.api.polymarket_rest_url,
                ws_url=self.config.api.polymarket_ws_url,
                gamma_url=self.config.api.gamma_api_url,
                timeout=self.config.api.timeout_seconds,
                max_retries=self.config.api.max_retries,
                retry_delay=self.config.api.retry_delay_seconds,
                dry_run=True,
            )
            await self._platform_poly_client.connect()
        if self.config.mode.kalshi_enabled:
            self._platform_kalshi_client = KalshiClient(
                base_url=self.config.api.kalshi_api_url,
                timeout=self.config.api.timeout_seconds,
                max_retries=self.config.api.max_retries,
                dry_run=True,
            )
            await self._platform_kalshi_client.__aenter__()
        self._platform_hot_task = asyncio.create_task(
            self._platform_hot_sampling_loop(),
            name="platform_opportunity_hot_sampler",
        )
        self._platform_catalog_task = asyncio.create_task(
            self._platform_catalog_loop(),
            name="platform_opportunity_catalog",
        )
        logger.info(
            "Platform-first shadow system started | catalog=%s hot_cap=%s authority=none",
            policy.catalog_path,
            policy.max_hot_contracts,
        )

    async def _start_platform_opportunity_safely(self) -> None:
        """A shadow-only failure must never abort locked-arbitrage startup."""
        try:
            await self._configure_platform_opportunity_system()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Platform-first shadow subsystem failed to start; "
                "locked-arbitrage runtime will continue"
            )
            await self._shutdown_platform_opportunity_system()
            dashboard_state.platform_opportunity.update(
                {
                    "enabled": True,
                    "mode": "shadow_only",
                    "status": "degraded",
                    "execution_authority": "none",
                    "last_error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )

    async def _shutdown_platform_opportunity_system(self) -> None:
        """Clean up any partially initialized research-only resources."""
        for task in (self._platform_catalog_task, self._platform_hot_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._platform_catalog_task = None
        self._platform_hot_task = None
        if self.platform_opportunity_worker:
            await self.platform_opportunity_worker.stop()
            self.platform_opportunity_worker = None
        if self._platform_poly_client:
            await self._platform_poly_client.disconnect()
            self._platform_poly_client = None
        if self._platform_kalshi_client:
            await self._platform_kalshi_client.__aexit__(None, None, None)
            self._platform_kalshi_client = None
        if self.platform_opportunity_store:
            self.platform_opportunity_store.close()
            self.platform_opportunity_store = None
        self.platform_opportunity_system = None

    def _platform_catalyst_references(self) -> list[CatalystReference]:
        snapshot = self._event_calendar_snapshot
        if snapshot is None:
            return []
        return [
            CatalystReference(
                reference_id=event.event_id,
                title=event.title,
                scheduled_at=event.scheduled_at,
                source=event.source_url,
                authoritative=True,
            )
            for event in snapshot.fresh_events
            if event.source_url
        ]

    async def _refresh_platform_catalog(self) -> None:
        worker = self.platform_opportunity_worker
        if worker is None:
            return
        now = datetime.now(timezone.utc)
        interval = self.config.platform_opportunity.catalog_refresh_seconds
        if (
            self._platform_catalog_refreshed_at is not None
            and (now - self._platform_catalog_refreshed_at).total_seconds() < interval
        ):
            return
        dashboard_state.platform_opportunity["status"] = "refreshing_catalog"
        poly_markets = []
        poly_catalog_status = {
            "complete": False,
            "stop_reason": "dedicated_source_unavailable",
        }
        if self._platform_poly_client is not None:
            try:
                poly_markets = await self._platform_poly_client.list_all_markets_keyset(
                    closed=False,
                    filters={"active": "true"},
                    max_markets=self.config.platform_opportunity.catalog_max_markets_per_venue,
                    max_pages=self.config.platform_opportunity.catalog_max_pages,
                    max_decoded_bytes=self.config.platform_opportunity.catalog_max_decoded_bytes,
                    wall_time_seconds=self.config.platform_opportunity.catalog_wall_time_seconds,
                )
                poly_catalog_status = dict(
                    self._platform_poly_client.last_catalog_status
                )
            except Exception as exc:
                logger.warning("polymarket platform catalog refresh failed: %s", exc)
                poly_catalog_status = {
                    "complete": False,
                    "stop_reason": "fetch_failed",
                    "failed": True,
                }
        elif self.data_feed is not None:
            poly_markets = list(self.data_feed._markets.values())
        ordinary_kalshi = []
        kalshi_not_applicable = not self.config.mode.kalshi_enabled
        ordinary_status = {
            "complete": kalshi_not_applicable,
            "stop_reason": (
                "not_applicable"
                if kalshi_not_applicable
                else "dedicated_source_unavailable"
            ),
        }
        if self._platform_kalshi_client is not None:
            try:
                ordinary_kalshi = await self._platform_kalshi_client.list_full_market_catalog(
                    status="open",
                    mve_filter="exclude",
                    max_markets=self.config.platform_opportunity.catalog_max_markets_per_venue,
                    max_pages=self.config.platform_opportunity.catalog_max_pages,
                    max_decoded_bytes=self.config.platform_opportunity.catalog_max_decoded_bytes,
                    wall_time_seconds=self.config.platform_opportunity.catalog_wall_time_seconds,
                )
                ordinary_status = dict(self._platform_kalshi_client.last_catalog_status)
            except Exception as exc:
                logger.warning("kalshi platform catalog refresh failed: %s", exc)
                ordinary_status = {
                    "complete": False,
                    "stop_reason": "fetch_failed",
                    "failed": True,
                }
        elif self._kalshi_markets:
            ordinary_kalshi = list(self._kalshi_markets)
        kalshi_by_ticker = {market.ticker: market for market in ordinary_kalshi}
        kalshi_milestones, milestone_status = await self._political_kalshi_milestones(
            list(kalshi_by_ticker.values()), now=now
        )
        refresh = await worker.refresh_catalog(
            polymarket_markets=poly_markets,
            kalshi_markets=list(kalshi_by_ticker.values()),
            kalshi_milestones=kalshi_milestones,
            catalyst_references=self._platform_catalyst_references(),
            snapshot_complete=all(
                bool(status.get("complete"))
                for status in (
                    poly_catalog_status,
                    ordinary_status,
                )
            ),
            venue_coverage={
                "polymarket": {
                    "status": (
                        "failure"
                        if poly_catalog_status.get("failed")
                        else (
                            "complete"
                            if poly_catalog_status.get("complete")
                            else "partial"
                        )
                    ),
                    "reason": str(
                        poly_catalog_status.get("stop_reason", "partial_response")
                    ),
                },
                "kalshi": {
                    "status": (
                        "failure"
                        if ordinary_status.get("failed")
                        else (
                            "complete" if ordinary_status.get("complete") else "partial"
                        )
                    ),
                    "reason": str(
                        ordinary_status.get("stop_reason", "partial_response")
                    ),
                },
            },
            observed_at=now,
        )
        # Warm assignments normally are inventory-only metadata.  Locked
        # political events are the bounded exception: they need a baseline
        # before the hot window, so preserve only those in the poller.
        self._platform_hot_assignments = (
            *refresh.monitoring.hot,
            *(
                item
                for item in refresh.monitoring.warm
                if item.reason == "political_event_lock"
            ),
        )
        self._platform_catalog_refreshed_at = now

        def assignment_payload(item) -> dict:
            payload = asdict(item)
            catalyst_at = payload.get("catalyst_at")
            payload["catalyst_at"] = (
                catalyst_at.isoformat() if catalyst_at is not None else None
            )
            return payload

        dashboard_state.platform_opportunity.update(
            {
                "status": "running",
                "last_catalog_refresh_at": now.isoformat(),
                "catalog": {
                    "contracts": refresh.catalog_contracts,
                    "revisions_written": refresh.revisions_written,
                    "polymarket": len(poly_markets),
                    "kalshi": len(kalshi_by_ticker),
                    "multivariate_requests": 0,
                    "source_status": {
                        "polymarket": poly_catalog_status,
                        "kalshi_ordinary": ordinary_status,
                        "kalshi_political_milestones": milestone_status,
                    },
                    # Transitional test doubles and external shadow workers may
                    # not yet return the new coverage field; the real worker
                    # always does, and no invented success state is shown here.
                    "venue_coverage": getattr(refresh, "venue_coverage", {}),
                    "possibly_truncated": not all(
                        bool(status.get("complete"))
                        for status in (
                            poly_catalog_status,
                            ordinary_status,
                        )
                    ),
                },
                "monitoring": {
                    "hot": [
                        assignment_payload(item) for item in refresh.monitoring.hot
                    ],
                    "warm_count": len(refresh.monitoring.warm),
                    "budget_excluded": [
                        assignment_payload(item)
                        for item in refresh.monitoring.budget_excluded
                    ],
                },
            }
        )

    async def _political_kalshi_milestones(self, markets, *, now: datetime):
        """Fetch exact, bounded milestone evidence for political event tickers.

        The generic milestone page is intentionally never used here: its sort
        order is unrelated to the political watchlist and can omit target
        events.  Pins are included before automatic candidates, but still use
        the same exact related-event-ticker endpoint and client-side window.
        """
        policy = self.config.platform_opportunity
        empty_status = {
            "requested_event_tickers": 0,
            "successful_event_tickers": 0,
            "cached_event_tickers": 0,
            "milestones": 0,
            "errors": {},
        }
        if self._platform_kalshi_client is None:
            return [], {**empty_status, "status": "dedicated_source_unavailable"}

        pins = tuple(
            event_id.removeprefix("kalshi:")
            for event_id in policy.reviewed_pinned_event_ids
            if event_id.startswith("kalshi:")
        )
        automatic = sorted(
            {
                market.event_ticker
                for market in markets
                if market.event_ticker
                and is_political_contract(normalize_kalshi(market))
            }
        )
        event_tickers = tuple(dict.fromkeys((*pins, *automatic)))[
            : policy.political_milestone_max_event_tickers
        ]
        lookahead = now + timedelta(days=policy.political_lookahead_days)
        semaphore = asyncio.Semaphore(policy.political_milestone_concurrency)
        errors: dict[str, str] = {}
        cached = 0

        async def fetch(event_ticker: str):
            nonlocal cached
            cached_value = self._platform_milestone_cache.get(event_ticker)
            if (
                cached_value
                and time.monotonic() - cached_value[0]
                < policy.political_milestone_cache_seconds
            ):
                cached += 1
                return cached_value[1]
            try:
                async with semaphore:
                    milestones = await self._platform_kalshi_client.list_all_milestones(
                        max_pages=policy.political_milestone_max_pages_per_event,
                        max_milestones=policy.political_milestone_max_results_per_event,
                        related_event_ticker=event_ticker,
                    )
            except Exception as exc:
                errors[event_ticker] = f"{type(exc).__name__}: {exc}"[:200]
                return ()
            exact = tuple(
                milestone
                for milestone in milestones
                if event_ticker in milestone.related_event_tickers
                and event_ticker in milestone.primary_event_tickers
                and milestone.end_time is not None
                and milestone.end_time > milestone.start_time
                and milestone.start_time <= lookahead
                and milestone.end_time > now
            )
            self._platform_milestone_cache[event_ticker] = (time.monotonic(), exact)
            return exact

        results = await asyncio.gather(*(fetch(ticker) for ticker in event_tickers))
        milestones = [milestone for result in results for milestone in result]
        return milestones, {
            "status": "partial" if errors else "complete",
            "requested_event_tickers": len(event_tickers),
            "successful_event_tickers": len(event_tickers) - len(errors),
            "cached_event_tickers": cached,
            "milestones": len(milestones),
            "errors": errors,
        }

    async def _platform_hot_sampling_loop(self) -> None:
        """Poll bounded hot contracts on isolated public read pools."""
        next_due: dict[str, float] = {}
        while self._running and self.platform_opportunity_worker is not None:
            now_mono = time.monotonic()
            due = [
                assignment
                for assignment in self._platform_hot_assignments
                if next_due.get(assignment.contract_id, 0.0) <= now_mono
            ][:8]
            if not due:
                await asyncio.sleep(0.25)
                continue

            async def fee_schedule(assignment):
                cached = self._platform_fee_cache.get(assignment.contract_id)
                if cached is not None and cached[1] > time.monotonic():
                    return cached[0]
                observed_at = datetime.now(timezone.utc)
                if assignment.venue == "polymarket":
                    if self._platform_poly_client is None:
                        raise RuntimeError("Polymarket research client unavailable")
                    market = await self._platform_poly_client.get_market(
                        assignment.native_id
                    )
                    if market is None or not market.condition_id:
                        raise RuntimeError("Polymarket condition metadata unavailable")
                    info = await self._platform_poly_client.get_clob_market_info(
                        market.condition_id
                    )
                    schedule = polymarket_fee_schedule_from_market_info(
                        info, observed_at=observed_at
                    )
                else:
                    if self._platform_kalshi_client is None:
                        raise RuntimeError("Kalshi research client unavailable")
                    metadata = await self._platform_kalshi_client.get_fee_schedule(
                        assignment.native_id
                    )
                    schedule = kalshi_fee_schedule_from_metadata(
                        fee_type=metadata.fee_type,
                        multiplier=metadata.fee_multiplier,
                        observed_at=observed_at,
                        source=metadata.source,
                    )
                self._platform_fee_cache[assignment.contract_id] = (
                    schedule,
                    time.monotonic() + 300.0,
                )
                return schedule

            async def fetch(assignment):
                request_started_at = datetime.now(timezone.utc)
                try:
                    if assignment.venue == "polymarket":
                        if self._platform_poly_client is None:
                            raise RuntimeError("Polymarket research client unavailable")
                        book = await self._platform_poly_client.get_orderbook(
                            assignment.native_id
                        )
                    else:
                        if self._platform_kalshi_client is None:
                            raise RuntimeError("Kalshi research client unavailable")
                        book = await self._platform_kalshi_client.get_orderbook_unified(
                            assignment.native_id
                        )
                    received_at = datetime.now(timezone.utc)
                except Exception:
                    self._platform_book_read_failures += 1
                    self._record_platform_observation_failure(
                        assignment.contract_id,
                        reason_code="book_read_failed",
                        failed_at=datetime.now(timezone.utc),
                    )
                    dashboard_state.platform_opportunity["book_read_failures"] = (
                        self._platform_book_read_failures
                    )
                    logger.warning(
                        "Shadow hot-book read failed | contract=%s",
                        assignment.contract_id,
                        exc_info=True,
                    )
                    return assignment, None, None, None, None
                try:
                    schedule = await fee_schedule(assignment)
                    return assignment, book, schedule, request_started_at, received_at
                except Exception:
                    self._platform_fee_failures += 1
                    self._record_platform_observation_failure(
                        assignment.contract_id,
                        reason_code="fee_metadata_failed",
                        failed_at=datetime.now(timezone.utc),
                    )
                    dashboard_state.platform_opportunity["fee_metadata_failures"] = (
                        self._platform_fee_failures
                    )
                    logger.warning(
                        "Shadow hot-book read failed | contract=%s",
                        assignment.contract_id,
                        exc_info=True,
                    )
                    return assignment, None, None, None, None

            results = await asyncio.gather(*(fetch(item) for item in due))
            for assignment, book, schedule, request_started_at, received_at in results:
                next_due[assignment.contract_id] = time.monotonic() + max(
                    self.config.platform_opportunity.hot_poll_seconds,
                    assignment.interval_seconds,
                )
                if book is not None and schedule is not None:
                    self.platform_opportunity_worker.submit_book(
                        assignment.contract_id,
                        book,
                        # The unified-book timestamp is not a verified venue
                        # timestamp. Receipt is the truthful local observation
                        # time carried into durable evidence.
                        observed_at=received_at,
                        request_started_at=request_started_at,
                        received_at=received_at,
                        fee_schedule=schedule,
                    )

    def _record_platform_observation_failure(
        self,
        contract_id: str,
        *,
        reason_code: str,
        failed_at: datetime,
    ) -> None:
        """Keep public sampling alive if optional failure telemetry is unavailable."""
        system = self.platform_opportunity_system
        if system is None:
            # Shutdown/startup races can leave the optional shadow system absent.
            # Degrade transparently instead of relying on an AttributeError path.
            dashboard_state.platform_opportunity.update(
                {
                    "status": "degraded",
                    "last_error": "platform observation telemetry not initialized",
                }
            )
            logger.warning(
                "Platform observation failure telemetry not initialized | contract=%s",
                contract_id,
            )
            return
        try:
            system.record_observation_failure(
                contract_id,
                reason_code=reason_code,
                failed_at=failed_at,
            )
            dashboard_state.platform_opportunity.update(system.dashboard_summary())
        except Exception as exc:
            # A shadow-only telemetry outage must never leave the sampler task
            # dead while its dashboard state still claims to be running.
            try:
                dashboard_state.platform_opportunity.update(
                    {
                        "status": "degraded",
                        "last_error": (
                            f"{type(exc).__name__}: platform observation telemetry "
                            "unavailable"
                        )[:300],
                    }
                )
            except Exception:
                logger.exception(
                    "Unable to publish platform observation telemetry failure"
                )
            logger.exception(
                "Platform observation failure telemetry unavailable | contract=%s",
                contract_id,
            )

    async def _platform_catalog_loop(self) -> None:
        """Refresh source-platform inventory independently of calendars/matching."""
        interval = self.config.platform_opportunity.catalog_refresh_seconds
        while self._running and self.platform_opportunity_worker is not None:
            try:
                await self._refresh_platform_catalog()
                worker = self.platform_opportunity_worker
                if worker is not None:
                    directional, relative = await asyncio.gather(
                        worker.acceptance_report("directional_reaction"),
                        worker.acceptance_report("relative_value"),
                    )
                    dashboard_state.platform_opportunity["acceptance"] = {
                        "directional_reaction": asdict(directional),
                        "relative_value": asdict(relative),
                    }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                dashboard_state.platform_opportunity.update(
                    {
                        "status": "degraded",
                        "last_error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                logger.exception("Platform-first catalog refresh failed")
            await asyncio.sleep(interval)

    def _configure_news_catalyst_scanner(self) -> None:
        """Configure source-backed log-only scanning without authorizing trades."""
        news = self.config.news_catalyst
        dashboard_state.news_catalysts = {
            "enabled": news.enabled,
            "apply_priority_boost": news.apply_priority_boost,
            "status": "log_only" if news.enabled else "disabled",
            "last_scan_at": None,
            "api_calls_today": 0,
            "items": [],
            "boosted_markets": [],
        }
        if not news.enabled:
            logger.info("News catalyst scanner disabled by configuration")
            return
        if self.paper_trade_store is None:
            raise RuntimeError(
                "news catalyst scanning requires the persistent performance store"
            )
        dashboard_state.news_catalysts["api_calls_today"] = (
            self.paper_trade_store.news_api_calls_today()
        )
        if not self.config.mode.semantic_matching_enabled:
            raise RuntimeError(
                "news catalyst scanning requires semantic matching so tracked "
                "market embeddings are cached before each scan"
            )
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when news catalyst scanning is enabled"
            )
        if self._semantic_embedder is None:
            self._semantic_embedder = OpenAIEmbeddingClient(
                api_key=api_key,
                cache_path=self.config.mode.semantic_cache_path,
                model=self.config.mode.semantic_embedding_model,
                dimensions=self.config.mode.semantic_embedding_dimensions,
            )
        self._news_catalyst_service = NewsCatalystService(
            provider=OpenAINewsProvider(
                api_key=api_key,
                model=news.model,
            ),
            mapper=NewsCatalystMapper(
                embedder=self._semantic_embedder,
                similarity_threshold=news.relevance_similarity_threshold,
            ),
            store=self.paper_trade_store,
            lookback_hours=int(news.lookback_hours),
            max_items=news.max_news_items_per_scan,
            max_daily_api_calls=news.max_daily_api_calls,
        )
        logger.info(
            "News catalyst scanner configured | model=%s interval=%.0fs "
            "mode=%s daily_cap=%s",
            news.model,
            news.scan_interval_seconds,
            "priority_boost" if news.apply_priority_boost else "log_only",
            news.max_daily_api_calls,
        )

    def _tracked_news_markets(self) -> list[TrackedMarket]:
        markets: list[TrackedMarket] = []
        if self.data_feed:
            markets.extend(
                TrackedMarket(
                    platform="polymarket",
                    market_id=market.market_id,
                    question=polymarket_document(market).semantic_text,
                    volume=float(market.volume_24h),
                )
                for market in self.data_feed._markets.values()
                if market.question.strip()
            )
        markets.extend(
            TrackedMarket(
                platform="kalshi",
                market_id=market.ticker,
                question=kalshi_document(market).semantic_text,
                volume=float(market.volume),
            )
            for market in self._kalshi_markets
            if market.matching_text.strip()
        )
        return markets

    async def _news_catalyst_loop(self) -> None:
        """Scan independently from orderbook and arbitrage loops."""
        interval = self.config.news_catalyst.scan_interval_seconds
        while self._running and self._news_catalyst_service is not None:
            matching_status = dashboard_state.cross_platform.get(
                "matching_status", "idle"
            )
            if matching_status not in {"complete", "no_matches"}:
                logger.info(
                    "News catalyst scan waiting for semantic market cache | "
                    "matching_status=%s",
                    matching_status,
                )
                await asyncio.sleep(min(30.0, interval))
                continue
            next_scan_delay = interval
            try:
                result = await self._news_catalyst_service.run_once(
                    self._tracked_news_markets()
                )
                if result.status == "complete":
                    current_scores: dict[tuple[str, str], float] = {}
                    for match in result.matches:
                        key = (match.market_platform, match.market_id)
                        current_scores[key] = max(
                            match.relevance_score,
                            current_scores.get(key, 0.0),
                        )
                    self._news_market_scores = current_scores
                    self._news_market_scores_updated_at = datetime.now(timezone.utc)
                matches_by_item: dict[int, list[dict]] = {}
                for match in result.matches:
                    matches_by_item.setdefault(match.news_index, []).append(
                        {
                            "market_platform": match.market_platform,
                            "market_id": match.market_id,
                            "relevance_score": match.relevance_score,
                        }
                    )
                state_update = {
                    "status": (
                        "daily_cap_reached"
                        if result.status == "daily_cap_reached"
                        else (
                            "active"
                            if self.config.news_catalyst.apply_priority_boost
                            else "log_only"
                        )
                    ),
                    "last_scan_at": datetime.utcnow().isoformat(),
                    "api_calls_today": result.api_calls_today,
                }
                if result.status == "complete":
                    state_update["items"] = [
                        {
                            **item.model_dump(mode="json"),
                            "matches": matches_by_item.get(index, []),
                        }
                        for index, item in enumerate(result.items)
                    ]
                dashboard_state.news_catalysts.update(state_update)
                self._update_pair_priority()
                logger.info(
                    "News catalyst scan %s | verified_items=%s matches=%s "
                    "api_calls_today=%s priority_application=%s",
                    result.status,
                    len(result.items),
                    len(result.matches),
                    result.api_calls_today,
                    self.config.news_catalyst.apply_priority_boost,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                next_scan_delay = self._handle_news_catalyst_error(error)
            await asyncio.sleep(next_scan_delay)

    def _handle_news_catalyst_error(self, error: Exception) -> float:
        """Fail closed and select a bounded retry delay for one scan failure."""
        self._news_market_scores = {}
        self._news_market_scores_updated_at = None
        self._update_pair_priority()
        calls_today = (
            self.paper_trade_store.news_api_calls_today()
            if self.paper_trade_store is not None
            else 0
        )
        transient = is_transient_upstream_error(error)
        dashboard_state.news_catalysts.update(
            {
                "status": "retrying" if transient else "error",
                "api_calls_today": calls_today,
                "last_error": f"{type(error).__name__}: {error}"[:300],
            }
        )
        if transient:
            retry_delay = min(
                30.0,
                self.config.news_catalyst.scan_interval_seconds,
            )
            logger.warning(
                "Transient news catalyst failure; retrying in %.0fs | error=%s",
                retry_delay,
                type(error).__name__,
                exc_info=True,
            )
            return retry_delay
        logger.exception("News catalyst scan failed; skipping until the next interval")
        return self.config.news_catalyst.scan_interval_seconds

    def _update_pair_priority(self) -> None:
        if not self.data_feed:
            return
        if self._news_market_scores_updated_at is not None and datetime.now(
            timezone.utc
        ) - self._news_market_scores_updated_at > timedelta(
            hours=self.config.news_catalyst.lookback_hours
        ):
            self._news_market_scores = {}
            self._news_market_scores_updated_at = None
        if not self._matched_pairs:
            self.data_feed.set_priority_markets([])
            dashboard_state.news_catalysts["boosted_markets"] = []
            return
        poly_markets = {
            market.market_id: market for market in self.data_feed._markets.values()
        }
        kalshi_markets = {market.ticker: market for market in self._kalshi_markets}
        rankings = rank_pairs(
            [
                PairRankingInput(
                    polymarket_id=pair.polymarket_id,
                    kalshi_ticker=pair.kalshi_ticker,
                    similarity_score=pair.similarity_score,
                    polymarket_volume=float(
                        getattr(poly_markets.get(pair.polymarket_id), "volume_24h", 0)
                    ),
                    kalshi_volume=float(
                        getattr(kalshi_markets.get(pair.kalshi_ticker), "volume", 0)
                    ),
                )
                for pair in self._matched_pairs
            ],
            news_scores=self._news_market_scores,
            catalyst_boost_weight=self.config.news_catalyst.catalyst_boost_weight,
        )
        candidate_ids = [
            row.polymarket_id for row in rankings[: self.config.mode.hot_pair_limit]
        ]
        dashboard_state.news_catalysts["boosted_markets"] = [
            {
                "polymarket_id": row.polymarket_id,
                "kalshi_ticker": row.kalshi_ticker,
                "news_relevance_score": row.news_relevance_score,
                "rank_score": row.rank_score,
                "applied": self.config.news_catalyst.apply_priority_boost,
            }
            for row in rankings[: self.config.mode.hot_pair_limit]
            if row.news_relevance_score > 0
        ]
        # PairSnapshotSource now owns the latency-sensitive cross-venue reads.
        # Keeping the same markets in DataFeed's priority loop duplicates CLOB
        # traffic and was the source of stale queued observations under load.
        if getattr(self, "pair_snapshot_source", None) is not None:
            self.data_feed.set_priority_markets([])
        else:
            self.data_feed.set_priority_markets(
                candidate_ids
                if self.config.news_catalyst.apply_priority_boost
                else [pair.polymarket_id for pair in self._matched_pairs]
            )

    async def _start_production_runtime(self) -> None:
        """Own all live cross-venue resources for this process."""
        if not isinstance(self.client, PolymarketClient):
            raise RuntimeError(
                "live cross-platform execution requires the Polymarket Global client"
            )
        if self.kalshi_client is None or self.cross_platform_engine is None:
            raise RuntimeError("live cross-platform clients are not initialized")

        alert_sink = WebhookAlertSink(
            self.config.production.alert_webhook_url,
            bearer_token=self.config.production.alert_webhook_token,
            timeout_seconds=self.config.production.alert_timeout_seconds,
        )
        controls = PersistentOperatorControls(
            Path(self.config.production.operator_state_path),
            auth_token=self.config.production.operator_token,
            alert_sink=alert_sink,
        )
        try:
            journal = ExecutionJournal(
                Path(self.config.production.execution_journal_path)
            )
        except BaseException:
            controls.close()
            raise

        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={
                "polymarket": PolymarketVenueAdapter(self.client),
                "kalshi": KalshiVenueAdapter(self.kalshi_client),
            },
            controls=controls,
            min_net_edge=self.config.trading.min_edge,
            max_order_notional=self.config.risk.max_order_notional,
            economics_max_age=timedelta(
                seconds=self.config.production.economics_max_age_seconds
            ),
            max_order_attempts_per_minute=(
                self.config.risk.max_order_attempts_per_minute
            ),
            max_daily_order_attempts=self.config.risk.max_daily_order_attempts,
            max_strategy_exposure=(
                self.config.risk.strategy_exposure_limits["cross_platform_arb"]
            ),
            max_global_exposure=self.config.risk.max_global_exposure,
            max_position_per_market=self.config.risk.max_position_per_market,
            max_open_positions=self.config.risk.max_open_positions,
            market_whitelist=tuple(self.config.risk.whitelist),
            market_blacklist=tuple(self.config.risk.blacklist),
            opportunity_max_age=(
                self.cross_platform_engine.max_observation_age or timedelta(seconds=5)
            ),
            require_collateral=True,
            economics_provider=AuthoritativeEconomicsProvider(
                self.client, self.kalshi_client
            ),
            detector=self.cross_platform_engine,
            private_stream=KalshiPrivateStream(
                api_key_id=self.config.api.kalshi_api_key_id,
                private_key_path=self.config.api.kalshi_private_key_path,
            ),
        )
        try:
            await runtime.start()
        except BaseException:
            journal.close()
            controls.close()
            raise

        self.operator_controls = controls
        self.execution_journal = journal
        self.production_runtime = runtime
        configure_dashboard_runtime(
            store=self.paper_trade_store,
            timezone=self.config.monitoring.display_timezone,
            runtime=runtime,
            production_required=True,
            readiness_check=self._critical_dependencies_ready,
        )
        logger.warning(
            "Production runtime recovered and is HALTED pending authenticated operator arming"
        )
        logger.info(f"Open http://{self.dashboard_host}:{self.port} in your browser")

    async def _start_server(self) -> None:
        """Start the uvicorn server."""
        config = uvicorn.Config(
            app,
            host=self.dashboard_host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())

    def _on_market_update(self, market_id: str, market_state) -> None:
        """Handle market updates."""
        if not self._running:
            return

        # Check risk limits
        if not self.risk_manager.within_global_limits():
            return

        # Analyze for opportunities
        signals = self.arb_engine.analyze(market_state)

        now = time.monotonic()
        if (
            self.same_platform_detector is not None
            and self.data_feed is not None
            and now - self._last_combinatorial_scan >= 2.0
        ):
            self._last_combinatorial_scan = now
            self._run_combinatorial_scan(now=now)

        for signal in signals:
            # Add to dashboard
            if signal.opportunity:
                self.dashboard_integration.add_opportunity(
                    opportunity_type=signal.opportunity.opportunity_type.value,
                    market_id=signal.market_id,
                    edge=signal.opportunity.edge,
                    suggested_size=signal.opportunity.suggested_size,
                )

            self.dashboard_integration.add_signal(
                action=signal.action,
                market_id=signal.market_id,
            )

            # Submit to execution
            asyncio.create_task(self.execution_engine.submit_signal(signal))

    def _run_combinatorial_scan(self, *, now: float | None = None) -> None:
        if self.same_platform_detector is None or self.data_feed is None:
            return
        now = time.monotonic() if now is None else now
        opportunities = self.same_platform_detector.detect(
            self.data_feed.get_all_market_states()
        )
        self._combinatorial_scans += 1
        self._combinatorial_opportunities += len(opportunities)
        metrics = self.same_platform_detector.last_metrics
        dashboard_state.operational["bundle_arb"] = {
            "enabled": True,
            "scans": self._combinatorial_scans,
            "states": metrics.states,
            "negative_risk_states": metrics.negative_risk_states,
            "event_groups": metrics.event_groups,
            "eligible_groups": metrics.eligible_groups,
            "opportunities": self._combinatorial_opportunities,
            "last_scan_at": datetime.utcnow().isoformat(),
        }
        if now - self._last_combinatorial_status_log >= 60.0:
            self._last_combinatorial_status_log = now
            logger.info(
                "Bundle arb scan active | scans=%s states=%s "
                "negative_risk_states=%s event_groups=%s eligible_groups=%s "
                "opportunities=%s",
                self._combinatorial_scans,
                metrics.states,
                metrics.negative_risk_states,
                metrics.event_groups,
                metrics.eligible_groups,
                self._combinatorial_opportunities,
            )
        for opportunity in opportunities:
            opportunity_logger.log_combinatorial_opportunity(
                opportunity_id=opportunity.opportunity_id,
                event_id=opportunity.event_id,
                kind=opportunity.kind,
                edge=opportunity.net_edge,
                total_price=opportunity.total_price,
                legs=len(opportunity.legs),
                max_size=opportunity.max_size,
            )
            if self.dashboard_integration:
                self.dashboard_integration.add_opportunity(
                    opportunity_type=opportunity.kind,
                    market_id=opportunity.event_id,
                    edge=opportunity.net_edge,
                    suggested_size=opportunity.max_size,
                )

    async def _simulate_fills(self) -> None:
        """Simulate order fills in dry run mode."""
        import random

        while self._running:
            try:
                await asyncio.sleep(2.0)

                orders = self.execution_engine.get_open_orders()
                for order in orders:
                    if random.random() < self.config.mode.fill_probability:
                        trade = self.client.simulate_fill(order.order_id)
                        if trade:
                            self.execution_engine.handle_fill(trade)
                            self.dashboard_integration.add_trade(
                                side=trade.side.value,
                                price=trade.price,
                                size=trade.size,
                                market_id=trade.market_id,
                                is_simulated=trade.is_simulated,
                                simulation_label=trade.simulation_label,
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fill simulation error: {e}")

    async def _start_kalshi_monitoring(self) -> None:
        """Continuously refresh eligible markets and equivalent pair discovery."""
        if not self.kalshi_client:
            return

        logger.info("Starting Kalshi market monitoring...")
        dashboard_state.cross_platform["enabled"] = True
        dashboard_state.cross_platform["matching_status"] = "loading"

        first_cycle = True
        while self._running:
            dashboard_state.cross_platform["matching_status"] = (
                "loading" if first_cycle else "refreshing"
            )
            dashboard_state.cross_platform["eligible_market_filter"] = (
                "single_event_only"
            )
            logger.info("Fetching eligible single-event Kalshi markets...")

            def on_kalshi_progress(count):
                dashboard_state.cross_platform["kalshi_markets"] = count

            self._kalshi_markets = await self.kalshi_client.list_all_event_markets(
                status="open",
                max_events=2_000,
                max_markets=10_000,
                on_progress=on_kalshi_progress,
            )
            dashboard_state.cross_platform["kalshi_markets"] = len(self._kalshi_markets)
            logger.info(
                "Loaded %s eligible Kalshi markets (MVE excluded)",
                len(self._kalshi_markets),
            )

            if first_cycle:
                logger.info("Waiting for Polymarket markets...")
                for index in range(30):
                    await asyncio.sleep(1)
                    poly_count = len(self.data_feed._markets) if self.data_feed else 0
                    dashboard_state.cross_platform["polymarket_markets"] = poly_count
                    if poly_count >= 50:
                        logger.info(
                            "Got %s Polymarket markets - starting matching!",
                            poly_count,
                        )
                        break
                    if index % 5 == 0:
                        logger.info("Polymarket: %s markets loaded...", poly_count)

            if self.data_feed and self._kalshi_markets:
                polymarket_markets = list(self.data_feed._markets.values())
                dashboard_state.cross_platform["polymarket_markets"] = len(
                    polymarket_markets
                )
                logger.info(
                    "Starting matching: %s Polymarket x %s eligible Kalshi",
                    len(polymarket_markets),
                    len(self._kalshi_markets),
                )
                await self._run_matching_background(polymarket_markets)
                self._discovery_supervisor.mark_healthy()

            first_cycle = False
            refresh_seconds = self.config.mode.cross_platform_refresh_seconds
            dashboard_state.cross_platform["next_discovery_refresh_seconds"] = (
                refresh_seconds
            )
            await asyncio.sleep(refresh_seconds)

    async def _refresh_event_week_discovery(self, polymarket_markets: list) -> list:
        """Refresh the event clock and run its independent verifier lane."""
        if (
            not self.config.event_week.enabled
            or self.event_calendar is None
            or self.event_contract_discovery is None
        ):
            self._event_pair_links = []
            return []
        now = datetime.now(timezone.utc)
        refresh_due = (
            self._event_calendar_snapshot is None
            or self._event_calendar_refreshed_at is None
            or (now - self._event_calendar_refreshed_at).total_seconds()
            >= self.config.event_week.calendar_refresh_seconds
        )
        if refresh_due:
            snapshot = await self.event_calendar.refresh(
                start=now
                - timedelta(hours=self.config.event_week.cooldown_after_hours),
                end=now + timedelta(days=self.config.event_week.lookahead_days),
            )
            limited_events = snapshot.events[
                : self.config.event_week.max_events_per_refresh
            ]
            snapshot = replace(snapshot, events=limited_events)
            self._event_calendar_snapshot = snapshot
            self._event_calendar_refreshed_at = now
            if self.paper_trade_store is not None:
                self.paper_trade_store.record_event_calendar_snapshot(snapshot)
            dashboard_state.event_week.update(
                {
                    "status": snapshot.status,
                    "last_calendar_refresh_at": snapshot.generated_at.isoformat(),
                    "calendar_sources": [
                        {
                            "source_id": source.source_id,
                            "source_url": source.source_url,
                            "status": source.status,
                            "last_success_at": (
                                source.last_success_at.isoformat()
                                if source.last_success_at
                                else None
                            ),
                            "event_count": source.event_count,
                            "error": source.error,
                        }
                        for source in snapshot.sources
                    ],
                    "upcoming_events": [
                        {
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "title": event.title,
                            "scheduled_at": event.scheduled_at.isoformat(),
                            "reference_period": event.reference_period,
                            "status": event.status,
                            "source_url": event.source_url,
                            "cadence_eligible": event in snapshot.fresh_events,
                        }
                        for event in snapshot.events
                    ],
                }
            )
        snapshot = self._event_calendar_snapshot
        if snapshot is None:
            self._event_pair_links = []
            return []
        result = await self.event_contract_discovery.discover(
            events=snapshot.fresh_events,
            polymarket_markets=polymarket_markets,
            kalshi_markets=self._kalshi_markets,
        )
        self._event_pair_links = list(result.links)
        if self.paper_trade_store is not None:
            self.paper_trade_store.record_event_contract_discovery(
                result, observed_at=now
            )
        dashboard_state.event_week.update(
            {
                "coverage": [
                    {
                        "event_id": row.event_id,
                        "event_type": row.event_type,
                        "status": row.status,
                        "polymarket_candidates": row.polymarket_candidates,
                        "kalshi_candidates": row.kalshi_candidates,
                        "verified_pairs": row.verified_pairs,
                    }
                    for row in result.coverage
                ],
                "verified_event_pairs": len(result.pairs),
            }
        )
        return list(result.pairs)

    def _apply_event_lane_schedule(self) -> None:
        """Apply current event states to the existing pair-monitoring seam."""
        if self.pair_monitor is None:
            return
        set_overrides = getattr(self.pair_monitor, "set_schedule_overrides", None)
        if self.event_lane_scheduler is None or not self._event_pair_links:
            if set_overrides is not None:
                set_overrides({})
            dashboard_state.event_week["active_lanes"] = []
            return
        if set_overrides is None:
            raise RuntimeError(
                "event-week monitoring requires schedule override support"
            )
        snapshot = self.event_lane_scheduler.schedule(self._event_pair_links)
        set_overrides(
            {
                schedule.pair_id: PairScheduleOverride(
                    event_id=schedule.event_id,
                    lane_state=schedule.state,
                    interval_seconds=schedule.interval_seconds,
                )
                for schedule in snapshot.pairs
            }
        )
        if (
            self.paper_trade_store is not None
            and self.paper_trade_store.active_run() is not None
        ):
            self.paper_trade_store.record_event_lane_snapshot(snapshot)
        dashboard_state.event_week["active_lanes"] = [
            {
                "pair_id": schedule.pair_id,
                "event_id": schedule.event_id,
                "event_type": schedule.event_type,
                "state": schedule.state,
                "scheduled_at": schedule.scheduled_at.isoformat(),
                "seconds_to_event": schedule.seconds_to_event,
                "interval_seconds": schedule.interval_seconds,
            }
            for schedule in snapshot.pairs
        ]

    async def _run_matching_background(self, polymarket_markets: list) -> None:
        """Match one venue snapshot while yielding to live dashboard work."""
        try:
            dashboard_state.cross_platform["matching_status"] = "matching"
            dashboard_state.cross_platform["matching_checked"] = 0
            dashboard_state.cross_platform["matching_total"] = 0
            dashboard_state.cross_platform["matching_progress"] = 0
            dashboard_state.cross_platform["matched_pairs"] = 0
            dashboard_state.cross_platform["matched_pairs_data"] = []
            clear_cached_pairs = getattr(
                self.market_matcher, "clear_cached_pairs", None
            )
            if clear_cached_pairs:
                clear_cached_pairs()

            def on_progress(checked, total, matches_found):
                dashboard_state.cross_platform["matching_checked"] = checked
                dashboard_state.cross_platform["matching_total"] = total
                dashboard_state.cross_platform["matching_progress"] = (
                    int(checked / total * 100) if total > 0 else 0
                )
                dashboard_state.cross_platform["matched_pairs"] = matches_found

                cached_pairs = self.market_matcher.get_cached_pairs()
                if cached_pairs:
                    dashboard_state.cross_platform["matched_pairs_data"] = [
                        self._matched_pair_dashboard_row(pair)
                        for pair in cached_pairs[-50:]
                    ]

            rules_verified_pairs = await self.market_matcher.find_matches(
                polymarket_markets,
                self._kalshi_markets,
                on_progress=on_progress,
            )
            event_verified_pairs = []
            try:
                event_verified_pairs = await self._refresh_event_week_discovery(
                    polymarket_markets
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event_pair_links = []
                dashboard_state.event_week.update(
                    {
                        "status": "error",
                        "last_error": f"{type(exc).__name__}: {exc}"[:300],
                        "verified_event_pairs": 0,
                        "active_lanes": [],
                    }
                )
                logger.exception(
                    "Scheduled event-week discovery failed; broad discovery continues"
                )
            merged_verified_pairs = {
                pair.pair_id: pair
                for pair in [*rules_verified_pairs, *event_verified_pairs]
            }
            self._matched_pairs, preflight_evidence = (
                await self._preflight_verified_pairs(
                    list(merged_verified_pairs.values())
                )
            )
            admitted_pair_ids = {pair.pair_id for pair in self._matched_pairs}
            self._event_pair_links = [
                link
                for link in self._event_pair_links
                if link.pair_id in admitted_pair_ids
            ]

            dashboard_state.cross_platform["matching_progress"] = 100
            dashboard_state.cross_platform["matched_pairs"] = len(self._matched_pairs)
            dashboard_state.cross_platform["rules_equivalent_pairs"] = len(
                merged_verified_pairs
            )
            dashboard_state.cross_platform["broad_rules_equivalent_pairs"] = len(
                rules_verified_pairs
            )
            dashboard_state.cross_platform["event_rules_equivalent_pairs"] = len(
                event_verified_pairs
            )
            dashboard_state.cross_platform["preflight_usable_pairs"] = len(
                [
                    item
                    for item in preflight_evidence.values()
                    if item["result"] == "usable"
                ]
            )
            preflight_usable_pairs = dashboard_state.cross_platform[
                "preflight_usable_pairs"
            ]
            dashboard_state.cross_platform["preflight_rejections"] = dict(
                Counter(
                    item["result"]
                    for item in preflight_evidence.values()
                    if item["result"] != "usable"
                )
            )
            pipeline_metrics = getattr(
                self.market_matcher, "last_pipeline_metrics", None
            )
            if pipeline_metrics is not None:
                dashboard_state.cross_platform["semantic_metrics"] = {
                    key: value for key, value in vars(pipeline_metrics).items()
                }
            dashboard_state.cross_platform["last_matching_completed_at"] = (
                datetime.utcnow().isoformat()
            )

            # Prepare matched pairs data for dashboard display
            dashboard_state.cross_platform["matched_pairs_data"] = [
                self._matched_pair_dashboard_row(pair)
                for pair in self._matched_pairs[:50]
            ]
            all_review_candidates = self.market_matcher.get_review_candidates(
                limit=None
            )
            review_candidates = all_review_candidates[:100]
            if self.paper_trade_store:
                complete_pairs = {
                    pair.pair_id: pair
                    for pair in [*rules_verified_pairs, *all_review_candidates]
                }
                discovery_candidates = [
                    dict(candidate)
                    for candidate in getattr(
                        self.market_matcher, "last_discovery_candidates", []
                    )
                ]
                for candidate in discovery_candidates:
                    preflight = preflight_evidence.get(candidate["pair_id"])
                    if preflight:
                        candidate["preflight_result"] = preflight["result"]
                        candidate["executable_capacity"] = preflight[
                            "executable_capacity"
                        ]
                shadow_preflight = {}
                for cohort, flag in (
                    ("baseline", "selected_by_baseline"),
                    ("stratified", "selected_by_stratified"),
                ):
                    cohort_rows = [
                        candidate
                        for candidate in discovery_candidates
                        if candidate["selected_for_verification"] and candidate[flag]
                    ]
                    shadow_preflight[cohort] = {
                        "verified_sample": len(cohort_rows),
                        "usable_books": sum(
                            candidate["preflight_result"] == "usable"
                            for candidate in cohort_rows
                        ),
                    }
                if pipeline_metrics is not None:
                    pipeline_metrics.allocation["shadow_preflight"] = shadow_preflight
                cycle_id = self.paper_trade_store.record_semantic_discovery_cycle(
                    pairs=[
                        {
                            "pair_id": pair.pair_id,
                            "polymarket_id": pair.polymarket_execution_id,
                            "kalshi_ticker": pair.kalshi_ticker,
                            "polymarket_question": pair.polymarket_question,
                            "kalshi_title": pair.kalshi_title,
                            "relation": pair.semantic_relation,
                            "retrieval_score": pair.similarity_score,
                            "verification_confidence": (pair.verification_confidence),
                            "verification_reasons": pair.verification_reasons,
                            "approval_status": (
                                "auto_approved"
                                if pair.auto_approved
                                else "manual_review"
                            ),
                        }
                        for pair in complete_pairs.values()
                    ],
                    metrics=(
                        {
                            **vars(pipeline_metrics),
                            "rules_equivalent_pairs": len(merged_verified_pairs),
                            "broad_rules_equivalent_pairs": len(rules_verified_pairs),
                            "event_rules_equivalent_pairs": len(event_verified_pairs),
                            "preflight_usable_pairs": preflight_usable_pairs,
                            "preflight_tracked_pairs": len(self._matched_pairs),
                            "preflight_rejections": dashboard_state.cross_platform[
                                "preflight_rejections"
                            ],
                        }
                        if pipeline_metrics is not None
                        else {
                            "verified_candidates": len(self._matched_pairs),
                            "review_candidates": len(all_review_candidates),
                        }
                    ),
                    candidates=discovery_candidates,
                )
                dashboard_state.cross_platform["discovery_cycle_id"] = cycle_id
                dashboard_state.cross_platform["durable_review_count"] = len(
                    complete_pairs
                )
            dashboard_state.cross_platform["review_candidate_count"] = len(
                all_review_candidates
            )
            dashboard_state.cross_platform["review_candidates"] = [
                {
                    "polymarket_id": pair.polymarket_execution_id,
                    "kalshi_ticker": pair.kalshi_ticker,
                    "poly_question": pair.polymarket_question,
                    "kalshi_title": pair.kalshi_title,
                    "similarity": pair.similarity_score,
                    "category": pair.category,
                    "trade_eligible": False,
                }
                for pair in review_candidates
            ]

            self._update_pair_priority()

            if not self._matched_pairs:
                dashboard_state.cross_platform["matching_status"] = "no_matches"
                logger.info("Matching complete: no equivalent single-event pairs found")
                self._record_cross_platform_decision(
                    outcome=DecisionOutcome.WAIT,
                    reason_code="no_equivalent_pairs",
                    explanation=(
                        "No equivalent Polymarket/Kalshi single-event pairs were "
                        "found; discovery will refresh automatically."
                    ),
                    evidence={
                        "matched_pairs": 0,
                        "review_candidates": len(all_review_candidates),
                    },
                )
                return

            dashboard_state.cross_platform["matching_status"] = "complete"
            logger.info(f"Matching complete! Found {len(self._matched_pairs)} pairs")
            self._record_cross_platform_decision(
                outcome=DecisionOutcome.WAIT,
                reason_code="matched_pairs_ready",
                explanation=(
                    f"Matched {len(self._matched_pairs)} Polymarket/Kalshi pairs; "
                    "price scanning is active."
                ),
                evidence={"matched_pairs": len(self._matched_pairs)},
            )

            if self._xplat_scan_task is None or self._xplat_scan_task.done():
                self._xplat_scan_task = asyncio.create_task(
                    self._scanner_supervisor.run(
                        self._scan_cross_platform_pairs,
                        should_run=lambda: self._running,
                    )
                )
                self._xplat_scan_task.add_done_callback(self._critical_task_done)
        except Exception as e:
            logger.exception("Matching error: %s", e)
            dashboard_state.cross_platform["matching_status"] = "error"
            raise

    async def _preflight_verified_pairs(self, pairs: list) -> tuple[list, dict]:
        """Fetch one fresh paired book and admit only executable verified pairs."""
        if not pairs:
            return [], {}
        if not self.config.mode.semantic_book_preflight_enabled:
            return list(pairs), {
                pair.pair_id: {"result": "disabled", "executable_capacity": 0.0}
                for pair in pairs
            }
        if not self.pair_snapshot_source:
            raise RuntimeError(
                "semantic book preflight requires a pair snapshot source"
            )

        self._preflight_snapshots = {}
        semaphore = asyncio.Semaphore(8)

        async def inspect(pair):
            async with semaphore:
                try:
                    snapshot = await self.pair_snapshot_source.fetch(pair)
                except PairSnapshotError as exc:
                    transient = exc.reason_code in {
                        "paired_snapshot_timeout",
                        "paired_snapshot_upstream_unavailable",
                    } or any(
                        exc.reason_code.startswith(prefix)
                        for prefix in ("stale_", "missing_", "empty_")
                    )
                    return (
                        pair,
                        None,
                        0.0,
                        "retry_pending" if transient else exc.reason_code,
                    )
                except Exception as exc:
                    logger.warning(
                        "Pair preflight rejected unexpected %s for %s",
                        type(exc).__name__,
                        pair.pair_id,
                    )
                    return pair, None, 0.0, "preflight_unexpected_error"
                raw_capacity = executable_top_capacity(snapshot)
                capacity = min(
                    raw_capacity
                    * self.config.trading.cross_platform_max_liquidity_fraction,
                    self.config.trading.cross_platform_max_order_size,
                )
                minimum = self.config.trading.cross_platform_min_executable_size
                result = (
                    "usable"
                    if capacity >= minimum and capacity > 0
                    else "insufficient_executable_liquidity"
                )
                return pair, snapshot, capacity, result

        observations = await asyncio.gather(*(inspect(pair) for pair in pairs))
        family_totals = Counter(pair.event_family or "unclassified" for pair in pairs)
        passed: list = []
        evidence: dict[str, dict] = {}
        for pair, snapshot, capacity, result in observations:
            pair.executable_capacity = capacity
            pair.discovery_priority = discovery_priority_score(
                family_size=family_totals[pair.event_family or "unclassified"],
                event_date_key=pair.event_date_key,
                executable_capacity=capacity,
                max_capacity=max(
                    1.0, self.config.trading.cross_platform_max_order_size
                ),
            )
            evidence[pair.pair_id] = {
                "result": result,
                "executable_capacity": capacity,
                "event_family": pair.event_family,
            }
            if result in {"usable", "retry_pending"}:
                passed.append(pair)
            if result == "usable" and snapshot is not None:
                self._preflight_snapshots[pair.pair_id] = snapshot
        passed.sort(
            key=lambda pair: (
                -pair.discovery_priority,
                -pair.similarity_score,
                pair.pair_id,
            )
        )
        return passed, evidence

    def _matched_pair_dashboard_row(self, pair) -> dict:
        """Build a monitoring row from the latest live venue books.

        Missing books remain null. A null is materially different from a
        genuine zero-cent bid and must stay distinguishable in the dashboard.
        """
        polymarket_book = self._polymarket_orderbooks.get(pair.polymarket_id)
        if polymarket_book is None and self.data_feed:
            polymarket_book = self.data_feed.get_order_book(pair.polymarket_id)
        kalshi_book = self._kalshi_orderbooks.get(pair.kalshi_ticker)
        return {
            "polymarket_id": pair.polymarket_id,
            "kalshi_ticker": pair.kalshi_ticker,
            "poly_question": pair.polymarket_question,
            "kalshi_title": pair.kalshi_title,
            "similarity": pair.similarity_score,
            "category": pair.category,
            "poly_yes": (polymarket_book.best_bid_yes if polymarket_book else None),
            "poly_no": polymarket_book.best_bid_no if polymarket_book else None,
            "kalshi_yes": kalshi_book.best_bid_yes if kalshi_book else None,
            "kalshi_no": kalshi_book.best_bid_no if kalshi_book else None,
        }

    def _publish_matched_pairs_dashboard(self) -> None:
        dashboard_state.cross_platform["matched_pairs_data"] = [
            self._matched_pair_dashboard_row(pair) for pair in self._matched_pairs[:50]
        ]

    def _on_cross_platform_task_retry(self, event: RestartEvent) -> None:
        """Expose transient recovery without marking the paper run failed."""
        restarts = dashboard_state.cross_platform.setdefault("task_restarts", {})
        restarts[event.task_name] = int(restarts.get(event.task_name, 0)) + 1
        dashboard_state.cross_platform["matching_status"] = "recovering"
        dashboard_state.cross_platform["last_transient_error"] = {
            "task": event.task_name,
            "error_type": event.error_type,
            "message": event.error_message,
            "attempt": event.attempt,
            "backoff_seconds": event.delay_seconds,
            "at": datetime.utcnow().isoformat(),
        }
        logger.warning(
            "Transient %s failure (%s); restart attempt %s in %.1fs: %s",
            event.task_name,
            event.error_type,
            event.attempt,
            event.delay_seconds,
            event.error_message,
        )

    def _critical_dependencies_ready(self) -> bool:
        """Report whether the configured cross-platform discovery path is alive."""
        if not self.cross_platform_discovery_enabled:
            return True
        return bool(
            self._kalshi_monitor_task
            and not self._kalshi_monitor_task.done()
            and dashboard_state.cross_platform.get("matching_status") == "complete"
            and self._matched_pairs
            and self._xplat_scan_task
            and not self._xplat_scan_task.done()
            and self._snapshot_scanner_is_fresh()
        )

    def _snapshot_scanner_is_fresh(self) -> bool:
        if dashboard_state.cross_platform.get("scan_status") != "scanning":
            return False
        raw_timestamp = dashboard_state.cross_platform.get("last_fresh_snapshot_at")
        if not isinstance(raw_timestamp, str) or not raw_timestamp:
            return False
        try:
            observed_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age_seconds = (
            datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)
        ).total_seconds()
        freshness_window = max(
            60.0,
            float(self.config.mode.cold_pair_scan_seconds) * 3,
        )
        return 0 <= age_seconds <= freshness_window

    def _critical_task_done(self, task: asyncio.Task) -> None:
        if not self._running or task.cancelled():
            return
        error = task.exception()
        reason = (
            f"critical cross-platform task failed: {type(error).__name__}"
            if error
            else "critical cross-platform task stopped unexpectedly"
        )
        logger.critical(reason)
        self._run_failed = True
        self._failure_event.set()
        dashboard_state.cross_platform["matching_status"] = "error"
        if self.production_runtime:
            self._critical_failure_task = asyncio.create_task(
                self._halt_after_critical_failure(reason)
            )

    async def _halt_after_critical_failure(self, reason: str) -> None:
        try:
            await self.production_runtime.panic(
                self.config.production.operator_token,
                reason=reason,
            )
        except Exception:
            logger.exception("Failed to deliver critical-task halt alert")

    def _record_cross_platform_decision(
        self,
        *,
        outcome: DecisionOutcome,
        reason_code: str,
        explanation: str,
        market_pair=None,
        evidence: dict | None = None,
        orders: list | None = None,
    ) -> None:
        """Record a cross-platform scanner decision."""
        if not self.decision_journal:
            return
        self.decision_journal.add_decision(
            decision_id=f"xplat_{datetime.utcnow().timestamp()}",
            strategy="cross_platform",
            outcome=outcome,
            reason_code=reason_code,
            explanation=explanation,
            market_id=market_pair.polymarket_id if market_pair else "",
            platform="polymarket+kalshi",
            market_question=(
                f"{market_pair.polymarket_question} / {market_pair.kalshi_title}"
                if market_pair
                else ""
            ),
            category=market_pair.category if market_pair else "",
            evidence=evidence or {},
            orders=orders or [],
            related_id=market_pair.pair_id if market_pair else None,
        )

    async def evaluate_cross_platform_pair(self, pair, polymarket_book, kalshi_book):
        """Route one live pair through the sole mutation-owning runtime."""
        if self.cross_platform_engine is None:
            raise RuntimeError("cross-platform detector is not initialized")
        if self.config.is_live and self.config.mode.cross_platform_execution_enabled:
            if self.production_runtime is None:
                raise RuntimeError("production runtime ownership is missing")
            evaluation = await self.production_runtime.evaluate_pair(
                pair, polymarket_book, kalshi_book
            )
            return evaluation.opportunity, evaluation
        economics = None
        if self.config.mode.data_mode == "real":
            if self.economics_provider is None:
                raise EconomicsUnavailableError(
                    "authoritative economics provider is unavailable"
                )
            economics = await self.economics_provider.quote_pair(pair)
        opportunity = self.cross_platform_engine.check_arbitrage(
            pair,
            polymarket_book,
            kalshi_book,
            economics=economics,
        )
        if opportunity is not None and self.paper_locked_arb is not None:
            paper_trade = self.paper_locked_arb.observe(opportunity)
            dashboard_state.cross_platform["paper_performance"] = (
                self.paper_locked_arb.summary()
            )
            if paper_trade is not None:
                logger.info(
                    "SHADOW LOCKED TRADE: %s contracts on %s | capital=$%.2f | projected locked pnl=$%.2f",
                    paper_trade.contracts,
                    paper_trade.pair_id,
                    paper_trade.committed_capital,
                    paper_trade.projected_locked_pnl,
                )
        return opportunity, None

    async def _scan_cross_platform_pairs(self) -> None:
        """Continuously evaluate matched Polymarket/Kalshi pairs for real cross-platform arbitrage."""
        if (
            not self.cross_platform_engine
            or not self.kalshi_client
            or not self.pair_snapshot_source
        ):
            return

        logger.info("Starting live cross-platform price scanner...")
        dashboard_state.cross_platform["scan_status"] = "scanning"
        scan_count = 0
        orderbooks_fetched = 0

        while self._running:
            started = datetime.utcnow()
            evaluation_counts: Counter[str] = Counter()
            evaluation_rows: list[dict] = []
            pending_snapshots: dict[asyncio.Task, object] = {}
            unexpected_snapshot_failure = False
            try:
                self._apply_event_lane_schedule()
                due_pairs = (
                    self.pair_monitor.due_pairs(list(self._matched_pairs))
                    if self.pair_monitor
                    else []
                )
                dashboard_state.cross_platform["hot_pairs_due"] = sum(
                    1 for item in due_pairs if item.tier == "hot"
                )
                dashboard_state.cross_platform["cold_pairs_due"] = sum(
                    1 for item in due_pairs if item.tier == "cold"
                )
                evaluation_counts["pair_due"] += len(due_pairs)
                dashboard_state.cross_platform["kalshi_rest_metrics"] = (
                    self.kalshi_client.request_metrics
                )
                dashboard_state.cross_platform["kalshi_connection_metrics"] = getattr(
                    self.kalshi_client, "connection_metrics", {}
                )
                if self.client:
                    dashboard_state.cross_platform["polymarket_rest_metrics"] = (
                        self.client.request_metrics
                    )
                    dashboard_state.cross_platform["polymarket_orderbook_metrics"] = (
                        self.client.orderbook_metrics
                    )
                    dashboard_state.cross_platform["polymarket_connection_metrics"] = (
                        getattr(self.client, "connection_metrics", {})
                    )
                if not due_pairs:
                    await asyncio.sleep(0.25)
                    continue
                dashboard_state.cross_platform["last_scan_attempt_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                async def fetch_due_snapshot(due_pair):
                    nonlocal unexpected_snapshot_failure
                    preflight = self._preflight_snapshots.pop(
                        due_pair.pair.pair_id, None
                    )
                    if preflight is not None:
                        try:
                            require_pair_snapshot_fresh(preflight)
                            return preflight
                        except PairSnapshotError:
                            pass
                    try:
                        return await self.pair_snapshot_source.fetch(due_pair.pair)
                    except PairSnapshotError as exc:
                        return exc
                    except Exception as exc:
                        unexpected_snapshot_failure = True
                        logger.warning(
                            "Pair snapshot isolated unexpected %s for %s",
                            type(exc).__name__,
                            due_pair.pair.pair_id,
                        )
                        dashboard_state.cross_platform["scan_status"] = "degraded"
                        dashboard_state.cross_platform["degraded_reason"] = (
                            "paired_snapshot_unexpected_error"
                        )
                        dashboard_state.cross_platform[
                            "last_pair_snapshot_unexpected_error"
                        ] = {
                            "error_type": type(exc).__name__,
                            "pair_id": due_pair.pair.pair_id,
                        }
                        return PairSnapshotError(
                            "paired_snapshot_unexpected_error",
                            evidence={"error_type": type(exc).__name__},
                        )

                due_iterator = iter(due_pairs)

                def schedule_snapshot() -> bool:
                    try:
                        scheduled_pair = next(due_iterator)
                    except StopIteration:
                        return False
                    task = asyncio.create_task(fetch_due_snapshot(scheduled_pair))
                    pending_snapshots[task] = scheduled_pair
                    return True

                for _ in range(min(8, len(due_pairs))):
                    schedule_snapshot()

                while pending_snapshots and self._running:
                    completed, _ = await asyncio.wait(
                        pending_snapshots,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    completed_task = completed.pop()
                    due_pair = pending_snapshots.pop(completed_task)
                    snapshot_result = completed_task.result()
                    pair = due_pair.pair
                    if not self._running:
                        break
                    if not isinstance(snapshot_result, PairSnapshotError):
                        try:
                            require_pair_snapshot_fresh(snapshot_result)
                        except PairSnapshotError:
                            snapshot_result = await fetch_due_snapshot(due_pair)
                    if not self._running:
                        break
                    schedule_snapshot()

                    if isinstance(snapshot_result, PairSnapshotError):
                        exc = snapshot_result
                        evaluation_counts[exc.reason_code] += 1
                        self._persist_event_operational_failure(
                            due_pair, exc.reason_code
                        )
                        self._record_cross_platform_decision(
                            outcome=DecisionOutcome.SKIP,
                            reason_code=exc.reason_code,
                            explanation=(
                                "Skipped cross-platform check because a fresh paired "
                                "market snapshot was unavailable."
                            ),
                            market_pair=pair,
                            evidence={
                                "similarity": pair.similarity_score,
                                **exc.evidence,
                            },
                        )
                        if self.pair_monitor:
                            self.pair_monitor.mark_evaluated(
                                pair.pair_id, observed_net_edge=0.0
                            )
                        continue
                    snapshot = snapshot_result
                    poly_ob = snapshot.polymarket_book
                    kalshi_ob = snapshot.kalshi_book
                    evaluation_counts["paired_snapshot_fresh"] += 1
                    scan_count += 1
                    orderbooks_fetched += 1
                    self._polymarket_orderbooks[pair.polymarket_id] = poly_ob
                    self._kalshi_orderbooks[pair.kalshi_ticker] = kalshi_ob
                    self._publish_matched_pairs_dashboard()
                    dashboard_state.cross_platform["pairs_scanned"] = scan_count
                    dashboard_state.cross_platform["kalshi_orderbooks"] = (
                        orderbooks_fetched
                    )
                    dashboard_state.cross_platform["last_scan_at"] = (
                        datetime.utcnow().isoformat()
                    )
                    dashboard_state.cross_platform["last_fresh_snapshot_at"] = (
                        datetime.now(timezone.utc).isoformat()
                    )

                    try:
                        evaluation_started = time.monotonic()
                        opportunity, evaluation = (
                            await self.evaluate_cross_platform_pair(
                                pair, poly_ob, kalshi_ob
                            )
                        )
                    except EconomicsUnavailableError as exc:
                        evaluation_counts["economics_unavailable"] += 1
                        self._persist_event_operational_failure(
                            due_pair, "economics_unavailable"
                        )
                        self._record_cross_platform_decision(
                            outcome=DecisionOutcome.SKIP,
                            reason_code="economics_unavailable",
                            explanation=(
                                "No cross-platform trade: authoritative pair "
                                "economics were unavailable."
                            ),
                            market_pair=pair,
                            evidence={"error": str(exc)},
                        )
                        if self.pair_monitor:
                            self.pair_monitor.mark_evaluated(
                                pair.pair_id, observed_net_edge=0.0
                            )
                        continue
                    except RuntimeNotReadyError as exc:
                        evaluation_counts["production_runtime_not_ready"] += 1
                        self._persist_event_operational_failure(
                            due_pair, "production_runtime_not_ready"
                        )
                        dashboard_state.cross_platform["scan_status"] = (
                            "operator_halted"
                        )
                        self._record_cross_platform_decision(
                            outcome=DecisionOutcome.SKIP,
                            reason_code="production_runtime_not_ready",
                            explanation=str(exc),
                            market_pair=pair,
                        )
                        await asyncio.sleep(2.0)
                        continue
                    direction_evaluations = (
                        self.cross_platform_engine.get_last_direction_evaluations(
                            pair.pair_id
                        )
                    )
                    observed_net_edge = max(
                        (item.executable_net_edge for item in direction_evaluations),
                        default=0.0,
                    )
                    if self.pair_monitor:
                        self.pair_monitor.mark_evaluated(
                            pair.pair_id,
                            observed_net_edge=observed_net_edge,
                            opportunity=opportunity is not None,
                        )
                    dashboard_state.cross_platform["last_evaluation_latency_ms"] = (
                        round((time.monotonic() - evaluation_started) * 1000, 2)
                    )
                    dashboard_state.cross_platform["last_evaluation_tier"] = (
                        due_pair.tier
                    )
                    evidence = {
                        "similarity": pair.similarity_score,
                        "polymarket_yes_bid": poly_ob.best_bid_yes,
                        "polymarket_yes_ask": poly_ob.best_ask_yes,
                        "polymarket_no_bid": poly_ob.best_bid_no,
                        "polymarket_no_ask": poly_ob.best_ask_no,
                        "kalshi_yes_bid": kalshi_ob.best_bid_yes,
                        "kalshi_yes_ask": kalshi_ob.best_ask_yes,
                        "kalshi_no_bid": kalshi_ob.best_bid_no,
                        "kalshi_no_ask": kalshi_ob.best_ask_no,
                        "required_net_edge": self.cross_platform_engine.min_edge,
                    }
                    if due_pair.scheduled_event_id:
                        evidence.update(
                            {
                                "scheduled_event_id": due_pair.scheduled_event_id,
                                "event_lane_state": due_pair.event_lane_state,
                            }
                        )
                    if direction_evaluations:
                        strongest_direction = max(
                            direction_evaluations,
                            key=lambda item: item.executable_net_edge,
                        )
                        evidence.update(
                            {
                                "best_gross_edge": strongest_direction.gross_edge,
                                "best_fee_cost": strongest_direction.fee_cost,
                                "best_slippage_reserve": (
                                    strongest_direction.slippage_reserve
                                ),
                                "best_net_edge": strongest_direction.net_edge,
                                "best_executable_net_edge": (
                                    strongest_direction.executable_net_edge
                                ),
                                "best_direction": (
                                    f"{strongest_direction.buy_platform}_to_"
                                    f"{strongest_direction.sell_platform}_"
                                    f"{strongest_direction.token.lower()}"
                                ),
                            }
                        )
                    if self.paper_trade_store and direction_evaluations:
                        observed_at = datetime.now(timezone.utc)
                        poly_age = max(
                            0.0,
                            (
                                observed_at - poly_ob.timestamp.astimezone(timezone.utc)
                            ).total_seconds(),
                        )
                        kalshi_age = max(
                            0.0,
                            (
                                observed_at
                                - kalshi_ob.timestamp.astimezone(timezone.utc)
                            ).total_seconds(),
                        )
                        evaluation_rows.extend(
                            {
                                "pair_id": item.pair_id,
                                "polymarket_id": pair.polymarket_execution_id,
                                "kalshi_ticker": pair.kalshi_ticker,
                                "polymarket_question": pair.polymarket_question,
                                "kalshi_title": pair.kalshi_title,
                                "token": item.token,
                                "buy_platform": item.buy_platform,
                                "sell_platform": item.sell_platform,
                                "buy_price": item.buy_price,
                                "sell_price": item.sell_price,
                                "buy_liquidity": item.buy_liquidity,
                                "sell_liquidity": item.sell_liquidity,
                                "polymarket_yes_bid": poly_ob.best_bid_yes,
                                "polymarket_yes_ask": poly_ob.best_ask_yes,
                                "polymarket_no_bid": poly_ob.best_bid_no,
                                "polymarket_no_ask": poly_ob.best_ask_no,
                                "polymarket_yes_bid_size": poly_ob.yes.bids.best_size,
                                "polymarket_yes_ask_size": poly_ob.yes.asks.best_size,
                                "polymarket_no_bid_size": poly_ob.no.bids.best_size,
                                "polymarket_no_ask_size": poly_ob.no.asks.best_size,
                                "kalshi_yes_bid": kalshi_ob.best_bid_yes,
                                "kalshi_yes_ask": kalshi_ob.best_ask_yes,
                                "kalshi_no_bid": kalshi_ob.best_bid_no,
                                "kalshi_no_ask": kalshi_ob.best_ask_no,
                                "kalshi_yes_bid_size": kalshi_ob.yes.bids.best_size,
                                "kalshi_yes_ask_size": kalshi_ob.yes.asks.best_size,
                                "kalshi_no_bid_size": kalshi_ob.no.bids.best_size,
                                "kalshi_no_ask_size": kalshi_ob.no.asks.best_size,
                                "polymarket_age_seconds": poly_age,
                                "kalshi_age_seconds": kalshi_age,
                                "gross_edge": item.gross_edge,
                                "fee_cost": item.fee_cost,
                                "slippage_reserve": item.slippage_reserve,
                                "net_edge": item.net_edge,
                                "executable_net_edge": (item.executable_net_edge),
                                "required_net_edge": (item.required_net_edge),
                                "suggested_size": item.suggested_size,
                                "outcome": item.outcome,
                                "reason_code": item.reason_code,
                                "scheduled_event_id": (
                                    due_pair.scheduled_event_id or None
                                ),
                                "event_lane_state": (due_pair.event_lane_state or None),
                            }
                            for item in direction_evaluations
                        )

                    if opportunity:
                        evaluation_counts["opportunity_detected"] += 1
                        if self.paper_locked_arb is not None:
                            evaluation_counts[self.paper_locked_arb.last_decision] += 1
                        opp_dict = {
                            "opportunity_id": opportunity.opportunity_id,
                            "market_pair": pair.polymarket_question,
                            "kalshi_title": pair.kalshi_title,
                            "buy_platform": opportunity.buy_platform,
                            "sell_platform": opportunity.sell_platform,
                            "token": opportunity.token,
                            "buy_price": opportunity.buy_price,
                            "sell_price": opportunity.sell_price,
                            "gross_edge": opportunity.gross_edge,
                            "net_edge": opportunity.net_edge,
                            "edge_pct": opportunity.edge_pct,
                            "suggested_size": opportunity.suggested_size,
                            "max_size": opportunity.max_size,
                            "similarity": pair.similarity_score,
                            "scheduled_event_id": (due_pair.scheduled_event_id or None),
                            "event_lane_state": due_pair.event_lane_state or None,
                        }
                        if evaluation is not None and evaluation.execution is not None:
                            opp_dict["execution_id"] = evaluation.execution.execution_id
                            opp_dict["execution_phase"] = (
                                evaluation.execution.phase.value
                            )
                        dashboard_state.add_cross_platform_opportunity(opp_dict)
                        emergency_phase = (
                            evaluation is not None
                            and evaluation.execution is not None
                            and evaluation.execution.phase
                            in {
                                ExecutionPhase.RECOVERY_REQUIRED,
                                ExecutionPhase.RESIDUAL_EXPOSURE,
                            }
                        )
                        self._record_cross_platform_decision(
                            outcome=(
                                DecisionOutcome.ERROR
                                if emergency_phase
                                else DecisionOutcome.TRADE
                            ),
                            reason_code=(
                                "cross_platform_execution_emergency"
                                if emergency_phase
                                else "cross_platform_edge"
                            ),
                            explanation=(
                                f"Cross-platform arbitrage found: buy {opportunity.token} on "
                                f"{opportunity.buy_platform} and sell on {opportunity.sell_platform}; "
                                f"net edge {opportunity.edge_pct:.2%}."
                            ),
                            market_pair=pair,
                            evidence={**evidence, **opp_dict},
                        )
                    else:
                        rejection_reason = (
                            strongest_direction.reason_code
                            if direction_evaluations
                            else "no_executable_direction"
                        )
                        evaluation_counts[rejection_reason] += 1
                        self._record_cross_platform_decision(
                            outcome=DecisionOutcome.SKIP,
                            reason_code=rejection_reason,
                            explanation=(
                                "No cross-platform trade: the strongest direction "
                                "did not clear executable economics."
                            ),
                            market_pair=pair,
                            evidence=evidence,
                        )

                    await asyncio.sleep(0.05)

                if pending_snapshots:
                    await self._cancel_pending_snapshot_tasks(pending_snapshots)

                if due_pairs and evaluation_counts["paired_snapshot_fresh"] == 0:
                    all_timed_out = evaluation_counts["paired_snapshot_timeout"] == len(
                        due_pairs
                    )
                    reason = (
                        "all_paired_snapshots_timed_out"
                        if all_timed_out
                        else "paired_snapshot_starvation"
                    )
                    dashboard_state.cross_platform["scan_status"] = "degraded"
                    dashboard_state.cross_platform["degraded_reason"] = reason
                    if evaluation_counts["paired_snapshot_timeout"] > 0:
                        await self._recycle_snapshot_http_clients(reason=reason)
                elif (
                    evaluation_counts["paired_snapshot_fresh"] > 0
                    and not unexpected_snapshot_failure
                ):
                    dashboard_state.cross_platform["scan_status"] = "scanning"
                    dashboard_state.cross_platform.pop("degraded_reason", None)

                elapsed = (datetime.utcnow() - started).total_seconds()
                dashboard_state.cross_platform["scan_cycle_seconds"] = round(elapsed, 2)
                self._persist_cross_platform_evaluations(evaluation_rows)
                self._persist_cross_platform_evaluation_counts(evaluation_counts)
                self._scanner_supervisor.mark_healthy()
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                await self._cancel_pending_snapshot_tasks(pending_snapshots)
                raise
            except Exception as e:
                await self._cancel_pending_snapshot_tasks(pending_snapshots)
                self._persist_cross_platform_evaluations(evaluation_rows)
                self._persist_cross_platform_evaluation_counts(evaluation_counts)
                logger.exception("Cross-platform scan error: %s", e)
                dashboard_state.cross_platform["scan_status"] = "error"
                self._record_cross_platform_decision(
                    outcome=DecisionOutcome.ERROR,
                    reason_code="scanner_error",
                    explanation=f"Cross-platform scanner error: {e}",
                    evidence={"error": str(e)},
                )
                raise

    async def _recycle_snapshot_http_clients(self, *, reason: str) -> None:
        """Recycle read pools after a cycle proves they cannot yield one pair."""
        for venue, client in (
            ("polymarket", self.client),
            ("kalshi", self.kalshi_client),
        ):
            recycle = getattr(client, "recycle_http_client", None)
            if recycle is None:
                logger.error(
                    "%s client cannot recycle its HTTP pool after %s", venue, reason
                )
                continue
            try:
                recycled = await recycle(reason=reason)
            except Exception:
                logger.exception(
                    "Failed to recycle %s HTTP pool after %s", venue, reason
                )
                continue
            if recycled:
                logger.warning("Recycled %s HTTP pool after %s", venue, reason)

    @staticmethod
    async def _cancel_pending_snapshot_tasks(
        pending_snapshots: dict[asyncio.Task, object],
    ) -> None:
        if not pending_snapshots:
            return
        tasks = tuple(pending_snapshots)
        pending_snapshots.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _persist_cross_platform_evaluations(
        self,
        evaluations: list[dict],
    ) -> None:
        if not evaluations or self.paper_trade_store is None:
            return
        self.paper_trade_store.record_cross_platform_evaluations(evaluations)
        dashboard_state.cross_platform["evaluation_ledger_count"] = (
            self.paper_trade_store.cross_platform_evaluation_count()
        )
        dashboard_state.event_week["scorecard"] = (
            self.paper_trade_store.event_week_scorecard()
        )

    def _persist_event_operational_failure(
        self,
        due_pair,
        reason_code: str,
    ) -> None:
        if (
            self.paper_trade_store is None
            or not due_pair.scheduled_event_id
            or not due_pair.event_lane_state
            or self.paper_trade_store.active_run() is None
        ):
            return
        self.paper_trade_store.record_event_operational_failure(
            event_id=due_pair.scheduled_event_id,
            pair_id=due_pair.pair.pair_id,
            lane_state=due_pair.event_lane_state,
            reason_code=reason_code,
        )
        dashboard_state.event_week["scorecard"] = (
            self.paper_trade_store.event_week_scorecard()
        )

    def _persist_cross_platform_evaluation_counts(
        self,
        counts: Counter[str],
    ) -> None:
        if (
            not counts
            or self.paper_trade_store is None
            or self.paper_trade_store.active_run() is None
        ):
            return
        self.paper_trade_store.record_cross_platform_evaluation_counts(dict(counts))
        dashboard_state.cross_platform["evaluation_funnel"] = (
            self.paper_trade_store.cross_platform_evaluation_funnel()
        )

    async def stop(self) -> None:
        """Stop everything gracefully."""
        logger.info("Shutting down...")
        self._running = False

        if self.dashboard_integration:
            await self.dashboard_integration.stop()

        if self._news_catalyst_task:
            self._news_catalyst_task.cancel()
            try:
                await self._news_catalyst_task
            except asyncio.CancelledError:
                pass
        await self._shutdown_platform_opportunity_system()
        if self._xplat_scan_task:
            self._xplat_scan_task.cancel()
            try:
                await self._xplat_scan_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "Cross-platform scanner had already failed before shutdown",
                    exc_info=True,
                )
        if self._kalshi_monitor_task:
            self._kalshi_monitor_task.cancel()
            try:
                await self._kalshi_monitor_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "Cross-platform discovery had already failed before shutdown",
                    exc_info=True,
                )
        if self._critical_failure_task:
            try:
                await self._critical_failure_task
            except asyncio.CancelledError:
                pass

        if self.production_runtime:
            await self.production_runtime.stop()

        if self.data_feed:
            await self.data_feed.stop()

        if self.execution_engine:
            await self.execution_engine.stop()

        if self.client:
            await self.client.disconnect()

        if self.kalshi_client:
            await self.kalshi_client.__aexit__(None, None, None)

        if self.execution_journal:
            self.execution_journal.close()
            self.execution_journal = None
        if self.operator_controls:
            self.operator_controls.close()
            self.operator_controls = None
        self.production_runtime = None
        if self.paper_trade_store:
            try:
                active_run = self.paper_trade_store.active_run()
                if active_run is not None:
                    ending_equity, run_pnl = self._paper_run_performance()
                    finished_run = self.paper_trade_store.finish_run(
                        ending_equity=ending_equity,
                        pnl=run_pnl,
                        status=(
                            "completed"
                            if self._startup_complete and not self._run_failed
                            else "failed"
                        ),
                    )
                    logger.info(
                        "Paper run #%s %s | elapsed=%.1fs | transactions=%s | projected pnl=$%.2f | ending equity=$%.2f",
                        finished_run.run_number,
                        finished_run.status,
                        finished_run.elapsed_seconds,
                        finished_run.transaction_count,
                        finished_run.pnl,
                        finished_run.ending_equity,
                    )
                else:
                    logger.warning("No active paper run remained to finalize")
                dashboard_state.run_session = {}
                dashboard_state.run_sessions = [
                    run.to_dict()
                    for run in self.paper_trade_store.recent_runs(limit=50)
                ]
            except Exception:
                logger.exception("Failed to finalize paper run; continuing shutdown")
            finally:
                self.paper_trade_store.close()
                self.paper_trade_store = None
        self.paper_locked_arb = None
        configure_dashboard_runtime(
            store=None,
            timezone=self.config.monitoring.display_timezone,
            runtime=None,
            production_required=False,
            readiness_check=None,
        )

        if self._server:
            self._server.should_exit = True
        if self._server_task:
            try:
                await asyncio.wait_for(self._server_task, timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("Dashboard server did not stop within 30 seconds")
                self._server_task.cancel()
                try:
                    await self._server_task
                except asyncio.CancelledError:
                    pass

        # Final summary
        if self.portfolio:
            summary = self.portfolio.get_summary()
            logger.info("=" * 60)
            logger.info("Final Summary")
            logger.info("=" * 60)
            logger.info(f"Total PnL: ${summary['pnl']['total_pnl']:.2f}")
            logger.info(f"Trades: {summary['total_trades']}")
            logger.info(f"Win Rate: {summary['win_rate']:.1%}")

        # Cross-platform summary
        if self.cross_platform_engine:
            cp_stats = self.cross_platform_engine.get_stats()
            logger.info(
                f"Cross-Platform Opportunities: {cp_stats['total_opportunities']}"
            )
            logger.info(f"Matched Market Pairs: {cp_stats['matched_pairs']}")

        logger.info("Shutdown complete")

    async def run_forever(self) -> None:
        """Run until interrupted."""
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass


async def main_async(args: argparse.Namespace) -> bool:
    """Async main function."""
    # Load config
    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)

    # Override mode and revalidate the effective config so --live cannot bypass
    # live-only credential and real-data checks performed during initial load.
    try:
        if args.live:
            config.mode.trading_mode = "live"
        elif args.dry_run:
            config.mode.trading_mode = "dry_run"
        resolve_runtime_secrets(config)
        validate_config(config)
    except Exception as e:
        logger.error(f"Invalid effective config: {e}")
        sys.exit(1)

    # Create and run bot with dashboard
    bot = TradingBotWithDashboard(config, port=args.port)

    # Handle shutdown
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()
    critical_failure = False

    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    try:
        await bot.start()

        # A critical dependency failure is a process-terminal condition. This
        # prevents a dead trading loop from retaining an "active" paper run.
        shutdown_wait = asyncio.create_task(shutdown_event.wait())
        failure_wait = asyncio.create_task(bot.failure_event.wait())
        _done, pending = await asyncio.wait(
            {shutdown_wait, failure_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for pending_task in pending:
            pending_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if bot.failure_event.is_set():
            critical_failure = True
            logger.error("Critical trading dependency failed; shutting down")

    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()
    return not critical_failure


def main() -> None:
    """Main entry point."""
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Bot with Live Dashboard"
    )

    parser.add_argument(
        "-c", "--config", default="config.yaml", help="Config file path"
    )

    parser.add_argument(
        "--port", type=int, default=8888, help="Dashboard port (default: 8888)"
    )

    parser.add_argument("--live", action="store_true", help="Run in live mode")

    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Run in dry-run mode (default)",
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    # Load the selected profile before creating handlers so profile-specific
    # log files never first appear under the default logs/ directory.
    try:
        logging_config = load_config(args.config).logging
    except Exception as exc:
        parser.error(f"Failed to load config: {exc}")

    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logging(
        log_dir=logging_config.log_dir,
        console_level=log_level,
        main_log_file=logging_config.main_log_file,
        trades_log_file=logging_config.trades_log_file,
        opportunities_log_file=logging_config.opportunities_log_file,
    )

    # Run
    try:
        if not asyncio.run(main_async(args)):
            raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nShutdown complete.")


if __name__ == "__main__":
    main()
