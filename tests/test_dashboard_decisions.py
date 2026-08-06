from datetime import datetime, timezone

import pytest

from core.portfolio import Portfolio
from dashboard.integration import DashboardIntegration
from dashboard.server import DashboardState, dashboard_state


def test_dashboard_state_includes_decision_journal_data():
    state = DashboardState()
    state.add_decision(
        {
            "decision_id": "dec_1",
            "strategy": "bundle_arb",
            "outcome": "skip",
            "reason_code": "edge_below_threshold",
            "explanation": "edge too low",
        }
    )
    state.decision_summary = {
        "total": 1,
        "by_outcome": {"skip": 1},
        "by_reason": {"edge_below_threshold": 1},
        "by_strategy": {"bundle_arb": 1},
    }

    data = state.to_dict()

    assert data["decisions"][0]["decision_id"] == "dec_1"
    assert data["decision_summary"]["by_outcome"] == {"skip": 1}


def test_dashboard_state_includes_active_trade_visibility_data():
    state = DashboardState()
    state.orders = [{"type": "open_order", "remaining_notional": 5.0}]
    state.paper_orders = [{"type": "paper_order", "notional": 5.0, "is_paper": True}]
    state.positions = [{"type": "position", "notional": 10.0}]
    state.active_trades = [*state.orders, *state.positions]
    state.exposure_breakdown = {
        "filled_exposure": 10.0,
        "open_order_exposure": 5.0,
        "total_active_exposure": 15.0,
    }

    data = state.to_dict()

    assert data["active_trades"] == state.active_trades
    assert data["paper_orders"] == state.paper_orders
    assert data["positions"] == state.positions
    assert data["exposure_breakdown"]["total_active_exposure"] == 15.0


def test_dashboard_state_exposes_current_run_timer_and_counters():
    state = DashboardState()
    state.run_session = {
        "run_number": 7,
        "status": "active",
        "elapsed_seconds": 125.0,
        "pnl": 3.25,
        "transaction_count": 2,
    }
    state.run_sessions = [state.run_session]

    data = state.to_dict()

    assert data["run_session"]["run_number"] == 7
    assert data["run_session"]["elapsed_seconds"] == 125.0
    assert data["run_session"]["pnl"] == 3.25
    assert data["run_session"]["transaction_count"] == 2
    assert data["run_sessions"] == [state.run_session]


def test_dashboard_state_limits_visible_paper_order_history():
    state = DashboardState()
    state.paper_orders = [
        {"type": "paper_order", "order_id": f"order-{idx}", "is_paper": True}
        for idx in range(105)
    ]

    data = state.to_dict()

    assert len(data["paper_orders"]) == 100
    assert data["paper_orders"][0]["order_id"] == "order-5"
    assert data["paper_orders"][-1]["order_id"] == "order-104"


def test_dashboard_distinguishes_missing_prices_and_matched_pair_monitoring():
    from fastapi.testclient import TestClient

    from dashboard.server import app

    page = TestClient(app).get("/").text

    assert "Matched Pairs — Monitoring" in page
    assert "pair.poly_yes ?? pair.buy_price" in page
    assert "pair.kalshi_yes ?? pair.sell_price" in page
    assert "return '—'" in page
    assert "pct > 0 && pct < 1" in page


def test_dashboard_exposes_inspectable_news_catalyst_status_and_panel():
    from fastapi.testclient import TestClient

    from dashboard.server import app

    state = DashboardState()
    state.news_catalysts["status"] = "log_only"
    state.news_catalysts["api_calls_today"] = 2

    assert state.to_dict()["news_catalysts"]["status"] == "log_only"
    page = TestClient(app).get("/").text
    assert "Today’s Catalysts" in page
    assert "updateNewsCatalysts" in page
    assert "news.source_url" in page
    assert "Transient upstream failure; retrying automatically" in page


@pytest.mark.asyncio
async def test_dashboard_integration_updates_paper_mode_fields():
    integration = DashboardIntegration(
        portfolio=Portfolio(initial_balance=100.0),
        mode="dry_run",
    )

    await integration._update_state()

    assert dashboard_state.mode == "dry_run"
    assert dashboard_state.portfolio["is_paper"] is True
    assert dashboard_state.portfolio["pnl_source"] == "paper"


@pytest.mark.asyncio
async def test_dashboard_exposes_durable_paper_evidence_ledgers(tmp_path):
    from utils.paper_trade_store import PaperTradeStore

    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        store.start_run(
            starting_equity=5_000.0,
            pnl_source="projected_locked_paper",
        )
        integration = DashboardIntegration(
            mode="dry_run",
            paper_trade_store=store,
        )

        await integration._update_state()

        assert dashboard_state.cross_platform["evaluation_ledger_count"] == 0
        assert dashboard_state.cross_platform["near_misses"] == []
        assert dashboard_state.cross_platform["paper_trade_receipts"] == []
        assert dashboard_state.cross_platform["evaluation_funnel"] == {}
    finally:
        store.close()


