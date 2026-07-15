from datetime import datetime, timezone

from dashboard.trade_history import (
    build_trade_history_payload,
    format_trade_summary,
    get_trade_history_html,
    human_reason,
)


def test_human_reason_maps_known_codes():
    assert human_reason("slippage") == "Blocked by slippage check"
    assert human_reason("custom_code") == "Custom Code"


def test_format_trade_summary_for_fill():
    summary = format_trade_summary({
        "event_type": "filled",
        "side": "buy",
        "token_type": "yes",
        "size": 10,
        "price": 0.5,
    })
    assert "BUY YES 10.00 @ $0.5000" in summary


def test_build_trade_history_payload_links_decisions_and_timeline():
    events = [{
        "id": 1,
        "event_id": "evt-1",
        "event_type": "filled",
        "event_at_utc": "2026-06-29T14:00:00Z",
        "order_id": "order-1",
        "trade_id": "trade-1",
        "market_id": "market-1",
        "side": "buy",
        "token_type": "yes",
        "price": 0.5,
        "size": 10,
        "notional": 5,
        "reason_code": "hypothetical_paper_fill",
        "reason_detail": "Simulated fill for evaluation",
    }]
    decisions = [{
        "decision_id": "dec-1",
        "strategy": "execution",
        "outcome": "trade",
        "reason_code": "hypothetical_paper_fill",
        "explanation": "Hypothetical paper fill simulated for strategy evaluation.",
        "timestamp": "2026-06-29T14:00:01Z",
        "related_id": "order-1",
        "evidence": {"order_id": "order-1", "trade_id": "trade-1"},
    }]
    timeline = [
        {"event_type": "placed", "event_at_utc": "2026-06-29T13:59:00Z", "reason_code": "paper_order_placed"},
        events[0],
    ]

    payload = build_trade_history_payload(
        events=events,
        decisions=decisions,
        display_timezone="America/New_York",
        mode="dry_run",
        timeline_for_order=lambda order_id: timeline if order_id == "order-1" else [],
    )

    entry = payload["entries"][0]
    assert entry["summary"].startswith("FILLED:")
    assert entry["reason_title"] == "Hypothetical paper fill"
    assert entry["decisions"][0]["decision_id"] == "dec-1"
    assert len(entry["timeline"]) == 2
    assert payload["display_timezone"] == "America/New_York"


def test_trade_history_html_contains_master_detail_markers():
    html = get_trade_history_html()
    assert "Trade History" in html
    assert 'id="tradeList"' in html
    assert 'id="detailPanel"' in html
    assert "/api/trade-history" in html
