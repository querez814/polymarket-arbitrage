"""
Dashboard Server
=================

FastAPI-based web server for the trading dashboard.
"""

import asyncio
from dataclasses import asdict, is_dataclass
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, TYPE_CHECKING, Optional

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from utils.time_utils import to_utc_iso, utc_now, utc_now_iso
from dashboard.trade_history import build_trade_history_payload, get_trade_history_html
from core.operations import AlertDeliveryError, OperatorAuthenticationError

if TYPE_CHECKING:
    from utils.paper_trade_store import PaperTradeStore

logger = logging.getLogger(__name__)

paper_trade_store: Optional["PaperTradeStore"] = None
display_timezone: str = "America/New_York"
production_runtime: Any = None
production_runtime_required: bool = False
critical_readiness_check: Callable[[], bool] | None = None
MAX_DASHBOARD_WEBSOCKETS = 16


def _same_origin_websocket(websocket: WebSocket) -> bool:
    """Only allow browser sockets opened by the dashboard's own origin."""
    origin = (websocket.headers.get("origin") or "").rstrip("/").lower()
    host = (websocket.headers.get("host") or "").strip().lower()
    if not origin or not host:
        return False
    return origin in {f"http://{host}", f"https://{host}"}


def configure_dashboard_runtime(
    *,
    store: Optional["PaperTradeStore"] = None,
    timezone: str = "America/New_York",
    runtime: Any = None,
    production_required: bool = False,
    readiness_check: Callable[[], bool] | None = None,
) -> None:
    """Wire runtime dependencies for dashboard API routes."""
    global paper_trade_store, display_timezone, production_runtime
    global production_runtime_required
    global critical_readiness_check
    paper_trade_store = store
    display_timezone = timezone
    production_runtime = runtime
    production_runtime_required = production_required
    critical_readiness_check = readiness_check


def _operator_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="operator bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="operator bearer token required")
    return token


def _operator_runtime() -> Any:
    if production_runtime is None:
        raise HTTPException(status_code=503, detail="production runtime is unavailable")
    return production_runtime


def _payload(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("operator response is not serializable")


def _reason(body: Mapping[str, Any]) -> str:
    value = body.get("reason")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail="reason must be non-empty text")
    return value.strip()


class DashboardState:
    """Holds the current state for the dashboard."""

    def __init__(self):
        self.markets: dict = {}
        self.opportunities: list = []
        self.signals: list = []
        self.orders: list = []
        self.paper_orders: list = []
        self.paper_history: list = []
        self.run_session: dict = {}
        self.run_sessions: list = []
        self.trades: list = []
        self.positions: list = []
        self.active_trades: list = []
        self.exposure_breakdown: dict = {}
        self.decisions: list = []
        self.decision_summary: dict = {}
        self.portfolio: dict = {}
        self.risk: dict = {}
        self.stats: dict = {}
        self.timing: dict = {}  # Opportunity timing stats
        self.operational: dict = {}  # Operational stats
        self.news_catalysts: dict = {
            "enabled": False,
            "apply_priority_boost": False,
            "status": "disabled",
            "last_scan_at": None,
            "api_calls_today": 0,
            "items": [],
            "boosted_markets": [],
        }
        self.event_week: dict = {
            "enabled": False,
            "status": "disabled",
            "last_calendar_refresh_at": None,
            "calendar_sources": [],
            "upcoming_events": [],
            "coverage": [],
            "verified_event_pairs": 0,
            "active_lanes": [],
            "scorecard": {},
        }
        self.platform_opportunity: dict = {
            "enabled": False,
            "mode": "disabled",
            "status": "disabled",
            "execution_authority": "none",
            "main_paper_fills_pnl": "disabled",
            "strategy_lanes": {
                "directional_reaction": {"authority": "disabled"},
                "relative_value": {"authority": "disabled"},
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
            "worker": {"queued": 0, "processed": 0, "dropped": 0, "failures": 0},
        }
        self.is_running: bool = False
        self.mode: str = "dry_run"
        self.last_update: datetime = utc_now()
        self.started_at: datetime = utc_now()

        # Cross-platform (Polymarket + Kalshi)
        self.cross_platform: dict = {
            "enabled": False,
            "kalshi_markets": 0,
            "polymarket_markets": 0,
            "matched_pairs": 0,
            "kalshi_orderbooks": 0,  # Number of Kalshi orderbooks fetched
            "cross_opportunities": [],
            "matched_pairs_data": [],  # Detailed data for display
            "review_candidate_count": 0,
            "review_candidates": [],
            "rules_equivalent_pairs": 0,
            "preflight_usable_pairs": 0,
            "preflight_rejections": {},
            "evaluation_funnel": {},
            "evaluation_ledger_count": 0,
            "near_misses": [],
            "paper_trade_receipts": [],
            "matching_progress": 0,  # Percentage of matching complete
            "matching_checked": 0,  # Number of comparisons done
            "matching_total": 0,  # Total comparisons to do
            "matching_status": "idle",  # idle/loading/matching/complete/no_matches/error
            "scan_status": "idle",
            "last_scan_attempt_at": None,
            "last_fresh_snapshot_at": None,
            "degraded_reason": None,
            "polymarket_connection_metrics": {},
            "kalshi_connection_metrics": {},
        }

        # WebSocket connections
        self._connections: list[WebSocket] = []

    def to_dict(self) -> dict:
        """Convert state to dictionary for JSON serialization."""
        uptime = (utc_now() - self.started_at).total_seconds()
        return {
            "markets": self.markets,
            "opportunities": self.opportunities[-50:],  # Last 50
            "signals": self.signals[-50:],
            "orders": self.orders,
            "paper_orders": self.paper_orders[-100:],
            "paper_history": self.paper_history[-200:],
            "run_session": self.run_session,
            "run_sessions": self.run_sessions[-50:],
            "trades": self.trades[-100:],  # Last 100
            "positions": self.positions,
            "active_trades": self.active_trades,
            "exposure_breakdown": self.exposure_breakdown,
            "decisions": self.decisions[-200:],
            "decision_summary": self.decision_summary,
            "portfolio": self.portfolio,
            "risk": self.risk,
            "stats": self.stats,
            "timing": self.timing,  # Opportunity timing stats
            "operational": self.operational,  # Operational stats
            "news_catalysts": self.news_catalysts,
            "event_week": self.event_week,
            "platform_opportunity": self.platform_opportunity,
            "cross_platform": self.cross_platform,  # Cross-platform arbitrage stats
            "is_running": self.is_running,
            "mode": self.mode,
            "last_update": to_utc_iso(self.last_update),
            "started_at": to_utc_iso(self.started_at),
            "display_timezone": display_timezone,
            "uptime_seconds": uptime,
        }

    async def broadcast(self, data: dict) -> None:
        """Broadcast update to all connected WebSocket clients."""
        if not self._connections:
            return

        message = json.dumps(data)
        disconnected = []

        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

        for ws in disconnected:
            self._connections.remove(ws)

    def add_opportunity(self, opportunity: dict) -> None:
        """Add a new opportunity."""
        opportunity.setdefault("timestamp", utc_now_iso())
        self.opportunities.append(opportunity)
        if len(self.opportunities) > 200:
            self.opportunities = self.opportunities[-100:]

    def add_signal(self, signal: dict) -> None:
        """Add a new signal."""
        signal.setdefault("timestamp", utc_now_iso())
        self.signals.append(signal)
        if len(self.signals) > 200:
            self.signals = self.signals[-100:]

    def add_trade(self, trade: dict) -> None:
        """Add a new trade."""
        trade.setdefault("timestamp", utc_now_iso())
        self.trades.append(trade)
        if len(self.trades) > 500:
            self.trades = self.trades[-250:]

    def add_cross_platform_opportunity(self, opportunity: dict) -> None:
        """Add a cross-platform arbitrage opportunity."""
        opportunity.setdefault("timestamp", utc_now_iso())
        self.cross_platform["cross_opportunities"].append(opportunity)
        if len(self.cross_platform["cross_opportunities"]) > 100:
            self.cross_platform["cross_opportunities"] = self.cross_platform[
                "cross_opportunities"
            ][-50:]

    def add_decision(self, decision: dict) -> None:
        """Add a decision journal record."""
        self.decisions.append(decision)
        if len(self.decisions) > 1000:
            self.decisions = self.decisions[-500:]

    def update_cross_platform_stats(
        self,
        kalshi_markets: int,
        polymarket_markets: int,
        matched_pairs: int,
        enabled: bool = True,
        matched_pairs_data: list = None,
    ) -> None:
        """Update cross-platform statistics."""
        self.cross_platform["enabled"] = enabled
        self.cross_platform["kalshi_markets"] = kalshi_markets
        self.cross_platform["polymarket_markets"] = polymarket_markets
        self.cross_platform["matched_pairs"] = matched_pairs
        if matched_pairs_data is not None:
            self.cross_platform["matched_pairs_data"] = matched_pairs_data


# Global state
dashboard_state = DashboardState()


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="Polymarket Arbitrage Dashboard",
        description="Live monitoring dashboard for the trading bot",
        version="1.0.0",
    )

    # Serve static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/health/live")
    async def health_live():
        """Process liveness only; never couples supervisor restarts to trading state."""
        return {"status": "alive"}

    @app.get("/health/ready")
    async def health_ready():
        """Traffic readiness with fail-closed production-runtime admission."""
        if not dashboard_state.is_running:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "bot_not_running"},
            )
        try:
            dependencies_ready = (
                critical_readiness_check is None or critical_readiness_check()
            )
        except Exception:
            dependencies_ready = False
        if not dependencies_ready:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": "critical_services_unavailable",
                },
            )
        if not production_runtime_required:
            return {"status": "ready"}
        if production_runtime is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": "production_runtime_unavailable",
                },
            )
        try:
            runtime_status = production_runtime.status()
            ready = bool(
                runtime_status["ready"]
                if isinstance(runtime_status, Mapping)
                else runtime_status.ready
            )
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": "runtime_status_unavailable",
                },
            )
        if not ready:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "trading_not_admitted"},
            )
        return {"status": "ready"}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Serve the main dashboard page."""
        html_path = Path(__file__).parent / "templates" / "index.html"
        if html_path.exists():
            return html_path.read_text()
        return get_embedded_html()

    @app.get("/history", response_class=HTMLResponse)
    async def trade_history_page():
        """Dedicated trade history page with expandable detail view."""
        return get_trade_history_html()

    @app.get("/api/trade-history")
    async def get_trade_history(
        limit: int = Query(default=500, ge=1, le=1000),
        event_type: Optional[str] = Query(default=None),
    ):
        """Enriched trade history for the history page."""
        if paper_trade_store is None:
            events = dashboard_state.paper_history[-limit:]
            if event_type:
                events = [
                    event for event in events if event.get("event_type") == event_type
                ]
            timeline_for_order = lambda _order_id: []
        else:
            events = [
                event.to_dict()
                for event in paper_trade_store.recent_events(
                    limit=limit, event_type=event_type
                )
            ]
            timeline_for_order = lambda order_id: [
                event.to_dict()
                for event in paper_trade_store.events_for_order(order_id)
            ]

        return build_trade_history_payload(
            events=events,
            decisions=dashboard_state.decisions[-1000:],
            display_timezone=display_timezone,
            mode=dashboard_state.mode,
            timeline_for_order=timeline_for_order,
        )

    @app.get("/api/state")
    async def get_state():
        """Get current dashboard state."""
        return dashboard_state.to_dict()

    @app.get("/api/markets")
    async def get_markets():
        """Get current market data."""
        return {"markets": dashboard_state.markets}

    @app.get("/api/opportunities")
    async def get_opportunities():
        """Get recent opportunities."""
        return {"opportunities": dashboard_state.opportunities[-50:]}

    @app.get("/api/portfolio")
    async def get_portfolio():
        """Get portfolio state."""
        return dashboard_state.portfolio

    @app.get("/api/risk")
    async def get_risk():
        """Get risk metrics."""
        return dashboard_state.risk

    @app.get("/api/decisions")
    async def get_decisions():
        """Get recent decision journal records."""
        return {
            "decisions": dashboard_state.decisions[-200:],
            "summary": dashboard_state.decision_summary,
        }

    @app.get("/api/timing")
    async def get_timing():
        """Get opportunity timing statistics."""
        return dashboard_state.timing

    @app.get("/api/operator/status")
    async def get_operator_status(authorization: str | None = Header(default=None)):
        runtime = _operator_runtime()
        token = _operator_token(authorization)
        try:
            operator = runtime.operator_status(token)
        except OperatorAuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"operator": _payload(operator), "runtime": _payload(runtime.status())}

    @app.post("/api/operator/panic")
    async def panic_operator(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ):
        runtime = _operator_runtime()
        token = _operator_token(authorization)
        try:
            status = await runtime.panic(token, reason=_reason(body))
        except OperatorAuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"operator": _payload(status), "runtime": _payload(runtime.status())}

    @app.post("/api/operator/resume")
    async def resume_operator(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ):
        runtime = _operator_runtime()
        token = _operator_token(authorization)
        try:
            status = await runtime.resume(token, reason=_reason(body))
        except OperatorAuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (AlertDeliveryError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"operator": _payload(status), "runtime": _payload(runtime.status())}

    @app.get("/api/paper-history")
    async def get_paper_history(
        limit: int = Query(default=200, ge=1, le=1000),
        event_type: Optional[str] = Query(default=None),
    ):
        """Get persisted paper trade lifecycle events."""
        if paper_trade_store is None:
            return {
                "events": dashboard_state.paper_history[-limit:],
                "display_timezone": display_timezone,
            }

        events = paper_trade_store.recent_events(limit=limit, event_type=event_type)
        return {
            "events": [event.to_dict() for event in events],
            "display_timezone": display_timezone,
        }

    @app.get("/api/paper-runs")
    async def get_paper_runs(limit: int = Query(default=50, ge=1, le=200)):
        """Get persisted paper application-run summaries."""
        return {
            "runs": dashboard_state.run_sessions[:limit],
            "active_run": dashboard_state.run_session or None,
            "display_timezone": display_timezone,
        }

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time updates."""
        if not _same_origin_websocket(websocket):
            await websocket.close(code=1008, reason="untrusted websocket origin")
            return
        if len(dashboard_state._connections) >= MAX_DASHBOARD_WEBSOCKETS:
            await websocket.close(
                code=1013, reason="dashboard connection limit reached"
            )
            return
        await websocket.accept()
        dashboard_state._connections.append(websocket)

        try:
            # Send initial state
            await websocket.send_text(
                json.dumps({"type": "initial", "data": dashboard_state.to_dict()})
            )

            # Keep connection alive and receive any commands
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(), timeout=30.0
                    )
                    # Handle any commands from client
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        await websocket.send_text(json.dumps({"type": "pong"}))
                except asyncio.TimeoutError:
                    # Send heartbeat
                    await websocket.send_text(json.dumps({"type": "heartbeat"}))

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            if websocket in dashboard_state._connections:
                dashboard_state._connections.remove(websocket)

    return app


