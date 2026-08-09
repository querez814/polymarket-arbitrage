from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio

import httpx
import pytest

from run_with_dashboard import TradingBotWithDashboard, _catalog_coverage_status
from core.combinatorial_arb import SamePlatformArbitrageDetector
from core.cross_platform_arb import MarketPair
from core.event_contracts import EventPairLink
from core.event_lane import EventLanePolicy, EventLaneScheduler
from core.pair_monitoring import PairTierMonitor
from core.platform_opportunities import (
    MonitoringAssignment,
    PlatformOpportunitySystem,
    PoliticalWatchPolicy,
)
from core.platform_opportunity_runtime import PlatformOpportunityWorker
from core.production_runtime import ProductionArbitrageRuntime
from core.execution_journal import ExecutionJournal
from core.operations import PersistentOperatorControls
from dashboard.server import dashboard_state
from utils.config_loader import BotConfig
from utils.platform_opportunity_store import PlatformOpportunityStore
from utils.task_supervision import RestartingTaskSupervisor
from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from kalshi_client.models import KalshiMarket
from kalshi_client.models import KalshiMilestone


def test_catalog_coverage_status_separates_bounded_and_failed_sources():
    assert _catalog_coverage_status(
        {"complete": True, "stop_reason": "source_exhausted"}
    ) == "complete"
    for stop_reason in (
        "page_budget",
        "wall_time_budget",
        "decoded_byte_budget",
        "market_budget",
    ):
        assert _catalog_coverage_status(
            {"complete": False, "stop_reason": stop_reason}
        ) == "bounded"
    assert _catalog_coverage_status(
        {"complete": False, "stop_reason": "decode_error"}
    ) == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_reason", "dashboard_key"),
    [
        ("book", "book_read_failed", "book_read_failures"),
        ("fee", "fee_metadata_failed", "fee_metadata_failures"),
    ],
)
async def test_platform_hot_sampler_persists_reason_coded_read_failures(
    tmp_path, failure, expected_reason, dashboard_key
):
    """A failed public read is durable evidence, never a successful observation."""
    bot = TradingBotWithDashboard(BotConfig())
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    system._sampled_contract_ids = {"polymarket:target"}
    bot.platform_opportunity_system = system
    bot.platform_opportunity_worker = SimpleNamespace(
        submit_book=lambda *args, **kwargs: True
    )
    bot._platform_hot_assignments = (
        MonitoringAssignment(
            contract_id="polymarket:target",
            venue="polymarket",
            native_id="target",
            catalyst_at=None,
            reason="test",
            priority_score=1.0,
            cadence="hot",
            interval_seconds=0.0,
        ),
    )
    bot.config.platform_opportunity.hot_poll_seconds = 0.0
    bot._running = True

    class Client:
        async def get_orderbook(self, native_id):
            if failure == "book":
                bot._running = False
                raise RuntimeError("book unavailable")
            return OrderBook(market_id=native_id)

        async def get_market(self, native_id):
            bot._running = False
            raise RuntimeError("fee metadata unavailable")

    bot._platform_poly_client = Client()
    await bot._platform_hot_sampling_loop()

    failures = system.store.observation_failure_telemetry(cohort_id=system.cohort_id)
    assert set(failures) == {"polymarket:target"}
    failure_record = failures["polymarket:target"][expected_reason]
    assert failure_record["failure_count"] == 1
    assert failure_record["last_failed_at"]
    assert system.store.observation_telemetry(cohort_id=system.cohort_id) == {}
    assert dashboard_state.platform_opportunity[dashboard_key] == 1
    assert dashboard_state.platform_opportunity["observation_failures"] == failures
    system.store.close()