def test_dashboard_state_preserves_trade_timestamp():
    state = DashboardState()
    state.add_trade(
        {
            "side": "buy",
            "price": 0.5,
            "size": 10.0,
            "market_id": "market-1",
            "timestamp": "2026-06-29T14:19:00.123456Z",
        }
    )

    assert state.trades[0]["timestamp"] == "2026-06-29T14:19:00.123456Z"


def test_paper_history_api_response_shape(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.server import app, configure_dashboard_runtime, dashboard_state
    from utils.paper_trade_store import PaperTradeStore

    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        store.record_event(
            event_type="placed",
            order_id="order-1",
            market_id="market-1",
            market_question="Will the Fed cut rates today?",
            reason_code="paper_order_placed",
        )
        configure_dashboard_runtime(store=store, timezone="America/New_York")
        dashboard_state.paper_history = [
            event.to_dict() for event in store.recent_events()
        ]

        client = TestClient(app)
        response = client.get("/api/paper-history?limit=10&event_type=placed")

        assert response.status_code == 200
        payload = response.json()
        assert payload["display_timezone"] == "America/New_York"
        assert len(payload["events"]) == 1
        assert payload["events"][0]["event_type"] == "placed"
        assert payload["events"][0]["order_id"] == "order-1"
        assert (
            payload["events"][0]["market_question"] == "Will the Fed cut rates today?"
        )
    finally:
        store.close()
        configure_dashboard_runtime(store=None, timezone="America/New_York")


def test_paper_runs_api_returns_active_and_historical_counters(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.server import app, configure_dashboard_runtime, dashboard_state
    from utils.paper_trade_store import PaperTradeStore

    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        run = store.start_run(
            starting_equity=1000.0,
            pnl_source="projected_locked_paper",
        )
        store.record_event(event_type="filled", order_id="order-1", trade_id="trade-1")
        store.checkpoint_run(current_equity=1002.5, pnl=2.5)
        configure_dashboard_runtime(store=store, timezone="America/New_York")
        active = store.active_run()
        assert active is not None
        dashboard_state.run_session = active.to_dict()
        dashboard_state.run_sessions = [
            item.to_dict() for item in store.recent_runs(limit=10)
        ]

        response = TestClient(app).get("/api/paper-runs?limit=10")

        assert response.status_code == 200
        payload = response.json()
        assert payload["active_run"]["run_id"] == run.run_id
        assert payload["active_run"]["transaction_count"] == 1
        assert payload["active_run"]["pnl"] == 2.5
        assert payload["runs"][0]["run_number"] == run.run_number
    finally:
        store.close()
        configure_dashboard_runtime(store=None, timezone="America/New_York")


def test_trade_history_page_and_api(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.server import app, configure_dashboard_runtime, dashboard_state
    from utils.paper_trade_store import PaperTradeStore

    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        store.record_event(
            event_type="filled",
            order_id="order-9",
            trade_id="trade-9",
            market_id="market-9",
            market_question="Will Candidate A win the election?",
            side="buy",
            token_type="yes",
            price=0.42,
            size=12,
            notional=5.04,
            reason_code="hypothetical_paper_fill",
            reason_detail="Simulated fill",
            event_at=datetime(2026, 6, 29, 18, 0, tzinfo=timezone.utc),
        )
        dashboard_state.decisions = [
            {
                "decision_id": "dec-9",
                "strategy": "execution",
                "outcome": "trade",
                "reason_code": "hypothetical_paper_fill",
                "explanation": "Hypothetical paper fill simulated for strategy evaluation.",
                "timestamp": "2026-06-29T18:00:01Z",
                "related_id": "order-9",
                "evidence": {"order_id": "order-9", "trade_id": "trade-9"},
            }
        ]
        configure_dashboard_runtime(store=store, timezone="America/New_York")

        client = TestClient(app)
        page = client.get("/history")
        api = client.get("/api/trade-history?limit=20")

        assert page.status_code == 200
        assert "Trade History" in page.text
        assert api.status_code == 200
        payload = api.json()
        assert payload["entries"][0]["order_id"] == "order-9"
        assert (
            payload["entries"][0]["market_question"]
            == "Will Candidate A win the election?"
        )
        assert "Will Candidate A win the election?" in payload["entries"][0]["summary"]
        assert payload["entries"][0]["decisions"][0]["decision_id"] == "dec-9"
        assert payload["entries"][0]["reason_explanation"] == "Simulated fill"
    finally:
        store.close()
        configure_dashboard_runtime(store=None, timezone="America/New_York")