def get_embedded_html() -> str:
    """Return embedded HTML for the dashboard."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket Arbitrage Dashboard</title>
    <style>
        :root {
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-card: #1a1a24;
            --border-color: #2a2a3a;
            --text-primary: #e0e0e0;
            --text-secondary: #888;
            --accent-green: #00ff88;
            --accent-red: #ff4466;
            --accent-blue: #4488ff;
            --accent-yellow: #ffaa00;
            --accent-purple: #aa66ff;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', monospace;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            overflow-x: hidden;
        }
        
        .header {
            background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-card) 100%);
            border-bottom: 1px solid var(--border-color);
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .logo {
            font-size: 1.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent-green) 0%, var(--accent-blue) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .status {
            display: flex;
            align-items: center;
            gap: 1rem;
        }
        
        .status-indicator {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background: var(--bg-card);
            border-radius: 8px;
            border: 1px solid var(--border-color);
        }
        
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        .status-dot.running {
            background: var(--accent-green);
            box-shadow: 0 0 10px var(--accent-green);
        }
        
        .status-dot.stopped {
            background: var(--accent-red);
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .mode-badge {
            padding: 0.25rem 0.75rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .mode-badge.dry-run {
            background: var(--accent-yellow);
            color: #000;
        }
        
        .mode-badge.live {
            background: var(--accent-red);
            color: #fff;
        }
        
        .dashboard {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            grid-template-rows: auto auto 1fr;
            gap: 1rem;
            padding: 1rem;
            max-width: 1800px;
            margin: 0 auto;
        }
        
        .card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
        }
        
        .card-header {
            padding: 1rem;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .card-title {
            font-size: 0.875rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
        }
        
        .card-body {
            padding: 1rem;
        }
        
        /* Metric Cards */
        .metrics {
            grid-column: span 4;
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 1rem;
        }
        
        .metric-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1.25rem;
            text-align: center;
        }
        
        .metric-label {
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 0.5rem;
        }
        
        .metric-value {
            font-size: 1.75rem;
            font-weight: 700;
        }
        
        .metric-value.positive {
            color: var(--accent-green);
        }
        
        .metric-value.negative {
            color: var(--accent-red);
        }
        
        .metric-value.neutral {
            color: var(--accent-blue);
        }
        
        .metric-change {
            font-size: 0.75rem;
            margin-top: 0.25rem;
        }
        
        /* Opportunities Card */
        .opportunities-card {
            grid-column: span 2;
            grid-row: span 2;
        }
        
        .opportunity-list {
            max-height: 400px;
            overflow-y: auto;
        }
        
        .opportunity-item {
            padding: 0.75rem;
            border-bottom: 1px solid var(--border-color);
            display: grid;
            grid-template-columns: auto 1fr auto;
            gap: 0.75rem;
            align-items: center;
        }
        
        .opportunity-item:last-child {
            border-bottom: none;
        }
        
        .opportunity-type {
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .opportunity-type.bundle-long {
            background: rgba(0, 255, 136, 0.2);
            color: var(--accent-green);
        }
        
        .opportunity-type.bundle-short {
            background: rgba(255, 68, 102, 0.2);
            color: var(--accent-red);
        }
        
        .opportunity-type.mm {
            background: rgba(68, 136, 255, 0.2);
            color: var(--accent-blue);
        }
        
        .opportunity-details {
            font-size: 0.8rem;
        }
        
        .opportunity-market {
            color: var(--text-primary);
            margin-bottom: 0.25rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 250px;
        }
        
        .opportunity-edge {
            color: var(--accent-green);
            font-weight: 600;
        }
        
        .opportunity-time {
            font-size: 0.7rem;
            color: var(--text-secondary);
        }
        
        /* Portfolio Card */
        .portfolio-card {
            grid-column: span 2;
        }
        
        .position-list {
            max-height: 200px;
            overflow-y: auto;
        }
        
        .position-item {
            display: grid;
            grid-template-columns: 1fr auto auto;
            gap: 1rem;
            padding: 0.5rem 0;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.85rem;
        }
        
        .position-item:last-child {
            border-bottom: none;
        }
        
        /* Risk Card */
        .risk-card {
            grid-column: span 2;
        }
        
        .risk-bar {
            height: 8px;
            background: var(--bg-secondary);
            border-radius: 4px;
            overflow: hidden;
            margin: 0.5rem 0;
        }
        
        .risk-bar-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.3s ease;
        }
        
        .risk-bar-fill.safe {
            background: var(--accent-green);
        }
        
        .risk-bar-fill.warning {
            background: var(--accent-yellow);
        }
        
        .risk-bar-fill.danger {
            background: var(--accent-red);
        }
        
        .risk-item {
            margin-bottom: 1rem;
        }
        
        .risk-label {
            display: flex;
            justify-content: space-between;
            font-size: 0.8rem;
            margin-bottom: 0.25rem;
        }
        
        /* Activity Feed */
        .activity-card {
            grid-column: span 2;
            grid-row: span 2;
        }
        
        .activity-list {
            max-height: 400px;
            overflow-y: auto;
        }
        
        .activity-item {
            padding: 0.5rem;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.8rem;
            display: flex;
            gap: 0.75rem;
            align-items: flex-start;
        }
        
        .activity-icon {
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.7rem;
            flex-shrink: 0;
        }
        
        .activity-icon.order {
            background: rgba(68, 136, 255, 0.2);
            color: var(--accent-blue);
        }
        
        .activity-icon.fill {
            background: rgba(0, 255, 136, 0.2);
            color: var(--accent-green);
        }
        
        .activity-icon.cancel {
            background: rgba(255, 68, 102, 0.2);
            color: var(--accent-red);
        }
        
        .activity-icon.signal {
            background: rgba(170, 102, 255, 0.2);
            color: var(--accent-purple);
        }
        
        .activity-content {
            flex: 1;
        }
        
        .activity-message {
            color: var(--text-primary);
        }
        
        .activity-time {
            color: var(--text-secondary);
            font-size: 0.7rem;
        }

        /* Paper Trade History */
        .paper-history-card {
            grid-column: span 4;
        }

        .paper-history-toolbar {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
        }

        .paper-history-filter {
            border: 1px solid var(--border-color);
            background: var(--bg-secondary);
            color: var(--text-primary);
            border-radius: 999px;
            padding: 0.25rem 0.65rem;
            font-size: 0.7rem;
            cursor: pointer;
        }

        .paper-history-filter.active {
            border-color: var(--accent-blue);
            color: var(--accent-blue);
        }

        .paper-history-table-wrap {
            max-height: 420px;
            overflow: auto;
        }

        .paper-history-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.72rem;
        }

        .paper-history-table th,
        .paper-history-table td {
            padding: 0.45rem 0.5rem;
            border-bottom: 1px solid var(--border-color);
            text-align: left;
            vertical-align: top;
        }

        .paper-history-table th {
            position: sticky;
            top: 0;
            background: var(--bg-secondary);
            color: var(--text-secondary);
            z-index: 1;
        }

        .event-badge {
            display: inline-block;
            padding: 0.1rem 0.45rem;
            border-radius: 999px;
            font-size: 0.65rem;
            font-weight: 600;
            text-transform: uppercase;
        }

        .event-badge.placed { background: rgba(68, 136, 255, 0.18); color: var(--accent-blue); }
        .event-badge.filled { background: rgba(0, 255, 136, 0.18); color: var(--accent-green); }
        .event-badge.rejected { background: rgba(255, 68, 102, 0.18); color: var(--accent-red); }
        .event-badge.cancelled { background: rgba(255, 170, 0, 0.18); color: #ffaa00; }
        .event-badge.expired { background: rgba(170, 102, 255, 0.18); color: var(--accent-purple); }
        
        /* Decision Journal */
        .decision-card {
            grid-column: span 4;
        }
        
        .decision-toolbar {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
        }
        
        .decision-filter {
            border: 1px solid var(--border-color);
            background: var(--bg-secondary);
            color: var(--text-primary);
            border-radius: 999px;
            padding: 0.25rem 0.65rem;
            font-size: 0.7rem;
            cursor: pointer;
        }
        
        .decision-filter.active {
            border-color: var(--accent-green);
            color: var(--accent-green);
        }
        
        .decision-list {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
            gap: 0.75rem;
            max-height: 520px;
            overflow-y: auto;
        }
        
        .decision-item {
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-left: 4px solid var(--accent-blue);
            border-radius: 10px;
            padding: 0.85rem;
        }
        
        .decision-item.trade { border-left-color: var(--accent-green); }
        .decision-item.skip { border-left-color: var(--accent-yellow); }
        .decision-item.reject, .decision-item.error { border-left-color: var(--accent-red); }
        
        .decision-topline {
            display: flex;
            justify-content: space-between;
            gap: 0.75rem;
            margin-bottom: 0.5rem;
        }
        
        .decision-badge {
            text-transform: uppercase;
            font-size: 0.65rem;
            font-weight: 700;
            color: var(--text-primary);
            background: var(--bg-primary);
            border-radius: 4px;
            padding: 0.2rem 0.45rem;
        }
        
        .decision-title {
            font-size: 0.82rem;
            font-weight: 600;
            line-height: 1.35;
            margin-bottom: 0.45rem;
        }
        
        .decision-explanation {
            color: var(--text-secondary);
            font-size: 0.75rem;
            line-height: 1.4;
            margin-bottom: 0.55rem;
        }
        
        .decision-evidence {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.35rem;
            font-size: 0.68rem;
            color: var(--text-secondary);
        }
        
        .decision-evidence span {
            background: var(--bg-primary);
            border-radius: 4px;
            padding: 0.25rem;
        }
        
        /* Operational Stats Card */
        .operational-card {
            grid-column: span 2;
        }
        
        .op-stats-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.75rem;
        }
        
        .op-stat {
            background: var(--bg-secondary);
            padding: 1rem;
            border-radius: 8px;
            text-align: center;
        }
        
        .op-stat-value {
            font-size: 1.5rem;
            font-weight: 700;
            color: var(--accent-blue);
        }
        
        .op-stat-value.active {
            color: var(--accent-green);
        }
        
        .op-stat-label {
            font-size: 0.7rem;
            color: var(--text-secondary);
        }
        
        /* Cross-Platform Arbitrage Card */
        .cross-platform-card {
            grid-column: span 2;
            border: 1px solid rgba(255, 165, 0, 0.3);
        }
        
        .cross-platform-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .cross-platform-badge {
            background: linear-gradient(135deg, #ff6b35, #f7931a);
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            font-size: 0.7rem;
            font-weight: 600;
        }
        
        /* Live Opportunities Feed */
        .opportunities-feed {
            grid-column: span 2;
            max-height: 600px;
            overflow-y: auto;
        }

        .opp-card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 1rem;
            margin-bottom: 0.75rem;
            border-left: 4px solid var(--accent-green);
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .opp-card:hover {
            transform: translateX(4px);
            box-shadow: 0 4px 12px rgba(0, 255, 136, 0.1);
        }
        
        .opp-card.cross-platform {
            border-left-color: #f7931a;
        }
        
        .opp-card.polymarket {
            border-left-color: #8b5cf6;
        }
        
        .opp-card.kalshi {
            border-left-color: #3b82f6;
        }

        .opp-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0.75rem;
        }
        
        .opp-category {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .opp-badge {
            background: var(--bg-primary);
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
        }
        
        .opp-badge.nfl { background: #1a472a; color: #4ade80; }
        .opp-badge.nba { background: #1e3a5f; color: #60a5fa; }
        .opp-badge.politics { background: #4a1d6a; color: #c084fc; }
        .opp-badge.crypto { background: #5c4b1a; color: #fbbf24; }
        .opp-badge.soccer { background: #1a3d3d; color: #2dd4bf; }
        .opp-badge.cross { background: #5c3d1a; color: #f7931a; }
        
        .opp-edge {
            font-size: 1.1rem;
            font-weight: 700;
            color: var(--accent-green);
        }
        
        .opp-title {
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 0.5rem;
        }
        
        .opp-market-info {
            font-size: 0.7rem;
            color: var(--text-muted);
            margin-bottom: 0.75rem;
        }
        
        .opp-platforms {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }
        
        .opp-platform-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.5rem;
            background: var(--bg-primary);
            border-radius: 6px;
        }
        
        .opp-platform-name {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.8rem;
            font-weight: 500;
        }
        
        .opp-platform-icon {
            width: 20px;
            height: 20px;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.6rem;
            font-weight: 700;
        }
        
        .opp-platform-icon.poly { background: #8b5cf6; }
        .opp-platform-icon.kalshi { background: #f7931a; }
        
        .opp-platform-price {
            font-size: 0.9rem;
            font-weight: 700;
        }
        
        .opp-platform-price.buy { color: var(--accent-green); }
        .opp-platform-price.sell { color: #ef4444; }
        
        .opp-status {
            display: flex;
            align-items: center;
            gap: 0.25rem;
            font-size: 0.65rem;
            color: var(--accent-green);
        }
        
        .opp-status-dot {
            width: 6px;
            height: 6px;
            background: var(--accent-green);
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        
        .no-opportunities {
            text-align: center;
            padding: 2rem;
            color: var(--text-muted);
        }
        
        .platform-stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 0.75rem;
            margin-bottom: 1rem;
        }
        
        .platform-stat {
            background: var(--bg-secondary);
            padding: 0.75rem;
            border-radius: 8px;
            text-align: center;
        }
        
        .platform-stat-value {
            font-size: 1.25rem;
            font-weight: 700;
        }
        
        .platform-stat-value.polymarket {
            color: #8b5cf6;
        }
        
        .platform-stat-value.kalshi {
            color: #f7931a;
        }
        
        .platform-stat-value.matched {
            color: var(--accent-green);
        }
        
        .platform-stat-value.cross-opp {
            color: #ff6b35;
        }
        
        .platform-stat-label {
            font-size: 0.65rem;
            color: var(--text-secondary);
            margin-top: 0.25rem;
        }
        
        .platform-stat-status {
            font-size: 0.6rem;
            margin-top: 0.35rem;
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            background: var(--bg-tertiary);
        }
        
        .platform-stat-status.loading {
            color: var(--accent-yellow);
            animation: pulse 1.5s infinite;
        }
        
        .platform-stat-status.ready {
            color: var(--accent-green);
        }
        
        .platform-stat-status.scanning {
            color: var(--accent-blue);
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .cross-opp-list {
            max-height: 200px;
            overflow-y: auto;
        }
        
        .cross-opp-item {
            display: grid;
            grid-template-columns: auto 1fr auto auto;
            gap: 1rem;
            padding: 0.5rem;
            border-bottom: 1px solid var(--border-color);
            align-items: center;
            font-size: 0.8rem;
        }
        
        .cross-opp-direction {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }
        
        .cross-opp-platform {
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            font-size: 0.65rem;
            font-weight: 600;
        }
        
        .cross-opp-platform.buy {
            background: rgba(0, 255, 136, 0.2);
            color: var(--accent-green);
        }
        
        .cross-opp-platform.sell {
            background: rgba(255, 68, 102, 0.2);
            color: var(--accent-red);
        }
        
        /* Matched Pairs Cards - Like reference design */
        .matched-pairs-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 1rem;
            max-height: 400px;
            overflow-y: auto;
            padding: 0.5rem;
        }
        
        .pair-card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 1rem;
            border: 1px solid var(--border-color);
            transition: all 0.2s ease;
        }
        
        .pair-card:hover {
            border-color: var(--accent-blue);
            transform: translateY(-2px);
        }
        
        .pair-card.has-arb {
            border-color: var(--accent-green);
            box-shadow: 0 0 20px rgba(0, 255, 136, 0.15);
        }
        
        .pair-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0.75rem;
        }
        
        .pair-sport-badge {
            background: linear-gradient(135deg, #4488ff, #2266cc);
            padding: 0.2rem 0.5rem;
            border-radius: 6px;
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
        }
        
        .pair-arb-badge {
            background: linear-gradient(135deg, #00ff88, #00cc66);
            color: #000;
            padding: 0.2rem 0.6rem;
            border-radius: 6px;
            font-size: 0.65rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.25rem;
        }
        
        .pair-title {
            font-weight: 600;
            font-size: 0.9rem;
            margin-bottom: 0.75rem;
            line-height: 1.3;
            color: var(--text-primary);
        }
        
        .pair-platforms {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 0.75rem;
        }
        
        .platform-box {
            background: var(--bg-tertiary);
            border-radius: 8px;
            padding: 0.6rem;
            text-align: center;
        }
        
        .platform-name {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.4rem;
            margin-bottom: 0.5rem;
        }
        
        .platform-name .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }
        
        .platform-name .dot.polymarket {
            background: #8b5cf6;
        }
        
        .platform-name .dot.kalshi {
            background: #f7931a;
        }
        
        .platform-name span {
            font-size: 0.75rem;
            font-weight: 600;
        }
        
        .platform-prices {
            display: flex;
            justify-content: center;
            gap: 0.5rem;
            font-size: 0.85rem;
        }
        
        .platform-prices .yes {
            color: var(--accent-green);
            font-weight: 700;
        }
        
        .platform-prices .no {
            color: var(--accent-red);
            font-weight: 700;
        }
        
        .platform-prices .divider {
            color: var(--text-secondary);
        }
        
        .pair-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 0.75rem;
            padding-top: 0.5rem;
            border-top: 1px solid var(--border-color);
            font-size: 0.7rem;
            color: var(--text-secondary);
        }
        
        .pair-edge {
            font-weight: 700;
            font-size: 0.85rem;
        }
        
        .pair-edge.positive {
            color: var(--accent-green);
        }
        
        .pair-edge.negative {
            color: var(--text-secondary);
        }
        
        .uptime-display {
            text-align: center;
            padding: 1rem;
            margin-top: 0.75rem;
            background: var(--bg-secondary);
            border-radius: 8px;
        }
        
        .uptime-value {
            font-size: 1.25rem;
            font-weight: 600;
            font-family: 'JetBrains Mono', monospace;
            color: var(--accent-green);
        }

        .run-session-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 0.75rem;
            margin-top: 0.75rem;
        }

        .run-session-stat {
            padding: 0.75rem;
            background: var(--bg-secondary);
            border-radius: 8px;
            text-align: center;
        }

        .run-session-label {
            color: var(--text-secondary);
            font-size: 0.65rem;
            letter-spacing: 0.08em;
        }

        .run-session-value {
            color: var(--accent-green);
            font-family: 'JetBrains Mono', monospace;
            font-size: 1rem;
            font-weight: 700;
            margin-top: 0.25rem;
        }

        .recent-runs {
            margin-top: 0.75rem;
            display: grid;
            gap: 0.35rem;
        }

        .recent-run-row {
            display: grid;
            grid-template-columns: 0.7fr 1fr 1fr 1fr 1fr;
            gap: 0.5rem;
            align-items: center;
            color: var(--text-secondary);
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.7rem;
            padding: 0.45rem 0.65rem;
            border: 1px solid var(--border-color);
            border-radius: 6px;
        }
        
        /* Timing Card */
        .timing-card {
            grid-column: span 2;
        }
        
        .timing-stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 1rem;
        }
        
        .timing-stat {
            text-align: center;
            padding: 0.75rem;
            background: var(--bg-secondary);
            border-radius: 8px;
        }
        
        .timing-stat-value {
            font-size: 1.5rem;
            font-weight: 700;
        }
        
        .timing-stat-value.fast {
            color: var(--accent-green);
        }
        
        .timing-stat-value.medium {
            color: var(--accent-yellow);
        }
        
        .timing-stat-value.slow {
            color: var(--accent-red);
        }
        
        .timing-stat-label {
            font-size: 0.7rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            margin-top: 0.25rem;
        }
        
        .timing-buckets {
            display: flex;
            gap: 0.5rem;
            margin-top: 1rem;
        }
        
        .timing-bucket {
            flex: 1;
            text-align: center;
            padding: 0.5rem;
            background: var(--bg-secondary);
            border-radius: 6px;
            font-size: 0.75rem;
        }
        
        .timing-bucket-count {
            font-size: 1.25rem;
            font-weight: 600;
            display: block;
        }
        
        .timing-bucket.fast .timing-bucket-count {
            color: var(--accent-green);
        }
        
        .timing-bucket.medium .timing-bucket-count {
            color: var(--accent-blue);
        }
        
        .timing-bucket.slow .timing-bucket-count {
            color: var(--accent-yellow);
        }
        
        .timing-bucket.very-slow .timing-bucket-count {
            color: var(--accent-red);
        }
        
        .timing-recent {
            margin-top: 1rem;
            max-height: 150px;
            overflow-y: auto;
        }
        
        .timing-recent-item {
            display: flex;
            justify-content: space-between;
            padding: 0.25rem 0;
            font-size: 0.75rem;
            border-bottom: 1px solid var(--border-color);
        }
        
        .timing-duration {
            font-weight: 600;
        }
        
        .timing-duration.fast { color: var(--accent-green); }
        .timing-duration.medium { color: var(--accent-yellow); }
        .timing-duration.slow { color: var(--accent-red); }
        
        /* Markets Card */
        .markets-card {
            grid-column: span 2;
        }
        
        .market-item {
            display: grid;
            grid-template-columns: 1fr auto auto auto;
            gap: 1rem;
            padding: 0.75rem;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.85rem;
        }
        
        .market-name {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .market-price {
            font-weight: 600;
        }
        
        .market-spread {
            color: var(--text-secondary);
        }
        
        /* Scrollbar */
        ::-webkit-scrollbar {
            width: 6px;
        }
        
        ::-webkit-scrollbar-track {
            background: var(--bg-secondary);
        }
        
        ::-webkit-scrollbar-thumb {
            background: var(--border-color);
            border-radius: 3px;
        }
        
        ::-webkit-scrollbar-thumb:hover {
            background: var(--text-secondary);
        }
        
        /* Empty State */
        .empty-state {
            text-align: center;
            padding: 2rem;
            color: var(--text-secondary);
        }
        
        .empty-icon {
            font-size: 2rem;
            margin-bottom: 0.5rem;
        }
        
        /* Connection Status */
        .connection-status {
            position: fixed;
            bottom: 1rem;
            right: 1rem;
            padding: 0.5rem 1rem;
            border-radius: 8px;
            font-size: 0.75rem;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
        }
        
        .connection-status.connected {
            border-color: var(--accent-green);
        }

        .connection-status.degraded {
            border-color: #f59e0b;
            color: #fbbf24;
        }
        
        .connection-status.disconnected {
            border-color: var(--accent-red);
        }
        
        @media (max-width: 1400px) {
            .dashboard {
                grid-template-columns: repeat(2, 1fr);
            }
            
            .metrics {
                grid-column: span 2;
                grid-template-columns: repeat(3, 1fr);
            }
            
            .opportunities-card,
            .activity-card {
                grid-column: span 2;
                grid-row: span 1;
            }
            
            .portfolio-card,
            .risk-card,
            .markets-card {
                grid-column: span 2;
            }
        }
    </style>
</head>
<body>
    <header class="header">
        <div class="logo">⚡ Polymarket Arbitrage</div>
        <div class="status">
            <a href="/history" style="color: var(--text-secondary); text-decoration:none; font-size:0.85rem; padding:0.45rem 0.75rem; border:1px solid var(--border-color); border-radius:8px;">Trade History</a>
            <div class="status-indicator">
                <span class="status-dot" id="statusDot"></span>
                <span id="statusText">Connecting...</span>
            </div>
            <span class="mode-badge" id="modeBadge">DRY RUN</span>
        </div>
    </header>
    
    <main class="dashboard">
        <!-- Metrics Row -->
        <section class="metrics">
            <div class="metric-card">
                <div class="metric-label">Total PnL</div>
                <div class="metric-value" id="totalPnl">$0.00</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Realized PnL</div>
                <div class="metric-value" id="realizedPnl">$0.00</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Exposure</div>
                <div class="metric-value neutral" id="exposure">$0.00</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Open Orders</div>
                <div class="metric-value neutral" id="openOrders">0</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Opportunities</div>
                <div class="metric-value neutral" id="opportunityCount">0</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Win Rate</div>
                <div class="metric-value" id="winRate">0%</div>
            </div>
        </section>
        
        <!-- Opportunities -->
        <section class="card opportunities-card">
            <div class="card-header">
                <span class="card-title">Live Opportunities</span>
                <span id="oppRefresh" style="font-size: 0.7rem; color: var(--text-secondary);"></span>
            </div>
            <div class="card-body">
                <div class="opportunity-list" id="opportunityList">
                    <div class="empty-state">
                        <div class="empty-icon">📊</div>
                        <div>Waiting for opportunities...</div>
                    </div>
                </div>
            </div>
        </section>
        
        <!-- Activity Feed -->
        <section class="card activity-card">
            <div class="card-header">
                <span class="card-title">Activity Feed</span>
            </div>
            <div class="card-body">
                <div class="activity-list" id="activityList">
                    <div class="empty-state">
                        <div class="empty-icon">📝</div>
                        <div>No activity yet...</div>
                    </div>
                </div>
            </div>
        </section>

        <!-- Paper Trade History -->
        <section class="card paper-history-card">
            <div class="card-header">
                <span class="card-title">Paper Trade History · <a href="/history" style="color:var(--accent-blue); text-decoration:none; font-size:0.75rem;">Open full page</a></span>
                <div class="paper-history-toolbar">
                    <button class="paper-history-filter active" data-paper-filter="all">All</button>
                    <button class="paper-history-filter" data-paper-filter="placed">Placed</button>
                    <button class="paper-history-filter" data-paper-filter="filled">Filled</button>
                    <button class="paper-history-filter" data-paper-filter="rejected">Rejected</button>
                    <button class="paper-history-filter" data-paper-filter="cancelled">Cancelled</button>
                    <button class="paper-history-filter" data-paper-filter="expired">Expired</button>
                </div>
            </div>
            <div class="card-body">
                <div class="paper-history-table-wrap">
                    <table class="paper-history-table">
                        <thead>
                            <tr>
                                <th>Date/Time (ET)</th>
                                <th>Event</th>
                                <th>Market</th>
                                <th>Side</th>
                                <th>Token</th>
                                <th>Price</th>
                                <th>Size</th>
                                <th>Notional</th>
                                <th>Strategy</th>
                                <th>Order ID</th>
                                <th>Reason</th>
                            </tr>
                        </thead>
                        <tbody id="paperHistoryBody">
                            <tr>
                                <td colspan="11">Waiting for paper trade events...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </section>
        
        <!-- Decision Journal -->
        <section class="card decision-card">
            <div class="card-header">
                <span class="card-title">Decision Journal</span>
                <div class="decision-toolbar">
                    <button class="decision-filter active" data-filter="all">All</button>
                    <button class="decision-filter" data-filter="trade">Trade</button>
                    <button class="decision-filter" data-filter="skip">Skip</button>
                    <button class="decision-filter" data-filter="reject">Reject</button>
                    <button class="decision-filter" data-filter="cross_platform">Cross-platform</button>
                    <button class="decision-filter" data-filter="bundle_arb">Bundle</button>
                    <button class="decision-filter" data-filter="market_making">Market-making</button>
                </div>
            </div>
            <div class="card-body">
                <div class="decision-list" id="decisionList">
                    <div class="empty-state">
                        <div class="empty-icon">Log</div>
                        <div>Waiting for decision records...</div>
                    </div>
                </div>
            </div>
        </section>
        
        <!-- Portfolio -->
        <section class="card portfolio-card">
            <div class="card-header">
                <span class="card-title">Positions</span>
            </div>
            <div class="card-body">
                <div class="position-list" id="positionList">
                    <div class="empty-state">
                        <div class="empty-icon">💼</div>
                        <div>No open positions</div>
                    </div>
                </div>
            </div>
        </section>
        
        <!-- Risk -->
        <section class="card risk-card">
            <div class="card-header">
                <span class="card-title">Risk Metrics</span>
            </div>
            <div class="card-body">
                <div class="risk-item">
                    <div class="risk-label">
                        <span>Global Exposure</span>
                        <span id="riskExposure">$0 / $5,000</span>
                    </div>
                    <div class="risk-bar">
                        <div class="risk-bar-fill safe" id="exposureBar" style="width: 0%"></div>
                    </div>
                </div>
                <div class="risk-item">
                    <div class="risk-label">
                        <span>Daily P&L</span>
                        <span id="riskDailyPnl">$0 / -$500</span>
                    </div>
                    <div class="risk-bar">
                        <div class="risk-bar-fill safe" id="dailyPnlBar" style="width: 0%"></div>
                    </div>
                </div>
                <div class="risk-item">
                    <div class="risk-label">
                        <span>Drawdown</span>
                        <span id="riskDrawdown">0% / 10%</span>
                    </div>
                    <div class="risk-bar">
                        <div class="risk-bar-fill safe" id="drawdownBar" style="width: 0%"></div>
                    </div>
                </div>
                <div id="killSwitch" style="display: none; padding: 0.75rem; background: rgba(255,68,102,0.2); border-radius: 8px; text-align: center; color: var(--accent-red); font-weight: 600;">
                    ⚠️ KILL SWITCH ACTIVE
                </div>
            </div>
        </section>
        
        <!-- Operational Stats -->
        <section class="card operational-card">
            <div class="card-header">
                <span class="card-title">🔄 Operational Stats</span>
                <span id="streamStatus" style="font-size: 0.75rem; color: var(--accent-green);">● Streaming</span>
            </div>
            <div class="card-body">
                <div class="op-stats-grid">
                    <div class="op-stat">
                        <div class="op-stat-value" id="totalMarkets">0</div>
                        <div class="op-stat-label">Total Markets</div>
                    </div>
                    <div class="op-stat">
                        <div class="op-stat-value" id="marketsWithData">0</div>
                        <div class="op-stat-label">With Order Books</div>
                    </div>
                    <div class="op-stat">
                        <div class="op-stat-value" id="marketsWithPrices">0</div>
                        <div class="op-stat-label">With Prices</div>
                    </div>
                    <div class="op-stat">
                        <div class="op-stat-value active" id="orderbookUpdates">0</div>
                        <div class="op-stat-label">Orderbook Updates</div>
                    </div>
                    <div class="op-stat">
                        <div class="op-stat-value" id="cycleTime">--</div>
                        <div class="op-stat-label">Est. Cycle Time</div>
                    </div>
                    <div class="op-stat">
                        <div class="op-stat-value" id="updatesPerMin">0</div>
                        <div class="op-stat-label">Updates/Min</div>
                    </div>
                </div>
                <div class="uptime-display">
                    <span style="color: var(--text-secondary); font-size: 0.75rem;">PROCESS UPTIME: </span>
                    <span class="uptime-value" id="uptime">00:00:00</span>
                </div>
                <div class="run-session-grid">
                    <div class="run-session-stat">
                        <div class="run-session-label">RUN</div>
                        <div class="run-session-value" id="runNumber">--</div>
                    </div>
                    <div class="run-session-stat">
                        <div class="run-session-label">RUN TIMER</div>
                        <div class="run-session-value" id="runTimer">00:00:00</div>
                    </div>
                    <div class="run-session-stat">
                        <div class="run-session-label">TRANSACTIONS</div>
                        <div class="run-session-value" id="runTransactions">0</div>
                    </div>
                    <div class="run-session-stat">
                        <div class="run-session-label">PROJECTED RUN PNL</div>
                        <div class="run-session-value" id="runPnl">$0.00</div>
                    </div>
                </div>
                <div class="recent-runs" id="recentRuns"></div>
            </div>
        </section>
        
        <!-- Cross-Platform Arbitrage (Polymarket + Kalshi) -->
        <section class="card cross-platform-card" id="crossPlatformCard">
            <div class="card-header cross-platform-header">
                <span class="card-title">🔀 Cross-Platform Arbitrage</span>
                <span class="cross-platform-badge" id="crossPlatformStatus">DISABLED</span>
            </div>
            <div class="card-body">
                <!-- Matching Progress Bar -->
                <div id="matchingProgressContainer" style="margin-bottom: 1rem; display: none;">
                    <div style="display: flex; justify-content: space-between; margin-bottom: 0.25rem;">
                        <span style="font-size: 0.8rem; color: var(--text-muted);">🔍 Matching markets by category...</span>
                        <span id="matchingProgressText" style="font-size: 0.8rem; color: #f7931a;">0%</span>
                    </div>
                    <div style="background: var(--bg-secondary); border-radius: 4px; height: 8px; overflow: hidden;">
                        <div id="matchingProgressBar" style="background: linear-gradient(90deg, #f7931a, #ff6b35); height: 100%; width: 0%; transition: width 0.3s;"></div>
                    </div>
                    <div id="matchingStats" style="font-size: 0.7rem; color: var(--text-muted); margin-top: 0.25rem;">
                        Checked: 0 / 0 comparisons | Found: 0 matches
                    </div>
                </div>
                
                <div class="platform-stats" style="grid-template-columns: repeat(5, 1fr);">
                    <div class="platform-stat">
                        <div class="platform-stat-value polymarket" id="polymarketMarkets">0</div>
                        <div class="platform-stat-label">Polymarket</div>
                        <div class="platform-stat-status" id="polymarketStatus">Loading...</div>
                    </div>
                    <div class="platform-stat">
                        <div class="platform-stat-value kalshi" id="kalshiMarkets">0</div>
                        <div class="platform-stat-label">Kalshi</div>
                        <div class="platform-stat-status" id="kalshiStatus">Loading...</div>
                    </div>
                    <div class="platform-stat">
                        <div class="platform-stat-value matched" id="matchedPairs">0</div>
                        <div class="platform-stat-label">Matched</div>
                        <div class="platform-stat-status" id="matchingStatus">Waiting...</div>
                    </div>
                    <div class="platform-stat">
                        <div class="platform-stat-value cross-opp" id="crossOpportunities">0</div>
                        <div class="platform-stat-label">Arb Found</div>
                        <div class="platform-stat-status" id="arbStatus">Scanning...</div>
                    </div>
                    <div class="platform-stat">
                        <div class="platform-stat-value kalshi" id="kalshiOrderbooks">0</div>
                        <div class="platform-stat-label">Kalshi Books</div>
                        <div class="platform-stat-status" id="kalshiObStatus">Waiting...</div>
                    </div>
                </div>
                <div class="matched-pairs-grid" id="matchedPairsGrid"></div>
                <div id="paperEvidencePanel" style="margin-top: 1rem;"></div>
            </div>
        </section>

        <section class="card" id="platformOpportunityCard">
            <div class="card-header">
                <span class="card-title">⚡ Platform-First Opportunity System</span>
                <span id="platformOpportunityStatus" style="font-size: 0.75rem; color: var(--text-secondary);">DISABLED</span>
            </div>
            <div class="card-body">
                <div id="platformOpportunitySummary" style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.75rem;">Shadow system disabled</div>
                <div id="platformOpportunityLanes" style="display: grid; gap: 0.5rem;"></div>
            </div>
        </section>

        <section class="card" id="eventWeekCard">
            <div class="card-header">
                <span class="card-title">📅 Scheduled Event Week</span>
                <span id="eventWeekStatus" style="font-size: 0.75rem; color: var(--text-secondary);">DISABLED</span>
            </div>
            <div class="card-body">
                <div id="eventWeekSummary" style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.75rem;">
                    Event lane disabled
                </div>
                <div id="eventWeekSources" style="font-size: 0.72rem; color: var(--text-muted); margin-bottom: 0.75rem;"></div>
                <div id="eventWeekItems" style="display: grid; gap: 0.65rem;"></div>
            </div>
        </section>

        <section class="card" id="newsCatalystCard">
            <div class="card-header">
                <span class="card-title">📰 Today’s Catalysts</span>
                <span id="newsCatalystStatus" style="font-size: 0.75rem; color: var(--text-secondary);">DISABLED</span>
            </div>
            <div class="card-body">
                <div id="newsCatalystSummary" style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.75rem;">
                    Scanner disabled
                </div>
                <div id="newsCatalystItems" style="display: grid; gap: 0.65rem;"></div>
            </div>
        </section>
        
        <!-- 🔥 LIVE OPPORTUNITIES FEED -->
        <section class="card opportunities-feed">
            <div class="card-header">
                <span class="card-title">🔥 Arbitrage & Market Monitoring</span>
                <div style="display: flex; align-items: center; gap: 1rem;">
                    <span class="opp-status">
                        <span class="opp-status-dot"></span>
                        Scanning
                    </span>
                    <span id="oppCount" style="font-size: 0.75rem; color: var(--accent-green); font-weight: 600;">0 found</span>
                </div>
            </div>
            <div class="card-body" id="opportunitiesFeed">
                <div class="no-opportunities" id="noOpportunities">
                    <div style="font-size: 2.5rem; margin-bottom: 1rem;">🔍</div>
                    <div style="font-size: 1rem; font-weight: 600; margin-bottom: 0.5rem;">Scanning for arbitrage...</div>
                    <div style="font-size: 0.8rem;">Checking Polymarket, Kalshi, and cross-platform opportunities</div>
                </div>
                <!-- Opportunities will be inserted here dynamically -->
            </div>
        </section>
        
        <!-- Opportunity Timing -->
        <section class="card timing-card">
            <div class="card-header">
                <span class="card-title">⏱️ Opportunity Timing</span>
                <span id="timingCount" style="font-size: 0.75rem; color: var(--text-secondary);">0 tracked</span>
            </div>
            <div class="card-body">
                <div class="timing-stats">
                    <div class="timing-stat">
                        <div class="timing-stat-value" id="avgDuration">--</div>
                        <div class="timing-stat-label">Avg Duration</div>
                    </div>
                    <div class="timing-stat">
                        <div class="timing-stat-value" id="minDuration">--</div>
                        <div class="timing-stat-label">Min Duration</div>
                    </div>
                    <div class="timing-stat">
                        <div class="timing-stat-value" id="maxDuration">--</div>
                        <div class="timing-stat-label">Max Duration</div>
                    </div>
                    <div class="timing-stat">
                        <div class="timing-stat-value" id="activeOpps">0</div>
                        <div class="timing-stat-label">Active Now</div>
                    </div>
                </div>
                <div class="timing-buckets">
                    <div class="timing-bucket fast">
                        <span class="timing-bucket-count" id="under100ms">0</span>
                        <div>&lt;100ms</div>
                    </div>
                    <div class="timing-bucket medium">
                        <span class="timing-bucket-count" id="under500ms">0</span>
                        <div>&lt;500ms</div>
                    </div>
                    <div class="timing-bucket slow">
                        <span class="timing-bucket-count" id="under1s">0</span>
                        <div>&lt;1s</div>
                    </div>
                    <div class="timing-bucket very-slow">
                        <span class="timing-bucket-count" id="over1s">0</span>
                        <div>&gt;1s</div>
                    </div>
                </div>
                <div class="timing-recent" id="recentTimings">
                    <div style="text-align: center; color: var(--text-secondary); padding: 1rem;">
                        Waiting for opportunity data...
                    </div>
                </div>
            </div>
        </section>
        
        <!-- Markets -->
        <section class="card markets-card">
            <div class="card-header">
                <span class="card-title">Monitored Markets</span>
            </div>
            <div class="card-body">
                <div id="marketList">
                    <div class="empty-state">
                        <div class="empty-icon">📈</div>
                        <div>Loading markets...</div>
                    </div>
                </div>
            </div>
        </section>
    </main>
    
    <div class="connection-status" id="connectionStatus">
        Connecting...
    </div>
    
    <script>
        let ws = null;
        let state = {};
        let reconnectAttempts = 0;
        let decisionFilter = 'all';
        let paperHistoryFilter = 'all';
        const displayTimezone = 'America/New_York';

        function connect() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
            
            ws.onopen = () => {
                console.log('WebSocket connected');
                document.getElementById('connectionStatus').textContent = '🟢 Dashboard connected';
                document.getElementById('connectionStatus').className = 'connection-status connected';
                reconnectAttempts = 0;
            };
            
            ws.onclose = () => {
                console.log('WebSocket disconnected');
                document.getElementById('connectionStatus').textContent = '🔴 Disconnected';
                document.getElementById('connectionStatus').className = 'connection-status disconnected';
                setTimeout(reconnect, Math.min(1000 * Math.pow(2, reconnectAttempts), 30000));
                reconnectAttempts++;
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };
            
            ws.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                
                if (msg.type === 'initial' || msg.type === 'update') {
                    state = msg.data || msg;
                    updateDashboard();
                } else if (msg.type === 'opportunity') {
                    addOpportunity(msg.data);
                } else if (msg.type === 'activity') {
                    addActivity(msg.data);
                }
            };
        }
        
        function reconnect() {
            if (ws && ws.readyState === WebSocket.OPEN) return;
            connect();
        }
        
        function updateDashboard() {
            // Status
            const statusDot = document.getElementById('statusDot');
            const statusText = document.getElementById('statusText');
            if (state.is_running) {
                statusDot.className = 'status-dot running';
                statusText.textContent = 'Running';
            } else {
                statusDot.className = 'status-dot stopped';
                statusText.textContent = 'Stopped';
            }
            
            // Mode
            const modeBadge = document.getElementById('modeBadge');
            if (state.mode === 'live') {
                modeBadge.className = 'mode-badge live';
                modeBadge.textContent = 'LIVE';
            } else {
                modeBadge.className = 'mode-badge dry-run';
                modeBadge.textContent = 'DRY RUN';
            }
            
            // Metrics
            updateMetrics();
            
            // Opportunities
            updateOpportunities();
            
            // Activity
            updateActivity();

            // Paper trade history
            updatePaperHistory();
            
            // Decision Journal
            updateDecisions();
            
            // Risk
            updateRisk();
            
            // Timing
            updateTiming();
            
            // Operational
            updateOperational();
            
            // Cross-Platform
            updateCrossPlatform();

            // Platform catalog and isolated shadow strategy lanes
            updatePlatformOpportunity();

            // Authoritative scheduled-event monitoring lane
            updateEventWeek();

            // Source-backed news priority signals
            updateNewsCatalysts();
            
            // Markets
            updateMarkets();
        }
        
        function updateMetrics() {
            const portfolio = state.portfolio || {};
            const pnl = portfolio.pnl || {};
            const stats = state.stats || {};
            
            const totalPnl = pnl.total_pnl || 0;
            const realizedPnl = pnl.realized_pnl || 0;
            const exposure = portfolio.total_exposure || 0;
            const winRate = (portfolio.win_rate || 0) * 100;
            
            document.getElementById('totalPnl').textContent = formatCurrency(totalPnl);
            document.getElementById('totalPnl').className = `metric-value ${totalPnl >= 0 ? 'positive' : 'negative'}`;
            
            document.getElementById('realizedPnl').textContent = formatCurrency(realizedPnl);
            document.getElementById('realizedPnl').className = `metric-value ${realizedPnl >= 0 ? 'positive' : 'negative'}`;
            
            document.getElementById('exposure').textContent = formatCurrency(exposure);
            document.getElementById('openOrders').textContent = (state.orders || []).length;
            document.getElementById('opportunityCount').textContent = (state.opportunities || []).length;
            document.getElementById('winRate').textContent = `${winRate.toFixed(1)}%`;
            document.getElementById('winRate').className = `metric-value ${winRate >= 50 ? 'positive' : winRate > 0 ? 'neutral' : 'negative'}`;
        }
        
        function updateOpportunities() {
            const list = document.getElementById('opportunityList');
            const opportunities = state.opportunities || [];
            
            if (opportunities.length === 0) {
                list.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><div>Waiting for opportunities...</div></div>';
                return;
            }
            
            const recent = opportunities.slice(-20).reverse();
            list.innerHTML = recent.map(opp => {
                const typeClass = opp.type?.includes('bundle') ? 
                    (opp.type.includes('long') ? 'bundle-long' : 'bundle-short') : 'mm';
                const typeLabel = opp.type?.replace('_', ' ').toUpperCase() || 'UNKNOWN';
                
                return `
                    <div class="opportunity-item">
                        <span class="opportunity-type ${typeClass}">${typeLabel}</span>
                        <div class="opportunity-details">
                            <div class="opportunity-market">${opp.market_id || 'Unknown'}</div>
                            <span class="opportunity-edge">Edge: ${((opp.edge || 0) * 100).toFixed(2)}%</span>
                        </div>
                        <span class="opportunity-time">${formatTime(opp.timestamp)}</span>
                    </div>
                `;
            }).join('');
            
            document.getElementById('oppRefresh').textContent = `Last: ${formatTime(state.last_update)}`;
        }
        
        function updateActivity() {
            const list = document.getElementById('activityList');
            const signals = state.signals || [];
            const paperOrders = state.paper_orders || [];
            const trades = state.trades || [];
            
            // Combine and sort by timestamp
            const activities = [
                ...signals.map(s => ({...s, activityType: 'signal'})),
                ...paperOrders.map(o => ({...o, activityType: 'paper_order', timestamp: o.created_at || o.updated_at})),
                ...trades.map(t => ({...t, activityType: 'trade'}))
            ].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp)).slice(0, 30);
            
            if (activities.length === 0) {
                list.innerHTML = '<div class="empty-state"><div class="empty-icon">📝</div><div>No activity yet...</div></div>';
                return;
            }
            
            list.innerHTML = activities.map(act => {
                let icon = '📋';
                let iconClass = 'signal';
                let message = '';
                
                if (act.activityType === 'trade') {
                    icon = '✓';
                    iconClass = 'fill';
                    const fillLabel = act.is_simulated ? 'HYPOTHETICAL FILL' : 'FILL';
                    message = `${fillLabel} ${act.side?.toUpperCase() || ''} ${act.token_type?.toUpperCase() || ''} ${(act.size || 0).toFixed(2)} @ ${(act.price || 0).toFixed(4)} (${formatCurrency(act.notional || 0)})`;
                } else if (act.activityType === 'paper_order') {
                    icon = 'P';
                    iconClass = 'signal';
                    message = `PAPER ${act.status?.toUpperCase() || 'ORDER'} ${act.side?.toUpperCase() || ''} ${act.token_type?.toUpperCase() || ''} ${(act.size || 0).toFixed(2)} @ ${(act.price || 0).toFixed(4)} (${formatCurrency(act.notional || 0)})`;
                } else {
                    icon = '→';
                    iconClass = 'signal';
                    message = `${act.action || 'Signal'}: ${act.market_id || ''}`;
                }
                
                return `
                    <div class="activity-item">
                        <div class="activity-icon ${iconClass}">${icon}</div>
                        <div class="activity-content">
                            <div class="activity-message">${message}</div>
                            <div class="activity-time">
                                ${act.market_id || ''}
                                ${act.strategy_tag ? ` · ${act.strategy_tag}` : ''}
                                ${act.order_id ? ` · ${act.order_id}` : ''}
                                · ${formatTime(act.timestamp)}
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }
        
        function updatePaperHistory() {
            const body = document.getElementById('paperHistoryBody');
            if (!body) return;

            const events = (state.paper_history || []).filter(event => {
                if (paperHistoryFilter === 'all') return true;
                return event.event_type === paperHistoryFilter;
            }).slice(0, 200);

            if (events.length === 0) {
                body.innerHTML = '<tr><td colspan="11">No paper trade events yet...</td></tr>';
                return;
            }

            body.innerHTML = events.map(event => {
                const badgeClass = event.event_type || 'placed';
                const reason = event.reason_code || event.reason_detail || '';
                return `
                    <tr>
                        <td>${formatDateTime(event.event_at_utc || event.timestamp)}</td>
                        <td><span class="event-badge ${badgeClass}">${badgeClass}</span></td>
                        <td>${truncate(event.market_id || '', 18)}</td>
                        <td>${(event.side || '').toUpperCase()}</td>
                        <td>${(event.token_type || '').toUpperCase()}</td>
                        <td>${event.price != null ? Number(event.price).toFixed(4) : '--'}</td>
                        <td>${event.size != null ? Number(event.size).toFixed(2) : '--'}</td>
                        <td>${event.notional != null ? formatCurrency(event.notional) : '--'}</td>
                        <td>${event.strategy_tag || '--'}</td>
                        <td>${truncate(event.order_id || '', 16)}</td>
                        <td>${reason}</td>
                    </tr>
                `;
            }).join('');
        }
        
        function updateDecisions() {
            const list = document.getElementById('decisionList');
            const decisions = state.decisions || [];
            
            if (!list) return;
            
            const filtered = decisions.filter(dec => {
                if (decisionFilter === 'all') return true;
                return dec.outcome === decisionFilter || dec.strategy === decisionFilter;
            }).slice(-60).reverse();
            
            if (filtered.length === 0) {
                list.innerHTML = '<div class="empty-state"><div class="empty-icon">Log</div><div>No matching decisions yet...</div></div>';
                return;
            }
            
            list.innerHTML = filtered.map(dec => {
                const evidence = dec.evidence || {};
                const evidenceItems = [
                    ['YES bid/ask', formatPair(evidence.best_bid_yes || evidence.polymarket_yes_bid, evidence.best_ask_yes || evidence.polymarket_yes_ask)],
                    ['NO bid/ask', formatPair(evidence.best_bid_no || evidence.polymarket_no_bid, evidence.best_ask_no || evidence.polymarket_no_ask)],
                    ['Net edge', formatMaybePct(evidence.net_edge_long ?? evidence.net_edge_short ?? evidence.edge_pct)],
                    ['Required', formatMaybePct(evidence.required_net_edge || evidence.required_spread)],
                    ['Spread', formatMaybePct(evidence.spread)],
                    ['Similarity', formatMaybePct(evidence.similarity)]
                ].filter(item => item[1] !== '--');
                
                return `
                    <div class="decision-item ${dec.outcome || ''}">
                        <div class="decision-topline">
                            <span class="decision-badge">${dec.outcome || 'decision'} · ${dec.strategy || 'unknown'}</span>
                            <span class="activity-time">${formatTime(dec.timestamp)}</span>
                        </div>
                        <div class="decision-title">${truncate(dec.market_question || dec.market_id || dec.platform || 'Trading decision', 110)}</div>
                        <div class="decision-explanation">${dec.explanation || dec.reason_code || ''}</div>
                        <div class="decision-evidence">
                            ${evidenceItems.map(([label, value]) => `<span>${label}: <strong>${value}</strong></span>`).join('')}
                        </div>
                    </div>
                `;
            }).join('');
        }
        
        function formatPair(bid, ask) {
            if ((bid === undefined || bid === null) && (ask === undefined || ask === null)) return '--';
            const bidText = bid === undefined || bid === null ? '?' : Number(bid).toFixed(3);
            const askText = ask === undefined || ask === null ? '?' : Number(ask).toFixed(3);
            return `${bidText}/${askText}`;
        }
        
        function formatMaybePct(value) {
            if (value === undefined || value === null) return '--';
            return `${(Number(value) * 100).toFixed(2)}%`;
        }
        
        function updateRisk() {
            const risk = state.risk || {};
            
            const exposure = risk.global_exposure || 0;
            const maxExposure = risk.max_global_exposure || 5000;
            const exposurePct = (exposure / maxExposure) * 100;
            
            document.getElementById('riskExposure').textContent = `$${exposure.toFixed(0)} / $${maxExposure.toLocaleString()}`;
            document.getElementById('exposureBar').style.width = `${Math.min(exposurePct, 100)}%`;
            document.getElementById('exposureBar').className = `risk-bar-fill ${exposurePct < 60 ? 'safe' : exposurePct < 80 ? 'warning' : 'danger'}`;
            
            const dailyPnl = risk.daily_pnl || 0;
            const maxLoss = risk.max_daily_loss || 500;
            const dailyPnlPct = Math.abs(Math.min(dailyPnl, 0)) / maxLoss * 100;
            
            document.getElementById('riskDailyPnl').textContent = `$${dailyPnl.toFixed(2)} / -$${maxLoss}`;
            document.getElementById('dailyPnlBar').style.width = `${Math.min(dailyPnlPct, 100)}%`;
            document.getElementById('dailyPnlBar').className = `risk-bar-fill ${dailyPnlPct < 50 ? 'safe' : dailyPnlPct < 80 ? 'warning' : 'danger'}`;
            
            const drawdown = (risk.current_drawdown_pct || 0);
            const maxDrawdown = (risk.max_drawdown_pct || 10);
            const drawdownPct = (drawdown / maxDrawdown) * 100;
            
            document.getElementById('riskDrawdown').textContent = `${drawdown.toFixed(1)}% / ${maxDrawdown}%`;
            document.getElementById('drawdownBar').style.width = `${Math.min(drawdownPct, 100)}%`;
            document.getElementById('drawdownBar').className = `risk-bar-fill ${drawdownPct < 50 ? 'safe' : drawdownPct < 80 ? 'warning' : 'danger'}`;
            
            document.getElementById('killSwitch').style.display = risk.kill_switch_triggered ? 'block' : 'none';
        }
        
        function updateTiming() {
            const timing = state.timing || {};
            
            // Update count
            document.getElementById('timingCount').textContent = `${timing.total_tracked || 0} tracked`;
            
            // Update main stats
            const avgDuration = timing.avg_duration_ms;
            if (avgDuration !== undefined && avgDuration !== null) {
                document.getElementById('avgDuration').textContent = formatDuration(avgDuration);
                document.getElementById('avgDuration').className = `timing-stat-value ${getDurationClass(avgDuration)}`;
            }
            
            const minDuration = timing.min_duration_ms;
            if (minDuration !== undefined && minDuration !== null) {
                document.getElementById('minDuration').textContent = formatDuration(minDuration);
                document.getElementById('minDuration').className = `timing-stat-value ${getDurationClass(minDuration)}`;
            }
            
            const maxDuration = timing.max_duration_ms;
            if (maxDuration !== undefined && maxDuration !== null) {
                document.getElementById('maxDuration').textContent = formatDuration(maxDuration);
                document.getElementById('maxDuration').className = `timing-stat-value ${getDurationClass(maxDuration)}`;
            }
            
            document.getElementById('activeOpps').textContent = timing.active_opportunities || 0;
            
            // Update buckets
            document.getElementById('under100ms').textContent = timing.under_100ms || 0;
            document.getElementById('under500ms').textContent = timing.under_500ms || 0;
            document.getElementById('under1s').textContent = timing.under_1s || 0;
            document.getElementById('over1s').textContent = timing.over_1s || 0;
            
            // Update recent timings
            const recentList = document.getElementById('recentTimings');
            const recent = timing.recent_durations || [];
            
            if (recent.length === 0) {
                recentList.innerHTML = '<div style="text-align: center; color: var(--text-secondary); padding: 1rem;">Waiting for opportunity data...</div>';
                return;
            }
            
            recentList.innerHTML = recent.slice().reverse().map(item => {
                const durationClass = getDurationClass(item.duration_ms);
                const typeLabel = item.type?.replace('_', ' ') || 'unknown';
                const executedBadge = item.executed ? '<span style="color: var(--accent-green);">✓</span>' : '';
                
                return `
                    <div class="timing-recent-item">
                        <span>${typeLabel} ${executedBadge}</span>
                        <span class="timing-duration ${durationClass}">${formatDuration(item.duration_ms)}</span>
                    </div>
                `;
            }).join('');
        }
        
        function formatDuration(ms) {
            if (ms === undefined || ms === null) return '--';
            if (ms < 1000) return `${Math.round(ms)}ms`;
            return `${(ms / 1000).toFixed(1)}s`;
        }
        
        function getDurationClass(ms) {
            if (ms < 200) return 'fast';
            if (ms < 1000) return 'medium';
            return 'slow';
        }
        
        let lastUpdateCount = 0;
        let lastUpdateTime = Date.now();
        
        function updateOperational() {
            const op = state.operational || {};
            const cp = state.cross_platform || {};
            
            // Show combined market count (Polymarket + Kalshi)
            const polyCount = cp.polymarket_markets || 0;
            const kalshiCount = cp.kalshi_markets || 0;
            const totalCombined = polyCount + kalshiCount;
            
            // Update stats - show combined if cross-platform is enabled
            const totalEl = document.getElementById('totalMarkets');
            if (cp.enabled && totalCombined > 0) {
                totalEl.innerHTML = `<span style="color: #8b5cf6;">${polyCount.toLocaleString()}</span> + <span style="color: #f7931a;">${kalshiCount.toLocaleString()}</span>`;
            } else {
                totalEl.textContent = op.total_markets || 0;
            }
            document.getElementById('marketsWithData').textContent = op.markets_with_orderbooks || 0;
            document.getElementById('marketsWithPrices').textContent = op.markets_with_prices || 0;
            document.getElementById('orderbookUpdates').textContent = formatNumber(op.orderbook_updates || 0);
            
            // Calculate updates per minute
            const now = Date.now();
            const timeDiff = (now - lastUpdateTime) / 1000; // seconds
            const updateDiff = (op.orderbook_updates || 0) - lastUpdateCount;
            
            if (timeDiff > 0 && lastUpdateCount > 0) {
                const updatesPerMin = Math.round((updateDiff / timeDiff) * 60);
                document.getElementById('updatesPerMin').textContent = updatesPerMin;
            }
            
            lastUpdateCount = op.orderbook_updates || 0;
            lastUpdateTime = now;
            
            // Estimate cycle time (time to check all markets)
            const totalMarkets = op.total_markets || 1;
            const updatesPerSec = (op.orderbook_updates || 0) / Math.max(state.uptime_seconds || 1, 1);
            if (updatesPerSec > 0) {
                const cycleSeconds = totalMarkets / updatesPerSec;
                document.getElementById('cycleTime').textContent = formatCycleTime(cycleSeconds);
            }
            
            // Stream status
            const statusEl = document.getElementById('streamStatus');
            if (op.is_streaming) {
                statusEl.textContent = '● Streaming';
                statusEl.style.color = 'var(--accent-green)';
            } else {
                statusEl.textContent = '○ Stopped';
                statusEl.style.color = 'var(--accent-red)';
            }
            
            // Uptime
            if (state.uptime_seconds) {
                document.getElementById('uptime').textContent = formatUptime(state.uptime_seconds);
            }

            const run = state.run_session || {};
            document.getElementById('runNumber').textContent = run.run_number ? `#${run.run_number}` : '--';
            document.getElementById('runTimer').textContent = formatUptime(run.elapsed_seconds || 0);
            document.getElementById('runTransactions').textContent = run.transaction_count || 0;
            const runPnl = Number(run.pnl || 0);
            const runPnlEl = document.getElementById('runPnl');
            runPnlEl.textContent = formatCurrency(runPnl);
            runPnlEl.style.color = runPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';

            const runs = state.run_sessions || [];
            document.getElementById('recentRuns').innerHTML = runs.slice(0, 5).map(item => {
                const pnl = Number(item.pnl || 0);
                return `<div class="recent-run-row">
                    <span>#${Number(item.run_number || 0)}</span>
                    <span>${escapeHtml(item.status || 'unknown')}</span>
                    <span>${formatUptime(Number(item.elapsed_seconds || 0))}</span>
                    <span>${Number(item.transaction_count || 0)} tx</span>
                    <span style="color: ${pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">${formatCurrency(pnl)}</span>
                </div>`;
            }).join('');
        }
        
        function formatNumber(num) {
            if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
            if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
            return num.toString();
        }
        
        function formatCycleTime(seconds) {
            if (seconds < 60) return Math.round(seconds) + 's';
            if (seconds < 3600) return Math.round(seconds / 60) + 'm';
            return (seconds / 3600).toFixed(1) + 'h';
        }
        
        function formatUptime(seconds) {
            const hrs = Math.floor(seconds / 3600);
            const mins = Math.floor((seconds % 3600) / 60);
            const secs = Math.floor(seconds % 60);
            return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
        
        function updateCrossPlatform() {
            const cp = state.cross_platform || {};
            const matchingStatus = cp.matching_status || 'idle';
            const scanStatus = cp.scan_status || 'idle';
            const semanticMetrics = cp.semantic_metrics || {};
            const connectionEl = document.getElementById('connectionStatus');

            if (ws && ws.readyState === WebSocket.OPEN) {
                if (scanStatus === 'degraded' || scanStatus === 'error') {
                    connectionEl.textContent = '🟠 Data scanner degraded';
                    connectionEl.className = 'connection-status degraded';
                } else {
                    connectionEl.textContent = '🟢 Dashboard connected';
                    connectionEl.className = 'connection-status connected';
                }
            }
            
            // Update status badge
            const statusEl = document.getElementById('crossPlatformStatus');
            statusEl.title = '';
            if (!cp.enabled) {
                statusEl.textContent = 'DISABLED';
                statusEl.style.background = 'linear-gradient(135deg, #666, #444)';
            } else if (matchingStatus === 'error') {
                statusEl.textContent = 'DISCOVERY ERROR';
                statusEl.style.background = 'linear-gradient(135deg, #ef4444, #b91c1c)';
            } else if (matchingStatus === 'no_matches') {
                statusEl.textContent = 'NO VERIFIED PAIRS';
                statusEl.style.background = 'linear-gradient(135deg, #f59e0b, #b45309)';
            } else if (scanStatus === 'degraded' || scanStatus === 'error') {
                statusEl.textContent = 'SCANNER DEGRADED';
                statusEl.title = cp.degraded_reason || 'Fresh paired snapshots are unavailable';
                statusEl.style.background = 'linear-gradient(135deg, #f59e0b, #b45309)';
            } else if (matchingStatus === 'complete' && (cp.matched_pairs || 0) > 0) {
                statusEl.textContent = 'SCANNING PRICES';
                statusEl.style.background = 'linear-gradient(135deg, #00ff88, #00cc66)';
            } else {
                statusEl.textContent = 'DISCOVERING';
                statusEl.style.background = 'linear-gradient(135deg, #8b5cf6, #6d28d9)';
            }
            
            // Update stats
            const polyCount = cp.polymarket_markets || 0;
            const kalshiCount = cp.kalshi_markets || 0;
            const matchedCount = cp.matched_pairs || 0;
            const kalshiObs = cp.kalshi_orderbooks || 0;
            
            document.getElementById('polymarketMarkets').textContent = polyCount.toLocaleString();
            document.getElementById('kalshiMarkets').textContent = kalshiCount.toLocaleString();
            document.getElementById('matchedPairs').textContent = matchedCount;
            document.getElementById('kalshiOrderbooks').textContent = kalshiObs;
            
            // Update status indicators with loading animation
            const polyStatus = document.getElementById('polymarketStatus');
            const kalshiStatus = document.getElementById('kalshiStatus');
            
            // Polymarket status
            if (semanticMetrics.eligible_polymarket_markets !== undefined) {
                polyStatus.textContent = `✓ ${semanticMetrics.eligible_polymarket_markets.toLocaleString()} eligible · ${semanticMetrics.filtered_polymarket_markets.toLocaleString()} filtered`;
                polyStatus.className = 'platform-stat-status ready';
            } else if (polyCount > 0) {
                polyStatus.textContent = '✓ Loaded';
                polyStatus.className = 'platform-stat-status ready';
            } else {
                polyStatus.textContent = '⏳ Loading...';
                polyStatus.className = 'platform-stat-status loading';
            }
            
            // Kalshi status
            if (semanticMetrics.eligible_kalshi_markets !== undefined) {
                kalshiStatus.textContent = `✓ ${semanticMetrics.eligible_kalshi_markets.toLocaleString()} eligible · ${semanticMetrics.filtered_kalshi_markets.toLocaleString()} filtered`;
                kalshiStatus.className = 'platform-stat-status ready';
            } else if (kalshiCount > 0) {
                kalshiStatus.textContent = '✓ Loaded';
                kalshiStatus.className = 'platform-stat-status ready';
            } else {
                kalshiStatus.textContent = '⏳ Loading...';
                kalshiStatus.className = 'platform-stat-status loading';
            }
            
            const matchStatus = document.getElementById('matchingStatus');
            const kalshiObStatus = document.getElementById('kalshiObStatus');
            
            const matchingProgress = cp.matching_progress || 0;
            const matchingChecked = cp.matching_checked || 0;
            const matchingTotal = cp.matching_total || 0;
            
            // Update progress bar
            const progressContainer = document.getElementById('matchingProgressContainer');
            const progressBar = document.getElementById('matchingProgressBar');
            const progressText = document.getElementById('matchingProgressText');
            const matchingStatsEl = document.getElementById('matchingStats');
            
            if (matchingStatus === 'matching' || matchingStatus === 'starting') {
                progressContainer.style.display = 'block';
                progressBar.style.width = `${matchingProgress}%`;
                progressText.textContent = `${matchingProgress}%`;
                matchingStatsEl.textContent = `Checked: ${matchingChecked.toLocaleString()} / ${matchingTotal.toLocaleString()} | Found: ${matchedCount} matches`;
                matchStatus.textContent = `🔍 ${matchingProgress}%`;
                matchStatus.className = 'platform-stat-status scanning';
            } else if (matchingStatus === 'complete') {
                progressContainer.style.display = 'none';
                matchStatus.textContent = `✓ ${matchedCount} pairs`;
                matchStatus.className = 'platform-stat-status ready';
            } else if (matchingStatus === 'no_matches') {
                progressContainer.style.display = 'none';
                matchStatus.textContent = '0 pairs · refresh scheduled';
                matchStatus.className = 'platform-stat-status';
            } else if (matchingStatus === 'refreshing') {
                progressContainer.style.display = 'none';
                matchStatus.textContent = '↻ Refreshing markets...';
                matchStatus.className = 'platform-stat-status loading';
            } else if (matchingStatus === 'error') {
                progressContainer.style.display = 'none';
                matchStatus.textContent = '⚠ Discovery error';
                matchStatus.className = 'platform-stat-status error';
            } else if (polyCount > 0 && kalshiCount > 0) {
                progressContainer.style.display = 'none';
                matchStatus.textContent = '⏳ Starting...';
                matchStatus.className = 'platform-stat-status loading';
            } else {
                progressContainer.style.display = 'none';
                matchStatus.textContent = 'Waiting...';
                matchStatus.className = 'platform-stat-status';
            }
            
            if (kalshiObs > 0) {
                kalshiObStatus.textContent = `${kalshiObs} fetched`;
                kalshiObStatus.className = 'platform-stat-status ready';
            } else if (matchedCount > 0) {
                kalshiObStatus.textContent = '⏳ Fetching...';
                kalshiObStatus.className = 'platform-stat-status loading';
            } else if (matchingStatus === 'no_matches') {
                kalshiObStatus.textContent = 'Needs verified pair';
                kalshiObStatus.className = 'platform-stat-status';
            }
            
            const crossOpps = cp.cross_opportunities || [];
            const matchedPairsData = cp.matched_pairs_data || [];
            document.getElementById('crossOpportunities').textContent = crossOpps.length;

            const evidencePanel = document.getElementById('paperEvidencePanel');
            const paper = cp.paper_performance || {};
            const nearMisses = Array.isArray(cp.near_misses) ? cp.near_misses : [];
            const receipts = Array.isArray(cp.paper_trade_receipts) ? cp.paper_trade_receipts : [];
            const funnel = cp.evaluation_funnel || {};
            const allocation = semanticMetrics.allocation || {};
            const shadowOutcomes = allocation.shadow_outcomes || {};
            const shadowPreflight = allocation.shadow_preflight || {};
            const mixText = (counts) => Object.entries(counts || {})
                .sort((left, right) => Number(right[1]) - Number(left[1]))
                .slice(0, 4)
                .map(([name, count]) => `${escapeHtml(name)} ${Number(count).toLocaleString()}`)
                .join(' · ') || 'No candidates';
            const strongest = nearMisses[0];
            evidencePanel.innerHTML = `<div style="border: 1px solid var(--border-color); border-radius: 8px; padding: 0.85rem; background: var(--bg-secondary);">
                <div style="font-weight: 700; margin-bottom: 0.55rem;">Paper evidence</div>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.65rem; font-size: 0.76rem;">
                    <div><span style="color: var(--text-muted);">Directions stored</span><br><strong>${Number(cp.evaluation_ledger_count || 0).toLocaleString()}</strong></div>
                    <div><span style="color: var(--text-muted);">Fresh snapshots</span><br><strong>${Number(funnel.paired_snapshot_fresh || 0).toLocaleString()}</strong></div>
                    <div><span style="color: var(--text-muted);">Pair trades</span><br><strong>${Number(paper.trade_count || receipts.length || 0).toLocaleString()}</strong></div>
                    <div><span style="color: var(--text-muted);">Projected locked PnL</span><br><strong>${formatCurrency(Number(paper.projected_locked_pnl || 0))}</strong></div>
                    <div><span style="color: var(--text-muted);">Realized settlement PnL</span><br><strong>${formatCurrency(Number(paper.realized_settlement_pnl || 0))}</strong></div>
                    <div><span style="color: var(--text-muted);">Bank cash</span><br><strong>${formatCurrency(Number(paper.cash_balance || paper.available_capital || 0))}</strong></div>
                    <div><span style="color: var(--text-muted);">Deployable capacity</span><br><strong>${formatCurrency(Number(paper.remaining_deployable_capital || 0))}</strong></div>
                </div>
                <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.65rem;">${strongest
                    ? `Strongest near miss: ${escapeHtml(strongest.token)} ${escapeHtml(strongest.buy_platform)} → ${escapeHtml(strongest.sell_platform)} · executable edge ${(Number(strongest.executable_net_edge || 0) * 100).toFixed(2)}% · ${escapeHtml(strongest.reason_code)}`
                    : 'No direction-level near misses have been recorded in this run yet.'}</div>
                <div style="font-size: 0.68rem; color: var(--text-muted); margin-top: 0.35rem;">PnL source: ${escapeHtml(paper.pnl_source || 'unavailable')}</div>
                <div style="border-top: 1px solid var(--border-color); margin-top: 0.8rem; padding-top: 0.7rem;">
                    <div style="font-weight: 700; margin-bottom: 0.45rem;">Discovery A/B evidence</div>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0.55rem; font-size: 0.72rem;">
                        <div><span style="color: var(--text-muted);">Rules equivalent</span><br><strong>${Number(cp.rules_equivalent_pairs || 0).toLocaleString()}</strong></div>
                        <div><span style="color: var(--text-muted);">Usable books</span><br><strong>${Number(cp.preflight_usable_pairs || 0).toLocaleString()}</strong></div>
                        <div><span style="color: var(--text-muted);">Verifier budget</span><br><strong>${Number(allocation.verification_budget || 0).toLocaleString()}</strong></div>
                        <div><span style="color: var(--text-muted);">Category entropy</span><br><strong>${Number(allocation.selected_category_entropy || 0).toFixed(2)}</strong></div>
                    </div>
                    <div style="font-size: 0.69rem; color: var(--text-muted); margin-top: 0.45rem;">Baseline top-score mix: ${mixText(allocation.baseline_category_counts)}</div>
                    <div style="font-size: 0.69rem; color: var(--text-muted); margin-top: 0.25rem;">Stratified mix: ${mixText(allocation.stratified_category_counts)}</div>
                    <div style="font-size: 0.69rem; color: var(--text-muted); margin-top: 0.25rem;">Shadow outcomes: baseline ${Number((shadowOutcomes.baseline || {}).equivalent_unique_families || 0)} equivalent families / ${Number((shadowPreflight.baseline || {}).usable_books || 0)} usable books · stratified ${Number((shadowOutcomes.stratified || {}).equivalent_unique_families || 0)} equivalent families / ${Number((shadowPreflight.stratified || {}).usable_books || 0)} usable books</div>
                </div>
            </div>`;
            
            // 🔥 Update Live Opportunities Feed
            updateOpportunitiesFeed(state, cp, matchedPairsData);
            
            // Update arb status
            const arbStatus = document.getElementById('arbStatus');
            if (crossOpps.length > 0) {
                arbStatus.textContent = `🎯 ${crossOpps.length} found!`;
                arbStatus.className = 'platform-stat-status ready';
            } else if (matchedCount > 0) {
                arbStatus.textContent = '🔍 Scanning...';
                arbStatus.className = 'platform-stat-status scanning';
            } else if (matchingStatus === 'no_matches') {
                arbStatus.textContent = 'No eligible pairs';
                arbStatus.className = 'platform-stat-status';
            } else {
                arbStatus.textContent = 'Waiting...';
                arbStatus.className = 'platform-stat-status';
            }
            
            // Update matched pairs grid
            const grid = document.getElementById('matchedPairsGrid');
            if (!cp.enabled) {
                grid.innerHTML = '<div style="text-align: center; color: var(--text-secondary); padding: 2rem; grid-column: 1 / -1;"><div style="font-size: 2rem; margin-bottom: 0.5rem;">⏸️</div><div>Cross-platform mode disabled</div></div>';
                return;
            }
            
            if (matchedPairsData.length === 0 && crossOpps.length === 0) {
                const polyCount = cp.polymarket_markets || 0;
                const kalshiCount = cp.kalshi_markets || 0;
                if (matchingStatus === 'no_matches') {
                    const reviewed = cp.review_candidate_count || 0;
                    const structural = semanticMetrics.structural_candidates || 0;
                    const retrieved = semanticMetrics.retrieved_candidates || 0;
                    grid.innerHTML = `<div style="text-align: center; color: var(--text-secondary); padding: 2rem; grid-column: 1 / -1;">
                        <div style="font-size: 2rem; margin-bottom: 0.5rem;">⚠️</div>
                        <div style="font-weight: 700; color: var(--warning);">No equivalent cross-venue pairs passed verification this cycle.</div>
                        <div style="font-size: 0.8rem; margin-top: 0.75rem;">${structural.toLocaleString()} structurally plausible · ${retrieved.toLocaleString()} retrieved · ${reviewed.toLocaleString()} rejected or queued for review</div>
                        <div style="font-size: 0.8rem; margin-top: 0.5rem;">Kalshi books are fetched only after a pair is verified. Price scanning is idle by design.</div>
                    </div>`;
                    return;
                }
                grid.innerHTML = `<div style="text-align: center; color: var(--text-secondary); padding: 2rem; grid-column: 1 / -1;">
                    <div style="font-size: 2rem; margin-bottom: 0.5rem;">🔍</div>
                    <div>Scanning ${polyCount.toLocaleString()} Polymarket & ${kalshiCount.toLocaleString()} Kalshi markets...</div>
                    <div style="font-size: 0.8rem; margin-top: 0.5rem;">Looking for matching NFL, NBA, Politics, Crypto predictions</div>
                </div>`;
                return;
            }
            
            // Render matched pairs as cards (show opportunities first, then other pairs)
            const allPairs = [...crossOpps.map(o => ({...o, hasArb: true})), ...matchedPairsData.slice(0, 20)];
            
            grid.innerHTML = allPairs.slice(0, 12).map((pair, idx) => {
                const hasArb = pair.hasArb || false;
                const edgePct = ((pair.edge_pct || 0) * 100);
                const category = detectCategory(pair.poly_question || pair.market_pair || '');
                
                return `
                    <div class="pair-card ${hasArb ? 'has-arb' : ''}">
                        <div class="pair-header">
                            <span class="pair-sport-badge">${category}</span>
                            ${hasArb ? '<span class="pair-arb-badge">⚡ Arb Available</span>' : ''}
                        </div>
                        <div class="pair-title">${truncate(pair.poly_question || pair.kalshi_title || 'Market ' + (idx + 1), 60)}</div>
                        <div class="pair-platforms">
                            <div class="platform-box">
                                <div class="platform-name">
                                    <span class="dot polymarket"></span>
                                    <span>Polymarket</span>
                                </div>
                                <div class="platform-prices">
                                    <span class="yes">${formatPct(pair.poly_yes ?? pair.buy_price)}</span>
                                    <span class="divider">/</span>
                                    <span class="no">${formatPct(pair.poly_no ?? (pair.buy_price != null ? 1 - pair.buy_price : null))}</span>
                                </div>
                            </div>
                            <div class="platform-box">
                                <div class="platform-name">
                                    <span class="dot kalshi"></span>
                                    <span>Kalshi</span>
                                </div>
                                <div class="platform-prices">
                                    <span class="yes">${formatPct(pair.kalshi_yes ?? pair.sell_price)}</span>
                                    <span class="divider">/</span>
                                    <span class="no">${formatPct(pair.kalshi_no ?? (pair.sell_price != null ? 1 - pair.sell_price : null))}</span>
                                </div>
                            </div>
                        </div>
                        <div class="pair-footer">
                            <span>Similarity: ${((pair.similarity || 0.8) * 100).toFixed(0)}%</span>
                            <span class="pair-edge ${edgePct > 1 ? 'positive' : 'negative'}">
                                ${hasArb ? `Edge: +${edgePct.toFixed(1)}%` : 'No arb'}
                            </span>
                        </div>
                    </div>
                `;
            }).join('');
        }
        
        // 🔥 Live Opportunities Feed Renderer
        function updateOpportunitiesFeed(state, cp, matchedPairs) {
            const feed = document.getElementById('opportunitiesFeed');
            const noOpps = document.getElementById('noOpportunities');
            const oppCount = document.getElementById('oppCount');
            
            // Collect ALL opportunities: bundle arb, cross-platform, and potential matches
            let allOpportunities = [];
            
            // 1. Add Polymarket bundle arbitrage opportunities
            const bundleOpps = state.opportunities || [];
            bundleOpps.forEach(opp => {
                allOpportunities.push({
                    type: 'polymarket',
                    title: opp.market_question || 'Bundle Arbitrage',
                    category: detectCategory(opp.market_question || ''),
                    edge: opp.net_edge_pct || opp.edge_pct || 0,
                    platform1: { name: 'Polymarket', price: opp.yes_price || 0.5, action: 'BUY YES' },
                    platform2: { name: 'Polymarket', price: opp.no_price || 0.5, action: 'BUY NO' },
                    marketInfo: 'Bundle: YES + NO < 100%'
                });
            });
            
            // 2. Add cross-platform opportunities
            const crossOpps = cp.cross_opportunities || [];
            crossOpps.forEach(opp => {
                allOpportunities.push({
                    type: 'cross-platform',
                    title: opp.market_pair || opp.token || 'Cross-Platform Arb',
                    category: detectCategory(opp.market_pair || opp.token || ''),
                    edge: opp.edge_pct || 0,
                    platform1: { name: opp.buy_platform || 'Polymarket', price: opp.buy_price || 0, action: 'BUY' },
                    platform2: { name: opp.sell_platform || 'Kalshi', price: opp.sell_price || 0, action: 'SELL' },
                    marketInfo: `${opp.buy_platform} vs ${opp.sell_platform}`
                });
            });
            
            // 3. Add matched pairs as potential opportunities (show best matches)
            if (matchedPairs && matchedPairs.length > 0) {
                matchedPairs.slice(0, 20).forEach(pair => {
                    // Only show high similarity matches
                    if ((pair.similarity || 0) >= 0.6) {
                        allOpportunities.push({
                            type: 'matched',
                            title: pair.poly_question || pair.kalshi_title || 'Matched Market',
                            category: detectCategory(pair.poly_question || pair.kalshi_title || ''),
                            edge: 0, // No arb found yet
                            similarity: pair.similarity || 0,
                            platform1: { name: 'Polymarket', price: pair.poly_yes ?? null, action: 'Market' },
                            platform2: { name: 'Kalshi', price: pair.kalshi_yes ?? null, action: 'Market' },
                            marketInfo: `Monitoring: ${((pair.similarity || 0) * 100).toFixed(0)}% equivalent`
                        });
                    }
                });
            }
            
            // Sort by edge (highest first)
            allOpportunities.sort((a, b) => (b.edge || 0) - (a.edge || 0));
            
            // Update count
            const arbCount = allOpportunities.filter(o => o.edge > 0).length;
            const monitoredCount = allOpportunities.filter(o => o.type === 'matched').length;
            oppCount.textContent = `${arbCount} ARB · ${monitoredCount} monitored`;
            
            // If no opportunities, show scanning message
            if (allOpportunities.length === 0) {
                noOpps.style.display = 'block';
                return;
            }
            
            noOpps.style.display = 'none';
            
            // Render actionable opportunities separately from non-actionable matches.
            const renderOpportunityCards = opportunities => opportunities.slice(0, 15).map(opp => {
                const edgePct = (opp.edge * 100).toFixed(2);
                const hasArb = opp.edge > 0;
                const cardClass = opp.type === 'cross-platform' ? 'cross-platform' : 
                                  opp.type === 'polymarket' ? 'polymarket' : 'kalshi';
                const badgeClass = getBadgeClass(opp.category);
                
                return `
                    <div class="opp-card ${cardClass}">
                        <div class="opp-header">
                            <div class="opp-category">
                                <span class="opp-badge ${badgeClass}">${opp.category}</span>
                                ${opp.type === 'cross-platform' ? '<span class="opp-badge cross">CROSS</span>' : ''}
                            </div>
                            ${hasArb ? 
                                `<div class="opp-edge">+${edgePct}%</div>` : 
                                (opp.similarity ? `<div style="color: var(--text-muted); font-size: 0.8rem;">${(opp.similarity * 100).toFixed(0)}% match</div>` : '')
                            }
                        </div>
                        <div class="opp-title">${truncate(opp.title, 70)}</div>
                        <div class="opp-market-info">
                            <span style="opacity: 0.6;">📊</span> ${escapeHtml(opp.marketInfo)}
                            ${hasArb ? '<span style="margin-left: 0.5rem; color: var(--accent-green);">● Active</span>' : ''}
                        </div>
                        <div class="opp-platforms">
                            <div class="opp-platform-row">
                                <div class="opp-platform-name">
                                    <span class="opp-platform-icon ${opp.platform1.name.toLowerCase().includes('poly') ? 'poly' : 'kalshi'}">
                                        ${escapeHtml(opp.platform1.name.charAt(0))}
                                    </span>
                                    ${escapeHtml(opp.platform1.name)}
                                    ${hasArb ? '<span style="margin-left: auto; font-size: 0.65rem; color: var(--accent-green);">↗</span>' : ''}
                                </div>
                                <div class="opp-platform-price ${hasArb ? 'buy' : ''}">${formatPct(opp.platform1.price)}</div>
                            </div>
                            <div class="opp-platform-row">
                                <div class="opp-platform-name">
                                    <span class="opp-platform-icon ${opp.platform2.name.toLowerCase().includes('kalshi') ? 'kalshi' : 'poly'}">
                                        ${escapeHtml(opp.platform2.name.charAt(0))}
                                    </span>
                                    ${escapeHtml(opp.platform2.name)}
                                    ${hasArb ? '<span style="margin-left: auto; font-size: 0.65rem; color: #ef4444;">↘</span>' : ''}
                                </div>
                                <div class="opp-platform-price ${hasArb ? 'sell' : ''}">${formatPct(opp.platform2.price)}</div>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');

            const actionable = allOpportunities.filter(opp => opp.type !== 'matched');
            const monitoring = allOpportunities.filter(opp => opp.type === 'matched');
            const cardsHTML =
                (actionable.length > 0
                    ? '<div class="feed-subsection-title">Live Arbitrage Opportunities</div>' +
                      renderOpportunityCards(actionable)
                    : '') +
                (monitoring.length > 0
                    ? '<div class="feed-subsection-title">Matched Pairs — Monitoring</div>' +
                      renderOpportunityCards(monitoring)
                    : '');
            
            // Insert before the noOpportunities div
            feed.innerHTML = cardsHTML + '<div class="no-opportunities" id="noOpportunities" style="display: none;"></div>';
        }
        
        function getBadgeClass(category) {
            const cat = category.toLowerCase();
            if (cat.includes('nfl') || cat.includes('football')) return 'nfl';
            if (cat.includes('nba') || cat.includes('basketball')) return 'nba';
            if (cat.includes('politic') || cat.includes('trump') || cat.includes('election')) return 'politics';
            if (cat.includes('crypto') || cat.includes('bitcoin')) return 'crypto';
            if (cat.includes('soccer') || cat.includes('premier') || cat.includes('league')) return 'soccer';
            return '';
        }
        
        function detectCategory(text) {
            const t = text.toLowerCase();
            if (t.includes('nfl') || t.includes('football') || t.includes('bears') || t.includes('chiefs') || t.includes('packers')) return 'NFL';
            if (t.includes('nba') || t.includes('basketball') || t.includes('lakers') || t.includes('celtics')) return 'NBA';
            if (t.includes('trump') || t.includes('biden') || t.includes('election') || t.includes('president')) return 'Politics';
            if (t.includes('bitcoin') || t.includes('btc') || t.includes('ethereum') || t.includes('crypto')) return 'Crypto';
            if (t.includes('fed') || t.includes('rate') || t.includes('inflation')) return 'Finance';
            return 'Other';
        }
        
        function formatPct(val) {
            if (val === undefined || val === null || !Number.isFinite(Number(val))) return '—';
            const pct = Number(val) * 100;
            if (pct > 0 && pct < 1) return pct.toFixed(1) + '%';
            return pct.toFixed(0) + '%';
        }

        function escapeHtml(value) {
            return String(value ?? '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#039;');
        }

        function safeHttpUrl(value) {
            try {
                const parsed = new URL(String(value || ''));
                return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
            } catch {
                return null;
            }
        }

        function updatePlatformOpportunity() {
            const opportunity = state.platform_opportunity || {};
            const status = document.getElementById('platformOpportunityStatus');
            const summary = document.getElementById('platformOpportunitySummary');
            const lanes = document.getElementById('platformOpportunityLanes');
            const mode = opportunity.status || 'disabled';
            status.textContent = mode.replaceAll('_', ' ').toUpperCase();
            status.style.color = mode === 'running'
                ? 'var(--accent-green)'
                : mode === 'degraded'
                    ? '#f59e0b'
                    : 'var(--text-secondary)';
            const catalog = opportunity.catalog || {};
            const monitoring = opportunity.monitoring || {};
            const worker = opportunity.worker || {};
            const hot = Array.isArray(monitoring.hot) ? monitoring.hot : [];
            const excluded = Array.isArray(monitoring.budget_excluded)
                ? monitoring.budget_excluded : [];
            const locks = Array.isArray(opportunity.political_event_locks)
                ? opportunity.political_event_locks : [];
            const researchPnl = opportunity.research_pnl || {};
            const actualExit = researchPnl.actual_exit || {};
            const strategyLanes = opportunity.strategy_lanes || {};
            summary.textContent = opportunity.enabled
                ? `${Number(catalog.contracts || 0)} catalog contracts · ${hot.length} hot · ${Number(monitoring.warm_count || 0)} warm · ${excluded.length} budget-excluded · ${Number(opportunity.relations || 0)} structural relations · execution authority: ${opportunity.execution_authority || 'none'}`
                : 'Shadow system disabled by configuration';
            const intents = opportunity.intents || {};
            const acceptance = opportunity.acceptance || {};
            const directionalAcceptance = acceptance.directional_reaction || {};
            lanes.innerHTML = [
                ['Locked arbitrage', 'existing execution lane; unchanged'],
                ['Relative value', `${(strategyLanes.relative_value || {}).authority || 'disabled'} · ${Number(intents.relative_value || 0)} shadow intents · proof pending`],
                ['Directional reaction', `${(strategyLanes.directional_reaction || {}).authority || 'disabled'} · ${Number(intents.directional_reaction || 0)} shadow intents · unvalidated forward study`],
                ['Political watchlist', locks.length
                    ? locks.map(lock => `${lock.event_title || lock.event_id} (${lock.state || 'unknown'}; ${Array.isArray(lock.sampled_contract_ids) ? lock.sampled_contract_ids.length : 0}/${Array.isArray(lock.contract_ids) ? lock.contract_ids.length : 0} sampled)`).join(' · ')
                    : 'No selected political events'],
                ['Research marks', `shadow research only · ${researchPnl.label || 'shadow_research_marks'}, not receipts or positions · main paper fills/PnL ${(opportunity.main_paper_fills_pnl || 'disabled')} · actual-exit: ${Number(actualExit.scored_marks || 0)}/${Number(actualExit.marks || 0)} scored · capacity PnL ${Number(actualExit.capacity_pnl || 0).toFixed(3)}`],
                ['Research threshold', `${directionalAcceptance.research_threshold_passed === true ? 'passed' : 'not passed'} · execution authority: ${directionalAcceptance.execution_authority || 'none'}`],
                ['Research worker', `${Number(worker.queued || 0)} queued · ${Number(worker.processed || 0)} processed · ${Number(worker.dropped || 0)} dropped · ${Number(worker.failures || 0)} failures`],
            ].map(row => `<div style="border-left: 2px solid var(--accent-green); padding-left: 0.65rem;">
                <div style="font-size: 0.82rem; font-weight: 600;">${escapeHtml(row[0])}</div>
                <div style="font-size: 0.72rem; color: var(--text-muted);">${escapeHtml(row[1])}</div>
            </div>`).join('');
        }

        function updateNewsCatalysts() {
            const news = state.news_catalysts || {};
            const status = document.getElementById('newsCatalystStatus');
            const summary = document.getElementById('newsCatalystSummary');
            const items = document.getElementById('newsCatalystItems');
            const mode = news.status || 'disabled';
            status.textContent = mode.replaceAll('_', ' ').toUpperCase();
            status.style.color = mode === 'active'
                ? 'var(--accent-green)'
                : mode === 'retrying'
                    ? '#f59e0b'
                : mode === 'error'
                    ? 'var(--accent-red)'
                    : 'var(--text-secondary)';
            summary.textContent = mode === 'retrying'
                ? `Transient upstream failure; retrying automatically · ${Number(news.api_calls_today || 0)} API units today`
                : news.enabled
                    ? `${Number(news.api_calls_today || 0)} API units today · ${
                        news.apply_priority_boost ? 'priority boost active' : 'log-only'
                    }`
                    : 'Scanner disabled by configuration';
            const rows = Array.isArray(news.items) ? news.items : [];
            if (!rows.length) {
                items.innerHTML = '<div style="color: var(--text-muted); font-size: 0.8rem;">No verified catalysts in the latest scan.</div>';
                return;
            }
            items.innerHTML = rows.slice(0, 12).map(news => {
                const source = safeHttpUrl(news.source_url);
                const matches = Array.isArray(news.matches) ? news.matches : [];
                const matchText = matches.slice(0, 4).map(match =>
                    `${escapeHtml(match.market_platform)}:${escapeHtml(match.market_id)} (${(Number(match.relevance_score || 0) * 100).toFixed(0)}%)`
                ).join(' · ');
                const headline = source
                    ? `<a href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer" style="color: var(--text-primary);">${escapeHtml(news.headline)}</a>`
                    : escapeHtml(news.headline);
                return `<div style="border-left: 2px solid var(--accent-purple); padding-left: 0.65rem;">
                    <div style="font-size: 0.85rem; font-weight: 600;">${headline}</div>
                    <div style="font-size: 0.72rem; color: var(--text-muted);">${escapeHtml(news.topic_category || 'other')} · ${matchText || 'no market above threshold'}</div>
                </div>`;
            }).join('');
        }

        function updateEventWeek() {
            const eventWeek = state.event_week || {};
            const status = document.getElementById('eventWeekStatus');
            const summary = document.getElementById('eventWeekSummary');
            const sources = document.getElementById('eventWeekSources');
            const items = document.getElementById('eventWeekItems');
            const mode = eventWeek.status || 'disabled';
            status.textContent = mode.replaceAll('_', ' ').toUpperCase();
            status.style.color = mode === 'complete'
                ? 'var(--accent-green)'
                : mode === 'partial'
                    ? '#f59e0b'
                    : mode === 'error'
                        ? 'var(--accent-red)'
                        : 'var(--text-secondary)';
            const upcoming = Array.isArray(eventWeek.upcoming_events)
                ? eventWeek.upcoming_events : [];
            const coverage = Array.isArray(eventWeek.coverage)
                ? eventWeek.coverage : [];
            const lanes = Array.isArray(eventWeek.active_lanes)
                ? eventWeek.active_lanes : [];
            const scorecard = eventWeek.scorecard || {};
            const edgeLift = scorecard.average_edge_lift == null
                ? 'baseline pending'
                : `${(Number(scorecard.average_edge_lift) * 100).toFixed(2)}¢ avg edge lift`;
            summary.textContent = eventWeek.enabled
                ? `${upcoming.length} scheduled · ${Number(eventWeek.verified_event_pairs || 0)} verified pairs · ${lanes.length} active lanes · ${Number(scorecard.evaluation_count || 0)} event-window evaluations · ${Number(scorecard.opportunity_count || 0)} opportunities · ${Number(scorecard.operational_failure_count || 0)} operational failures · ${edgeLift}`
                : 'Event lane disabled by configuration';
            const sourceRows = Array.isArray(eventWeek.calendar_sources)
                ? eventWeek.calendar_sources : [];
            sources.textContent = sourceRows.length
                ? sourceRows.map(row => `${row.source_id}: ${row.status}`).join(' · ')
                : 'No calendar source refresh recorded';
            if (!upcoming.length) {
                items.innerHTML = '<div style="color: var(--text-muted); font-size: 0.8rem;">No scheduled events in the active window.</div>';
                return;
            }
            const coverageByEvent = new Map(coverage.map(row => [row.event_id, row]));
            const laneByEvent = new Map(lanes.map(row => [row.event_id, row]));
            items.innerHTML = upcoming.slice(0, 16).map(event => {
                const source = safeHttpUrl(event.source_url);
                const eventCoverage = coverageByEvent.get(event.event_id) || {};
                const lane = laneByEvent.get(event.event_id);
                const title = source
                    ? `<a href="${escapeHtml(source)}" target="_blank" rel="noopener noreferrer" style="color: var(--text-primary);">${escapeHtml(event.title)}</a>`
                    : escapeHtml(event.title);
                const clock = new Date(event.scheduled_at).toLocaleString();
                const coverageText = `${Number(eventCoverage.polymarket_candidates || 0)}P / ${Number(eventCoverage.kalshi_candidates || 0)}K candidates · ${Number(eventCoverage.verified_pairs || 0)} verified`;
                const laneText = lane ? ` · ${escapeHtml(lane.state)} @ ${Number(lane.interval_seconds || 0)}s` : '';
                const freshnessText = event.cadence_eligible === false ? ' · stale schedule (no priority)' : '';
                return `<div style="border-left: 2px solid #f59e0b; padding-left: 0.65rem;">
                    <div style="font-size: 0.85rem; font-weight: 600;">${title}</div>
                    <div style="font-size: 0.72rem; color: var(--text-muted);">${escapeHtml(event.event_type)} · ${escapeHtml(clock)} · ${coverageText}${laneText}${freshnessText}</div>
                </div>`;
            }).join('');
        }
        
        function truncate(str, len) {
            if (!str) return '';
            const shortened = str.length > len ? str.substring(0, len) + '...' : str;
            return escapeHtml(shortened);
        }
        
        function updateMarkets() {
            const list = document.getElementById('marketList');
            const markets = state.markets || {};
            const marketIds = Object.keys(markets);
            const cp = state.cross_platform || {};
            
            // Show cross-platform matched pairs if available
            const matchedPairs = cp.matched_pairs_data || [];
            
            // If we have matched pairs from cross-platform, show those
            if (matchedPairs.length > 0) {
                list.innerHTML = matchedPairs.slice(0, 10).map((pair, idx) => {
                    const category = detectCategory(pair.poly_question || pair.kalshi_title || '');
                    const similarity = ((pair.similarity || 0) * 100).toFixed(0);
                    return `
                        <div class="market-item">
                            <div class="market-question">
                                <span class="opp-badge" style="font-size: 0.6rem; margin-right: 0.5rem;">${category}</span>
                                ${truncate(pair.poly_question || pair.kalshi_title || 'Market', 50)}
                            </div>
                            <div class="market-prices">
                                <span style="color: #8b5cf6; font-size: 0.7rem;">P: ${formatPct(pair.poly_yes)}</span>
                                <span style="color: #f7931a; font-size: 0.7rem;">K: ${formatPct(pair.kalshi_yes)}</span>
                                <span style="color: var(--text-muted); font-size: 0.65rem;">${similarity}% match</span>
                            </div>
                        </div>
                    `;
                }).join('');
                return;
            }
            
            // Show Polymarket markets if available
            if (marketIds.length === 0) {
                const polyCount = cp.polymarket_markets || 0;
                const kalshiCount = cp.kalshi_markets || 0;
                
                if (polyCount > 0 || kalshiCount > 0) {
                    list.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-icon">🔄</div>
                            <div>Loading orderbooks...</div>
                            <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.5rem;">
                                ${polyCount.toLocaleString()} Polymarket + ${kalshiCount.toLocaleString()} Kalshi markets
                            </div>
                        </div>
                    `;
                } else {
                    list.innerHTML = '<div class="empty-state"><div class="empty-icon">📈</div><div>Loading markets...</div></div>';
                }
                return;
            }
            
            list.innerHTML = marketIds.slice(0, 10).map(id => {
                const m = markets[id];
                const bid = m.best_bid_yes || 0;
                const ask = m.best_ask_yes || 0;
                const spread = ask - bid;
                
                return `
                    <div class="market-item">
                        <span class="market-name">${escapeHtml(m.question || id)}</span>
                        <span class="market-price">${bid.toFixed(2)}/${ask.toFixed(2)}</span>
                        <span class="market-spread">${(spread * 100).toFixed(1)}c</span>
                    </div>
                `;
            }).join('');
        }
        
        function formatCurrency(value) {
            const sign = value >= 0 ? '' : '-';
            return `${sign}$${Math.abs(value).toFixed(2)}`;
        }
        
        function formatTime(timestamp) {
            return formatDateTime(timestamp);
        }

        function formatDateTime(timestamp) {
            if (!timestamp) return '';
            const date = new Date(timestamp);
            if (Number.isNaN(date.getTime())) return '';
            const tz = state.display_timezone || displayTimezone;
            return `${date.toLocaleString('en-US', {
                timeZone: tz,
                month: 'numeric',
                day: 'numeric',
                hour: 'numeric',
                minute: '2-digit',
                second: '2-digit',
            })} ET`;
        }
        
        function addOpportunity(opp) {
            if (!state.opportunities) state.opportunities = [];
            state.opportunities.push(opp);
            updateOpportunities();
        }
        
        function addActivity(activity) {
            if (!state.signals) state.signals = [];
            state.signals.push(activity);
            updateActivity();
        }
        
        // Ping to keep connection alive
        setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({type: 'ping'}));
            }
        }, 25000);
        
        document.addEventListener('click', (event) => {
            const button = event.target.closest('.decision-filter');
            if (button) {
                decisionFilter = button.dataset.filter || 'all';
                document.querySelectorAll('.decision-filter').forEach(el => el.classList.remove('active'));
                button.classList.add('active');
                updateDecisions();
                return;
            }

            const paperButton = event.target.closest('.paper-history-filter');
            if (paperButton) {
                paperHistoryFilter = paperButton.dataset.paperFilter || 'all';
                document.querySelectorAll('.paper-history-filter').forEach(el => el.classList.remove('active'));
                paperButton.classList.add('active');
                updatePaperHistory();
            }
        });
        
        // Fetch initial state via REST as backup
        async function fetchState() {
            try {
                const response = await fetch('/api/state');
                state = await response.json();
                updateDashboard();
            } catch (e) {
                console.error('Failed to fetch state:', e);
            }
        }
        
        // Initial load
        connect();
        fetchState();
        
        // Periodic refresh as backup
        setInterval(fetchState, 5000);
    </script>
</body>
</html>"""


# Create the app
app = create_app()