@pytest.mark.asyncio
async def test_platform_hot_sampler_survives_one_time_failure_telemetry_error(
    tmp_path,
):
    """Failure telemetry must not silently kill the hot sampler task."""
    bot = TradingBotWithDashboard(BotConfig())
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(tmp_path / "opportunities.db")
    )
    submitted = []

    def submit_book(
        contract_id,
        book,
        *,
        observed_at,
        request_started_at,
        received_at,
        fee_schedule,
    ):
        submitted.append((contract_id, observed_at, request_started_at, received_at))
        bot._running = False
        return True

    bot.platform_opportunity_system = system
    bot.platform_opportunity_worker = SimpleNamespace(submit_book=submit_book)
    bot._platform_hot_assignments = (
        MonitoringAssignment(
            contract_id="kalshi:failed",
            venue="kalshi",
            native_id="failed",
            catalyst_at=None,
            reason="test",
            priority_score=2.0,
            cadence="hot",
            interval_seconds=0.0,
        ),
        MonitoringAssignment(
            contract_id="kalshi:later",
            venue="kalshi",
            native_id="later",
            catalyst_at=None,
            reason="test",
            priority_score=1.0,
            cadence="hot",
            interval_seconds=0.0,
        ),
    )
    bot._platform_fee_cache = {
        assignment.contract_id: (object(), float("inf"))
        for assignment in bot._platform_hot_assignments
    }
    bot.config.platform_opportunity.hot_poll_seconds = 0.0
    bot._running = True

    class Client:
        async def get_orderbook_unified(self, native_id):
            if native_id == "failed":
                raise RuntimeError("book unavailable")
            return SimpleNamespace(timestamp=datetime.now(timezone.utc))

    calls = 0

    def record_failure_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("database temporarily unavailable")

    bot._platform_kalshi_client = Client()
    system.record_observation_failure = record_failure_once

    await bot._platform_hot_sampling_loop()

    assert calls == 1
    assert [item[0] for item in submitted] == ["kalshi:later"]
    _, observed_at, request_started_at, received_at = submitted[0]
    assert request_started_at <= received_at == observed_at
    assert dashboard_state.platform_opportunity["status"] == "degraded"
    system.store.close()


