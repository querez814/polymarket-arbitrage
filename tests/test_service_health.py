from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect
from fastapi.testclient import TestClient

from dashboard.server import (
    MAX_DASHBOARD_WEBSOCKETS,
    app,
    configure_dashboard_runtime,
    dashboard_state,
    get_embedded_html,
)


def test_liveness_is_public_and_independent_of_trading_admission():
    dashboard_state.is_running = False
    configure_dashboard_runtime(runtime=None, production_required=True)

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_requires_started_bot_and_owned_live_runtime_when_configured():
    client = TestClient(app)
    try:
        dashboard_state.is_running = False
        configure_dashboard_runtime(runtime=None, production_required=True)
        stopped = client.get("/health/ready")

        dashboard_state.is_running = True
        missing_runtime = client.get("/health/ready")

        configure_dashboard_runtime(
            runtime=SimpleNamespace(
                status=lambda: SimpleNamespace(
                    started=True,
                    recovery_ready=True,
                    private_stream_ready=True,
                    halted=True,
                    ready=False,
                    last_error="",
                )
            ),
            production_required=True,
        )
        halted = client.get("/health/ready")

        configure_dashboard_runtime(
            runtime=SimpleNamespace(
                status=lambda: SimpleNamespace(
                    started=True,
                    recovery_ready=True,
                    private_stream_ready=True,
                    halted=False,
                    ready=True,
                    last_error="",
                )
            ),
            production_required=True,
        )
        ready = client.get("/health/ready")

        assert stopped.status_code == 503
        assert stopped.json()["reason"] == "bot_not_running"
        assert missing_runtime.status_code == 503
        assert missing_runtime.json()["reason"] == "production_runtime_unavailable"
        assert halted.status_code == 503
        assert halted.json()["reason"] == "trading_not_admitted"
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
    finally:
        dashboard_state.is_running = False
        configure_dashboard_runtime(runtime=None, production_required=False)


def test_monitoring_only_process_does_not_require_production_runtime():
    dashboard_state.is_running = True
    configure_dashboard_runtime(runtime=None, production_required=False)
    try:
        response = TestClient(app).get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
    finally:
        dashboard_state.is_running = False


def test_readiness_fails_when_critical_background_service_is_unavailable():
    dashboard_state.is_running = True
    configure_dashboard_runtime(
        runtime=None,
        production_required=False,
        readiness_check=lambda: False,
    )
    try:
        response = TestClient(app).get("/health/ready")
        assert response.status_code == 503
        assert response.json()["reason"] == "critical_services_unavailable"
    finally:
        dashboard_state.is_running = False
        configure_dashboard_runtime(readiness_check=None)


def test_dashboard_websocket_rejects_cross_origin_browser_connections():
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(
            "/ws", headers={"origin": "https://attacker.example"}
        ):
            pass

    assert exc_info.value.code == 1008


def test_dashboard_websocket_accepts_its_own_origin():
    with TestClient(app).websocket_connect(
        "/ws", headers={"origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_json()["type"] == "initial"


def test_dashboard_websocket_has_a_bounded_connection_limit():
    assert MAX_DASHBOARD_WEBSOCKETS == 16


def test_embedded_dashboard_escapes_venue_controlled_text():
    html = get_embedded_html()

    assert "function escapeHtml(value)" in html
    assert "return escapeHtml(shortened);" in html
    assert "${escapeHtml(m.question || id)}" in html
    assert "${escapeHtml(opp.marketInfo)}" in html


def test_embedded_dashboard_shows_current_run_timer_pnl_and_transactions():
    html = get_embedded_html()

    assert 'id="runNumber"' in html
    assert 'id="runTimer"' in html
    assert 'id="runTransactions"' in html
    assert 'id="runPnl"' in html
    assert 'id="recentRuns"' in html
    assert "state.run_session || {}" in html
    assert "state.run_sessions || []" in html


def test_embedded_dashboard_distinguishes_no_matches_from_active_price_scanning():
    html = get_embedded_html()

    assert "NO VERIFIED PAIRS" in html
    assert "No equivalent cross-venue pairs passed verification this cycle." in html
    assert "Kalshi books are fetched only after a pair is verified." in html
    assert "semantic_metrics" in html
    assert "filtered_polymarket_markets" in html
    assert "filtered_kalshi_markets" in html