def test_platform_observation_failure_without_system_degrades_dashboard(caplog):
    """An optional system that has shut down cannot crash failure handling."""
    bot = TradingBotWithDashboard(BotConfig())
    bot.platform_opportunity_system = None

    bot._record_platform_observation_failure(
        "kalshi:unavailable",
        reason_code="book_read_failed",
        failed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    assert dashboard_state.platform_opportunity["status"] == "degraded"
    assert "not initialized" in dashboard_state.platform_opportunity["last_error"]
    assert "not initialized" in caplog.text


@pytest.mark.asyncio
async def test_live_cross_platform_pair_uses_owned_production_runtime():
    config = BotConfig()
    config.mode.trading_mode = "live"
    config.mode.cross_platform_execution_enabled = True
    bot = TradingBotWithDashboard(config)

    class Detector:
        def check_arbitrage(self, *args, **kwargs):
            raise AssertionError(
                "live execution must not use the detached detector path"
            )

    class Runtime:
        def status(self):
            raise AssertionError("the owned runtime performs its own admission checks")

        async def evaluate_pair(self, pair, polymarket_book, kalshi_book):
            assert pair == "pair"
            assert polymarket_book == "poly-book"
            assert kalshi_book == "kalshi-book"
            return SimpleNamespace(
                opportunity="owned-opportunity", execution="execution"
            )

    bot.cross_platform_engine = Detector()
    bot.production_runtime = Runtime()

    opportunity, evaluation = await bot.evaluate_cross_platform_pair(
        "pair", "poly-book", "kalshi-book"
    )

    assert opportunity == "owned-opportunity"
    assert evaluation.execution == "execution"


@pytest.mark.asyncio
async def test_live_cross_platform_pair_fails_closed_without_runtime_owner():
    config = BotConfig()
    config.mode.trading_mode = "live"
    config.mode.cross_platform_execution_enabled = True
    bot = TradingBotWithDashboard(config)
    bot.cross_platform_engine = object()

    with pytest.raises(RuntimeError, match="ownership is missing"):
        await bot.evaluate_cross_platform_pair("pair", "poly-book", "kalshi-book")


@pytest.mark.asyncio
async def test_real_data_paper_pair_uses_authoritative_pair_economics():
    config = BotConfig()
    config.mode.trading_mode = "dry_run"
    config.mode.data_mode = "real"
    bot = TradingBotWithDashboard(config)
    fee_snapshot = object()

    class Provider:
        async def quote_pair(self, pair):
            assert pair == "pair"
            return fee_snapshot

    class Detector:
        def check_arbitrage(
            self, pair, polymarket_book, kalshi_book, *, economics: object
        ):
            assert pair == "pair"
            assert polymarket_book == "poly-book"
            assert kalshi_book == "kalshi-book"
            assert economics is fee_snapshot
            return "authoritative-paper-opportunity"

    bot.cross_platform_engine = Detector()
    bot.economics_provider = Provider()

    opportunity, evaluation = await bot.evaluate_cross_platform_pair(
        "pair", "poly-book", "kalshi-book"
    )

    assert opportunity == "authoritative-paper-opportunity"
    assert evaluation is None


def test_live_dashboard_binds_operator_routes_to_loopback_only():
    live = BotConfig()
    live.mode.trading_mode = "live"
    dry = BotConfig()

    assert TradingBotWithDashboard(live).dashboard_host == "127.0.0.1"
    assert TradingBotWithDashboard(dry).dashboard_host == "0.0.0.0"


def test_cross_platform_readiness_requires_live_monitor_and_scanner_tasks():
    config = BotConfig()
    config.mode.cross_platform_enabled = True
    config.mode.kalshi_enabled = True
    bot = TradingBotWithDashboard(config)
    pending = SimpleNamespace(done=lambda: False)
    finished = SimpleNamespace(done=lambda: True)

    assert bot._critical_dependencies_ready() is False

    bot._kalshi_monitor_task = pending
    bot._xplat_scan_task = pending
    bot._matched_pairs = [object()]
    from dashboard.server import dashboard_state

    dashboard_state.cross_platform.update(
        {
            "matching_status": "complete",
            "scan_status": "scanning",
            "last_fresh_snapshot_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    assert bot._critical_dependencies_ready() is True

    bot._xplat_scan_task = finished
    assert bot._critical_dependencies_ready() is False


def test_cross_platform_readiness_rejects_stale_snapshot_scanner():
    config = BotConfig()
    config.mode.cross_platform_enabled = True
    config.mode.kalshi_enabled = True
    bot = TradingBotWithDashboard(config)
    pending = SimpleNamespace(done=lambda: False)
    bot._kalshi_monitor_task = pending
    bot._xplat_scan_task = pending
    bot._matched_pairs = [object()]

    from dashboard.server import dashboard_state

    dashboard_state.cross_platform.update(
        {
            "matching_status": "complete",
            "scan_status": "scanning",
            "last_fresh_snapshot_at": (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat(),
        }
    )

    assert bot._critical_dependencies_ready() is False


@pytest.mark.asyncio
async def test_critical_background_task_completion_durably_panics_runtime():
    config = BotConfig()
    config.production.operator_token = "operator-token"
    bot = TradingBotWithDashboard(config)
    calls = []

    class Runtime:
        async def panic(self, token, *, reason):
            calls.append((token, reason))

    async def completed():
        return None

    bot.production_runtime = Runtime()
    bot._running = True
    task = asyncio.create_task(completed())
    await task

    bot._critical_task_done(task)
    await bot._critical_failure_task

    assert bot._run_failed is True
    assert bot.failure_event.is_set() is True
    assert calls == [
        ("operator-token", "critical cross-platform task stopped unexpectedly")
    ]


@pytest.mark.asyncio
async def test_zero_matches_is_an_honest_wait_state_not_a_started_scan():
    bot = TradingBotWithDashboard(BotConfig())
    bot._kalshi_markets = [object()]

    class Matcher:
        async def find_matches(self, polymarket_markets, kalshi_markets, on_progress):
            assert polymarket_markets == ["poly-market"]
            assert kalshi_markets == bot._kalshi_markets
            return []

        def get_cached_pairs(self):
            return []

        def get_review_candidates(self, limit=100):
            return []

    bot.market_matcher = Matcher()

    await bot._run_matching_background(["poly-market"])

    from dashboard.server import dashboard_state

    assert dashboard_state.cross_platform["matching_status"] == "no_matches"
    decision = bot.decision_journal.recent()[-1]
    assert decision.reason_code == "no_equivalent_pairs"
    assert "price scan" not in decision.explanation
    assert bot._xplat_scan_task is None


def test_runtime_applies_event_clock_to_existing_pair_monitoring_seam():
    event_at = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    now = event_at - timedelta(minutes=3)
    bot = TradingBotWithDashboard(BotConfig())
    pair = MarketPair(
        polymarket_id="poly-cpi",
        kalshi_ticker="kx-cpi",
        polymarket_question="Core CPI above 0.3%?",
        kalshi_title="Core CPI above 0.3%?",
        similarity_score=0.98,
    )
    bot._event_pair_links = [
        EventPairLink(
            event_id="bls:cpi-july-2026",
            event_type="cpi",
            scheduled_at=event_at,
            pair=pair,
        )
    ]
    bot.event_lane_scheduler = EventLaneScheduler(
        policy=EventLanePolicy(),
        clock=lambda: now,
    )
    bot.pair_monitor = PairTierMonitor(
        hot_limit=1,
        hot_interval=2,
        cold_interval=30,
        clock=lambda: now,
    )

    bot._apply_event_lane_schedule()

    due = bot.pair_monitor.due_pairs([pair])
    assert len(due) == 1
    assert due[0].tier == "event_burst"
    assert due[0].scheduled_event_id == "bls:cpi-july-2026"
    from dashboard.server import dashboard_state

    assert dashboard_state.event_week["active_lanes"][0]["state"] == "burst"


@pytest.mark.asyncio
async def test_market_discovery_stays_alive_after_a_matching_cycle():
    config = BotConfig()
    config.mode.cross_platform_refresh_seconds = 3_600
    bot = TradingBotWithDashboard(config)
    bot._running = True
    bot.data_feed = SimpleNamespace(
        _markets={f"poly-{index}": object() for index in range(50)}
    )

    class KalshiClient:
        async def list_all_event_markets(self, **kwargs):
            return [object()]

    matching_completed = asyncio.Event()

    async def complete_one_cycle(polymarket_markets):
        matching_completed.set()

    bot.kalshi_client = KalshiClient()
    bot._run_matching_background = complete_one_cycle

    task = asyncio.create_task(bot._start_kalshi_monitoring())
    await asyncio.wait_for(matching_completed.wait(), timeout=2)

    assert task.done() is False

    bot._running = False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_market_discovery_recovers_after_transient_upstream_503():
    config = BotConfig()
    config.mode.cross_platform_refresh_seconds = 0
    bot = TradingBotWithDashboard(config)
    bot._running = True
    bot.data_feed = SimpleNamespace(
        _markets={f"poly-{index}": object() for index in range(50)}
    )
    requests = 0
    retry_delays = []

    class KalshiClient:
        async def list_all_event_markets(self, **kwargs):
            nonlocal requests
            requests += 1
            if requests == 1:
                request = httpx.Request("GET", "https://trading-api.kalshi.com/events")
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError(
                    "temporary upstream failure",
                    request=request,
                    response=response,
                )
            return [object()]

    async def matching_completed(_polymarket_markets):
        bot._running = False

    async def fake_sleep(delay):
        retry_delays.append(delay)

    bot.kalshi_client = KalshiClient()
    bot._run_matching_background = matching_completed
    bot._discovery_supervisor = RestartingTaskSupervisor(
        "cross-platform discovery",
        base_delay=2.0,
        sleep=fake_sleep,
        on_retry=bot._on_cross_platform_task_retry,
    )

    await bot._discovery_supervisor.run(
        bot._start_kalshi_monitoring,
        should_run=lambda: bot._running,
    )

    assert requests == 2
    assert retry_delays == [2.0]
    assert bot._run_failed is False

    from dashboard.server import dashboard_state

    assert (
        dashboard_state.cross_platform["task_restarts"]["cross-platform discovery"] >= 1
    )
    assert dashboard_state.cross_platform["last_transient_error"]["attempt"] == 1


def test_bundle_scan_activity_is_visible_when_no_candidates_exist(caplog):
    caplog.set_level("INFO")
    config = BotConfig()
    bot = TradingBotWithDashboard(config)
    bot.same_platform_detector = SamePlatformArbitrageDetector(min_edge=0.02)
    bot.data_feed = SimpleNamespace(get_all_market_states=lambda: {})

    bot._run_combinatorial_scan()

    from dashboard.server import dashboard_state

    metrics = dashboard_state.operational["bundle_arb"]
    assert metrics["scans"] == 1
    assert metrics["states"] == 0
    assert metrics["eligible_groups"] == 0
    assert metrics["opportunities"] == 0
    assert "Bundle arb scan active" in caplog.text


def _priced_book(market_id, *, yes_bid, yes_ask, no_bid, no_ask):
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            TokenType.YES,
            bids=OrderBookSide([PriceLevel(yes_bid, 10)]),
            asks=OrderBookSide([PriceLevel(yes_ask, 10)]),
        ),
        no=TokenOrderBook(
            TokenType.NO,
            bids=OrderBookSide([PriceLevel(no_bid, 10)]),
            asks=OrderBookSide([PriceLevel(no_ask, 10)]),
        ),
    )


def test_matched_pair_dashboard_snapshot_uses_live_books():
    bot = TradingBotWithDashboard(BotConfig())
    pair = SimpleNamespace(
        polymarket_id="poly-1",
        kalshi_ticker="KX-1",
        polymarket_question="Will Alice win?",
        kalshi_title="Alice wins?",
        similarity_score=0.94,
        category="politics",
    )
    poly_book = _priced_book(
        "poly-1", yes_bid=0.42, yes_ask=0.46, no_bid=0.54, no_ask=0.58
    )
    kalshi_book = _priced_book(
        "KX-1", yes_bid=0.51, yes_ask=0.53, no_bid=0.47, no_ask=0.49
    )
    bot.data_feed = SimpleNamespace(get_order_book=lambda _market_id: poly_book)
    bot._kalshi_orderbooks["KX-1"] = kalshi_book

    row = bot._matched_pair_dashboard_row(pair)

    assert row["poly_yes"] == pytest.approx(0.42)
    assert row["poly_no"] == pytest.approx(0.54)
    assert row["kalshi_yes"] == pytest.approx(0.51)
    assert row["kalshi_no"] == pytest.approx(0.47)


def test_matched_pair_dashboard_snapshot_preserves_missing_prices_as_null():
    bot = TradingBotWithDashboard(BotConfig())
    pair = SimpleNamespace(
        polymarket_id="poly-1",
        kalshi_ticker="KX-1",
        polymarket_question="Will Alice win?",
        kalshi_title="Alice wins?",
        similarity_score=0.94,
        category="politics",
    )
    bot.data_feed = SimpleNamespace(get_order_book=lambda _market_id: None)

    row = bot._matched_pair_dashboard_row(pair)

    assert row["poly_yes"] is None
    assert row["kalshi_yes"] is None


@pytest.mark.asyncio
async def test_bot_shutdown_waits_for_dashboard_server_to_exit():
    bot = TradingBotWithDashboard(BotConfig())
    server = SimpleNamespace(should_exit=False)

    async def serve_until_stopped():
        while not server.should_exit:
            await asyncio.sleep(0)

    bot._server = server
    task = asyncio.create_task(serve_until_stopped())
    bot._server_task = task

    await bot.stop()

    assert server.should_exit is True
    assert task.done()


@pytest.mark.asyncio
async def test_bot_shutdown_continues_when_no_active_paper_run_exists(tmp_path):
    from utils.paper_trade_store import PaperTradeStore

    bot = TradingBotWithDashboard(BotConfig())
    bot.paper_trade_store = PaperTradeStore(str(tmp_path / "paper.db"))

    await bot.stop()

    assert bot.paper_trade_store is None


@pytest.mark.asyncio
async def test_shadow_startup_failure_degrades_without_aborting_bot(monkeypatch):
    bot = TradingBotWithDashboard(BotConfig())

    async def fail():
        raise OSError("research disk unavailable")

    monkeypatch.setattr(bot, "_configure_platform_opportunity_system", fail)

    await bot._start_platform_opportunity_safely()

    assert dashboard_state.platform_opportunity["status"] == "degraded"
    assert dashboard_state.platform_opportunity["execution_authority"] == "none"
    assert (
        "research disk unavailable"
        in dashboard_state.platform_opportunity["last_error"]
    )


@pytest.mark.asyncio
async def test_platform_catalog_uses_only_ordinary_kalshi_inventory():
    """Political discovery must not fetch or persist the MVE-only inventory."""

    class KalshiCatalog:
        def __init__(self):
            self.calls = []
            self.last_catalog_status = {"complete": True, "stop_reason": "complete"}

        async def list_full_market_catalog(self, **kwargs):
            self.calls.append(kwargs)
            return [
                KalshiMarket(
                    ticker="KXPOL-1",
                    event_ticker="KXPOL",
                    series_ticker="KXPOL",
                    title="Political event",
                    event_title="Political event",
                )
            ]

        async def list_all_milestones(self, **kwargs):
            self.calls.append(kwargs)
            return []

    class Worker:
        async def refresh_catalog(self, **kwargs):
            self.received = kwargs
            return SimpleNamespace(
                catalog_contracts=len(kwargs["kalshi_markets"]),
                revisions_written=1,
                monitoring=SimpleNamespace(hot=(), warm=(), budget_excluded=()),
            )

    bot = TradingBotWithDashboard(BotConfig())
    bot._platform_kalshi_client = KalshiCatalog()
    bot.platform_opportunity_worker = Worker()

    await bot._refresh_platform_catalog()

    assert [
        call["mve_filter"]
        for call in bot._platform_kalshi_client.calls
        if "mve_filter" in call
    ] == ["exclude"]
    assert [
        market.ticker
        for market in bot.platform_opportunity_worker.received["kalshi_markets"]
    ] == ["KXPOL-1"]
    assert dashboard_state.platform_opportunity["catalog"]["multivariate_requests"] == 0


@pytest.mark.asyncio
async def test_platform_catalog_fetches_exact_political_milestones_and_passes_them_on():
    now = datetime.now(timezone.utc)

    class KalshiCatalog:
        last_catalog_status = {"complete": True, "stop_reason": "complete"}

        async def list_full_market_catalog(self, **kwargs):
            assert kwargs["mve_filter"] == "exclude"
            return [
                KalshiMarket(
                    ticker="KXTRUMPMENTION-26AUG10-T1",
                    event_ticker="KXTRUMPMENTION-26AUG10",
                    series_ticker="KXTRUMPMENTION",
                    title="Will Trump mention tariffs?",
                    event_title="Trump remarks",
                    category="Politics",
                )
            ]

        async def list_all_milestones(self, **kwargs):
            assert kwargs == {
                "max_pages": 2,
                "max_milestones": 20,
                "related_event_ticker": "KXTRUMPMENTION-26AUG10",
            }
            from kalshi_client.models import KalshiMilestone

            return [
                KalshiMilestone(
                    milestone_id="mention",
                    title="Trump remarks",
                    category="Politics",
                    milestone_type="speech",
                    start_time=now - timedelta(minutes=15),
                    end_time=now + timedelta(minutes=45),
                    related_event_tickers=("KXTRUMPMENTION-26AUG10",),
                    primary_event_tickers=("KXTRUMPMENTION-26AUG10",),
                    source_id="source",
                )
            ]

    class Worker:
        async def refresh_catalog(self, **kwargs):
            self.received = kwargs
            return SimpleNamespace(
                catalog_contracts=1,
                revisions_written=1,
                monitoring=SimpleNamespace(hot=(), warm=(), budget_excluded=()),
            )

    config = BotConfig()
    config.platform_opportunity.reviewed_pinned_event_ids = [
        "kalshi:KXTRUMPMENTION-26AUG10"
    ]
    bot = TradingBotWithDashboard(config)
    bot._platform_kalshi_client = KalshiCatalog()
    bot.platform_opportunity_worker = Worker()

    await bot._refresh_platform_catalog()

    assert [
        item.milestone_id
        for item in bot.platform_opportunity_worker.received["kalshi_milestones"]
    ] == ["mention"]
    status = dashboard_state.platform_opportunity["catalog"]["source_status"][
        "kalshi_political_milestones"
    ]
    assert status["requested_event_tickers"] == 1
    assert status["successful_event_tickers"] == 1


@pytest.mark.asyncio
async def test_political_v2_real_component_preflight_is_shadow_only_and_restart_safe(
    tmp_path, monkeypatch
):
    """Exercise the real catalog boundary without network, runtime startup, or orders."""
    start = datetime(2026, 8, 10, 22, 30, tzinfo=timezone.utc)
    end = datetime(2026, 8, 10, 23, 15, tzinfo=timezone.utc)
    config = BotConfig()
    config.mode.cross_platform_enabled = False
    config.mode.cross_platform_execution_enabled = False
    config.mode.semantic_matching_enabled = False
    config.platform_opportunity.reviewed_pinned_event_ids = [
        "kalshi:KXTRUMPMENTION-26AUG10"
    ]
    config.platform_opportunity.catalog_path = str(tmp_path / "catalog.db")
    config.platform_opportunity.political_max_events = 1
    config.platform_opportunity.political_max_contracts_per_event = 6
    config.platform_opportunity.political_warm_before_hours = 24
    config.platform_opportunity.political_hot_before_minutes = 60
    config.platform_opportunity.political_cooldown_after_hours = 2
    watch_policy = PoliticalWatchPolicy(
        max_events=config.platform_opportunity.political_max_events,
        max_contracts_per_event=(
            config.platform_opportunity.political_max_contracts_per_event
        ),
        lookahead=timedelta(days=config.platform_opportunity.political_lookahead_days),
        warm_before=timedelta(
            hours=config.platform_opportunity.political_warm_before_hours
        ),
        hot_before=timedelta(
            minutes=config.platform_opportunity.political_hot_before_minutes
        ),
        warm_poll_seconds=config.platform_opportunity.political_warm_poll_seconds,
        hot_poll_seconds=config.platform_opportunity.political_hot_poll_seconds,
        event_poll_seconds=config.platform_opportunity.political_event_poll_seconds,
        cooldown_poll_seconds=config.platform_opportunity.political_cooldown_poll_seconds,
        cooldown_after=timedelta(
            hours=config.platform_opportunity.political_cooldown_after_hours
        ),
        reviewed_pinned_event_ids=tuple(
            config.platform_opportunity.reviewed_pinned_event_ids
        ),
    )

    class CatalogClient:
        last_catalog_status = {"complete": True, "stop_reason": "complete"}

        def __init__(self):
            self.catalog_calls = []
            self.milestone_calls = []

        async def list_full_market_catalog(
            self,
            *,
            status,
            mve_filter,
            max_markets,
            max_pages,
            max_decoded_bytes,
            wall_time_seconds,
        ):
            self.catalog_calls.append(
                {
                    "status": status,
                    "mve_filter": mve_filter,
                    "max_markets": max_markets,
                    "max_pages": max_pages,
                    "max_decoded_bytes": max_decoded_bytes,
                    "wall_time_seconds": wall_time_seconds,
                }
            )
            return [
                KalshiMarket(
                    ticker=f"KXTRUMPMENTION-26AUG10-T{index}",
                    event_ticker="KXTRUMPMENTION-26AUG10",
                    series_ticker="KXTRUMPMENTION",
                    title=f"Will Trump mention topic {index}?",
                    event_title="Trump remarks",
                    category="Politics",
                    volume=1_000,
                    open_interest=500,
                )
                for index in range(1, 3)
            ]

        async def list_all_milestones(
            self, *, max_pages, max_milestones, related_event_ticker
        ):
            self.milestone_calls.append(
                {
                    "max_pages": max_pages,
                    "max_milestones": max_milestones,
                    "related_event_ticker": related_event_ticker,
                }
            )
            return [
                KalshiMilestone(
                    milestone_id="trump-remarks",
                    title="Trump remarks",
                    category="Politics",
                    milestone_type="speech",
                    start_time=start,
                    end_time=end,
                    related_event_tickers=(related_event_ticker,),
                    primary_event_tickers=(related_event_ticker,),
                    source_id="official-schedule",
                )
            ]

    class NoMutationVenue:
        def __init__(self, venue):
            self.venue = venue
            self.mutations = []

        async def available_collateral(self):
            return 1_000.0

        async def prepare_ioc(self, intent, *, idempotency_key, size):
            self.mutations.append("prepare_ioc")
            raise AssertionError("preflight must not prepare an order")

        async def submit_prepared(self, prepared):
            self.mutations.append("submit_prepared")
            raise AssertionError("preflight must not submit an order")

        async def cancel_open(self, order):
            self.mutations.append("cancel_open")

        async def read_order(self, lookup):
            return None

        async def list_open_orders(self):
            return ()

        async def list_positions(self):
            return ()

    bot = TradingBotWithDashboard(config)
    system = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(config.platform_opportunity.catalog_path),
        political_watch_policy=watch_policy,
    )
    worker = PlatformOpportunityWorker(system)
    client = CatalogClient()
    bot._platform_kalshi_client = client
    bot.platform_opportunity_system = system
    bot.platform_opportunity_worker = worker
    monkeypatch.setattr(bot, "_platform_catalyst_references", lambda: [])

    await bot._refresh_platform_catalog()

    assert client.catalog_calls[0]["mve_filter"] == "exclude"
    assert client.milestone_calls == [
        {
            "max_pages": 2,
            "max_milestones": 20,
            "related_event_ticker": "KXTRUMPMENTION-26AUG10",
        }
    ]
    assert dashboard_state.platform_opportunity["catalog"]["multivariate_requests"] == 0
    assert system.store.summary(cohort_id=system.cohort_id)["current"] == 2
    assert len(system._political_locks) == 1
    [lock] = system._political_locks.values()
    assert 1 <= len(lock.contract_ids) <= 6
    assert (lock.event_start_at, lock.event_end_at) == (start, end)
    assert lock.locked_until == end + timedelta(hours=2)

    def cadence(at):
        return {item.cadence for item in system.plan_monitoring(at).hot}

    assert {
        item.cadence
        for item in system.plan_monitoring(start - timedelta(hours=25)).warm
    } == {"warm"}
    assert cadence(start - timedelta(minutes=30)) == {"hot"}
    assert cadence(start + timedelta(minutes=10)) == {"event_live"}
    assert cadence(end + timedelta(minutes=30)) == {"cooldown"}
    assert system.plan_monitoring(end + timedelta(hours=2, seconds=1)).hot == ()

    schedule = system._fee_schedules.get(lock.contract_ids[0])
    if schedule is None:
        from core.platform_opportunities import VenueFeeSchedule

        schedule = VenueFeeSchedule("kalshi", "none", 0, 1, 0, start, "preflight")
    await worker.start()
    assert worker.submit_book(
        lock.contract_ids[0],
        OrderBook(market_id=lock.contract_ids[0]),
        observed_at=start,
        fee_schedule=schedule,
    )
    await worker.stop()
    selected_at = lock.selected_at
    assert (
        system.store.observation_telemetry(cohort_id=system.cohort_id)[
            lock.contract_ids[0]
        ]["observation_count"]
        == 1
    )
    system.store.close()

    resumed = PlatformOpportunitySystem(
        store=PlatformOpportunityStore(config.platform_opportunity.catalog_path),
        political_watch_policy=watch_policy,
    )
    [resumed_lock] = resumed._political_locks.values()
    assert resumed_lock.selected_at == selected_at
    assert resumed_lock.contract_ids == lock.contract_ids
    assert resumed.store.summary(cohort_id=resumed.cohort_id)["current"] == 2

    poly = NoMutationVenue("polymarket")
    kalshi = NoMutationVenue("kalshi")
    controls = PersistentOperatorControls(
        tmp_path / "operator.sqlite3", auth_token="x" * 32
    )
    with ExecutionJournal(tmp_path / "journal.sqlite3") as journal:
        runtime = ProductionArbitrageRuntime(
            journal=journal,
            adapters={"polymarket": poly, "kalshi": kalshi},
            controls=controls,
            min_net_edge=0.02,
            max_order_notional=10.0,
            economics_max_age=timedelta(seconds=30),
            require_private_stream=False,
        )
        assert runtime.status().started is False
        assert runtime.status().ready is False
    controls.close()
    resumed.store.close()
    assert config.mode.cross_platform_enabled is False
    assert config.mode.semantic_matching_enabled is False
    assert poly.mutations == []
    assert kalshi.mutations == []


@pytest.mark.asyncio
async def test_critical_failure_marks_persisted_run_failed(tmp_path):
    from utils.paper_trade_store import PaperTradeStore

    db_path = tmp_path / "paper.db"
    bot = TradingBotWithDashboard(BotConfig())
    bot.paper_trade_store = PaperTradeStore(str(db_path))
    bot.paper_trade_store.start_run(
        starting_equity=1000.0,
        pnl_source="projected_locked_paper",
    )
    bot._startup_complete = True
    bot._run_failed = True

    await bot.stop()

    reopened = PaperTradeStore(str(db_path))
    try:
        assert reopened.recent_runs(limit=1)[0].status == "failed"
    finally:
        reopened.close()
