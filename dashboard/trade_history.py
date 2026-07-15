"""
Trade history page helpers and enriched API payloads.
"""

from __future__ import annotations

from typing import Any, Optional

REASON_LABELS = {
    "paper_order_placed": "Paper order placed",
    "hypothetical_paper_fill": "Hypothetical paper fill",
    "slippage": "Blocked by slippage check",
    "risk_limit": "Blocked by risk limits",
    "order_error": "Order placement failed",
    "order_cancelled": "Order cancelled",
    "order_timeout": "Order timed out",
    "order_filled": "Order filled",
}


def human_reason(reason_code: str | None) -> str:
    if not reason_code:
        return "No reason recorded"
    return REASON_LABELS.get(reason_code, reason_code.replace("_", " ").title())


def format_trade_summary(event: dict[str, Any]) -> str:
    market_question = event.get("market_question") or event.get("market_id") or "Unknown market"
    side = (event.get("side") or "").upper()
    token = (event.get("token_type") or "").upper()
    size = event.get("size")
    price = event.get("price")
    event_type = (event.get("event_type") or "event").upper()

    if side and token and size is not None and price is not None:
        return f"{event_type}: {side} {token} {float(size):.2f} @ ${float(price):.4f} · {market_question}"

    if event_type == "REJECTED":
        return f"Rejected: {human_reason(event.get('reason_code'))}"

    return event_type.title()


def _match_decisions(event: dict[str, Any], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order_id = event.get("order_id")
    trade_id = event.get("trade_id")
    signal_id = event.get("signal_id")
    matched: list[dict[str, Any]] = []

    for decision in decisions:
        evidence = decision.get("evidence") or {}
        if order_id and (
            decision.get("related_id") == order_id
            or evidence.get("order_id") == order_id
        ):
            matched.append(decision)
        elif trade_id and evidence.get("trade_id") == trade_id:
            matched.append(decision)
        elif signal_id and decision.get("related_id") == signal_id:
            matched.append(decision)

    return matched


def build_trade_history_payload(
    *,
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    display_timezone: str,
    mode: str,
    timeline_for_order=None,
) -> dict[str, Any]:
    """Build enriched trade history entries for the history page API."""
    timeline_for_order = timeline_for_order or (lambda _order_id: [])

    entries: list[dict[str, Any]] = []
    for event in events:
        linked_decisions = _match_decisions(event, decisions)
        order_id = event.get("order_id")
        timeline = timeline_for_order(order_id) if order_id else [event]
        if not timeline:
            timeline = [event]

        reason_detail = event.get("reason_detail") or ""
        if not reason_detail and linked_decisions:
            reason_detail = linked_decisions[0].get("explanation") or ""

        entries.append({
            **event,
            "entry_id": event.get("event_id") or f"row-{event.get('id')}",
            "summary": format_trade_summary(event),
            "reason_title": human_reason(event.get("reason_code")),
            "reason_explanation": reason_detail or human_reason(event.get("reason_code")),
            "timeline": timeline,
            "decisions": linked_decisions,
            "is_paper": bool(event.get("is_simulated", True)),
            "mode": mode,
        })

    return {
        "display_timezone": display_timezone,
        "mode": mode,
        "entries": entries,
        "total": len(entries),
    }


def get_trade_history_html() -> str:
    """Return the standalone trade history page."""
    return TRADE_HISTORY_HTML


TRADE_HISTORY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trade History · Polymarket Arbitrage</title>
    <style>
        :root {
            --bg: #0b0e14;
            --panel: #121722;
            --panel-2: #171d2b;
            --border: #2a3347;
            --text: #eef2ff;
            --muted: #8b97b3;
            --blue: #4d8dff;
            --blue-soft: rgba(77, 141, 255, 0.12);
            --green: #22c55e;
            --green-soft: rgba(34, 197, 94, 0.14);
            --red: #ef4444;
            --red-soft: rgba(239, 68, 68, 0.14);
            --yellow: #f59e0b;
            --yellow-soft: rgba(245, 158, 11, 0.14);
            --purple: #a78bfa;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: Inter, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: radial-gradient(circle at top, #141b2d 0%, var(--bg) 45%);
            color: var(--text);
            min-height: 100vh;
        }

        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            padding: 1rem 1.5rem;
            border-bottom: 1px solid var(--border);
            background: rgba(18, 23, 34, 0.92);
            backdrop-filter: blur(10px);
            position: sticky;
            top: 0;
            z-index: 10;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .brand h1 {
            font-size: 1.1rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .brand p {
            font-size: 0.78rem;
            color: var(--muted);
        }

        .nav-links {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .nav-links a {
            color: var(--muted);
            text-decoration: none;
            font-size: 0.85rem;
            padding: 0.45rem 0.75rem;
            border-radius: 8px;
            border: 1px solid transparent;
        }

        .nav-links a:hover,
        .nav-links a.active {
            color: var(--text);
            border-color: var(--border);
            background: var(--panel);
        }

        .pill {
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            padding: 0.35rem 0.65rem;
            border-radius: 999px;
            background: var(--yellow-soft);
            color: var(--yellow);
        }

        .layout {
            display: grid;
            grid-template-columns: minmax(360px, 42%) 1fr;
            gap: 1rem;
            padding: 1rem 1.5rem 2rem;
            min-height: calc(100vh - 72px);
        }

        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }

        .panel-header {
            padding: 1rem 1.1rem;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
        }

        .panel-header h2 {
            font-size: 0.95rem;
            font-weight: 600;
        }

        .filters {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
        }

        .filter-btn {
            border: 1px solid var(--border);
            background: var(--panel-2);
            color: var(--muted);
            border-radius: 999px;
            padding: 0.35rem 0.7rem;
            font-size: 0.72rem;
            cursor: pointer;
        }

        .filter-btn.active {
            color: var(--blue);
            border-color: rgba(77, 141, 255, 0.45);
            background: var(--blue-soft);
        }

        .search {
            width: 100%;
            margin-top: 0.75rem;
            background: var(--panel-2);
            border: 1px solid var(--border);
            color: var(--text);
            border-radius: 10px;
            padding: 0.65rem 0.8rem;
            font-size: 0.85rem;
        }

        .trade-list {
            overflow: auto;
            flex: 1;
        }

        .trade-row {
            display: grid;
            grid-template-columns: 110px 1fr auto;
            gap: 0.75rem;
            padding: 0.9rem 1rem;
            border-bottom: 1px solid rgba(42, 51, 71, 0.65);
            cursor: pointer;
            transition: background 0.15s ease;
        }

        .trade-row:hover { background: rgba(77, 141, 255, 0.06); }
        .trade-row.selected {
            background: var(--blue-soft);
            border-left: 3px solid var(--blue);
            padding-left: calc(1rem - 3px);
        }

        .trade-date {
            font-size: 0.74rem;
            color: var(--muted);
            line-height: 1.35;
        }

        .trade-main {
            min-width: 0;
        }

        .trade-title {
            font-size: 0.88rem;
            font-weight: 600;
            margin-bottom: 0.2rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .trade-sub {
            font-size: 0.76rem;
            color: var(--muted);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .trade-amount {
            text-align: right;
            font-size: 0.84rem;
            font-weight: 600;
            white-space: nowrap;
        }

        .badge {
            display: inline-block;
            font-size: 0.68rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            padding: 0.18rem 0.45rem;
            border-radius: 999px;
            margin-bottom: 0.35rem;
        }

        .badge.filled { background: var(--green-soft); color: var(--green); }
        .badge.placed { background: var(--blue-soft); color: var(--blue); }
        .badge.rejected { background: var(--red-soft); color: var(--red); }
        .badge.cancelled, .badge.expired { background: var(--yellow-soft); color: var(--yellow); }

        .detail-body {
            padding: 1.25rem 1.35rem 1.5rem;
            overflow: auto;
        }

        .detail-empty {
            color: var(--muted);
            padding: 3rem 1rem;
            text-align: center;
        }

        .detail-headline {
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            align-items: flex-start;
            margin-bottom: 1rem;
        }

        .detail-headline h3 {
            font-size: 1.35rem;
            line-height: 1.2;
            margin-bottom: 0.35rem;
        }

        .detail-headline p {
            color: var(--muted);
            font-size: 0.85rem;
        }

        .detail-amount {
            font-size: 1.4rem;
            font-weight: 700;
            text-align: right;
        }

        .reason-box {
            background: var(--panel-2);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1rem;
            margin-bottom: 1rem;
        }

        .reason-box h4 {
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--muted);
            margin-bottom: 0.45rem;
        }

        .reason-box strong {
            display: block;
            font-size: 0.95rem;
            margin-bottom: 0.35rem;
        }

        .reason-box p {
            color: #c7d2fe;
            font-size: 0.88rem;
            line-height: 1.5;
        }

        .grid-2 {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 0.75rem;
            margin-bottom: 1rem;
        }

        .kv {
            background: var(--panel-2);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 0.75rem 0.85rem;
        }

        .kv label {
            display: block;
            font-size: 0.72rem;
            color: var(--muted);
            margin-bottom: 0.25rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .kv span {
            font-size: 0.9rem;
            font-weight: 600;
            word-break: break-word;
        }

        .section-title {
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--muted);
            margin: 1rem 0 0.6rem;
        }

        .timeline {
            border-left: 2px solid var(--border);
            margin-left: 0.4rem;
            padding-left: 1rem;
        }

        .timeline-item {
            position: relative;
            padding-bottom: 0.85rem;
        }

        .timeline-item::before {
            content: "";
            position: absolute;
            left: -1.28rem;
            top: 0.25rem;
            width: 0.55rem;
            height: 0.55rem;
            border-radius: 50%;
            background: var(--blue);
            box-shadow: 0 0 0 4px rgba(77, 141, 255, 0.15);
        }

        .timeline-item strong {
            display: block;
            font-size: 0.84rem;
            margin-bottom: 0.15rem;
        }

        .timeline-item span {
            font-size: 0.76rem;
            color: var(--muted);
        }

        .decision-card {
            background: var(--panel-2);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 0.85rem;
            margin-bottom: 0.65rem;
        }

        .decision-card .meta {
            font-size: 0.72rem;
            color: var(--muted);
            margin-bottom: 0.35rem;
        }

        .decision-card p {
            font-size: 0.84rem;
            line-height: 1.45;
        }

        .footnote {
            margin-top: 1rem;
            font-size: 0.75rem;
            color: var(--muted);
        }

        @media (max-width: 980px) {
            .layout {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <header class="topbar">
        <div class="brand">
            <div>
                <h1>Trade History</h1>
                <p>Click any row for full trade details and reasoning</p>
            </div>
        </div>
        <div class="nav-links">
            <span class="pill" id="modePill">PAPER</span>
            <a href="/">Dashboard</a>
            <a href="/history" class="active">Trade History</a>
        </div>
    </header>

    <main class="layout">
        <section class="panel">
            <div class="panel-header">
                <div style="width:100%">
                    <h2>Recent Activity</h2>
                    <div class="filters" id="filters">
                        <button class="filter-btn active" data-filter="all">All</button>
                        <button class="filter-btn" data-filter="filled">Filled</button>
                        <button class="filter-btn" data-filter="placed">Placed</button>
                        <button class="filter-btn" data-filter="rejected">Rejected</button>
                        <button class="filter-btn" data-filter="cancelled">Cancelled</button>
                        <button class="filter-btn" data-filter="expired">Expired</button>
                    </div>
                    <input class="search" id="searchInput" placeholder="Search market, strategy, order id, reason..." />
                </div>
            </div>
            <div class="trade-list" id="tradeList">
                <div class="detail-empty">Loading trade history...</div>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <h2>Trade Details</h2>
                <span id="detailUpdated" style="font-size:0.75rem;color:var(--muted);"></span>
            </div>
            <div class="detail-body" id="detailPanel">
                <div class="detail-empty">Select a trade on the left to inspect it.</div>
            </div>
        </section>
    </main>

    <script>
        let entries = [];
        let selectedId = null;
        let activeFilter = 'all';
        let displayTimezone = 'America/New_York';

        function formatDateTime(timestamp) {
            if (!timestamp) return '--';
            const date = new Date(timestamp);
            if (Number.isNaN(date.getTime())) return '--';
            return date.toLocaleString('en-US', {
                timeZone: displayTimezone,
                month: 'short',
                day: 'numeric',
                year: 'numeric',
                hour: 'numeric',
                minute: '2-digit',
                second: '2-digit',
            }) + ' ET';
        }

        function formatCurrency(value) {
            if (value == null || Number.isNaN(Number(value))) return '--';
            const num = Number(value);
            const sign = num < 0 ? '-' : '';
            return `${sign}$${Math.abs(num).toFixed(2)}`;
        }

        function sideClass(side, eventType) {
            if (eventType === 'rejected') return 'rejected';
            if ((side || '').toLowerCase() === 'buy') return 'filled';
            if ((side || '').toLowerCase() === 'sell') return 'placed';
            return eventType || 'placed';
        }

        function filteredEntries() {
            const query = (document.getElementById('searchInput').value || '').trim().toLowerCase();
            return entries.filter(entry => {
                if (activeFilter !== 'all' && entry.event_type !== activeFilter) return false;
                if (!query) return true;
                const haystack = [
                    entry.market_question,
                    entry.market_id,
                    entry.strategy_tag,
                    entry.order_id,
                    entry.trade_id,
                    entry.reason_code,
                    entry.reason_title,
                    entry.reason_explanation,
                    entry.summary,
                    entry.event_type,
                ].join(' ').toLowerCase();
                return haystack.includes(query);
            });
        }

        function renderList() {
            const list = document.getElementById('tradeList');
            const visible = filteredEntries();

            if (!visible.length) {
                list.innerHTML = '<div class="detail-empty">No trades match your filters.</div>';
                return;
            }

            list.innerHTML = visible.map(entry => {
                const selected = entry.entry_id === selectedId ? 'selected' : '';
                const badge = entry.event_type || 'event';
                const amount = entry.notional != null ? formatCurrency(entry.notional) : '--';
                const contractName = entry.market_question || entry.market_id || 'Unknown market';
                const meta = [entry.reason_title || '', entry.market_id || ''].filter(Boolean).join(' · ');
                return `
                    <article class="trade-row ${selected}" data-entry-id="${entry.entry_id}">
                        <div class="trade-date">${formatDateTime(entry.event_at_utc)}</div>
                        <div class="trade-main">
                            <span class="badge ${badge}">${badge}</span>
                            <div class="trade-title">${contractName}</div>
                            <div class="trade-sub">${meta || entry.summary || 'Trade event'}</div>
                        </div>
                        <div class="trade-amount">${amount}</div>
                    </article>
                `;
            }).join('');
        }

        function renderDetail(entry) {
            const panel = document.getElementById('detailPanel');
            if (!entry) {
                panel.innerHTML = '<div class="detail-empty">Select a trade on the left to inspect it.</div>';
                return;
            }

            const contractName = entry.market_question || entry.market_id || 'Unknown market';
            const timeline = (entry.timeline || []).map(item => `
                <div class="timeline-item">
                    <strong>${(item.event_type || 'event').toUpperCase()} · ${item.reason_code || 'n/a'}</strong>
                    <span>${formatDateTime(item.event_at_utc)} · ${item.reason_detail || item.reason_code || ''}</span>
                </div>
            `).join('');

            const decisions = (entry.decisions || []).map(decision => `
                <div class="decision-card">
                    <div class="meta">${(decision.outcome || 'decision').toUpperCase()} · ${decision.strategy || 'unknown'} · ${formatDateTime(decision.timestamp)}</div>
                    <p>${decision.explanation || decision.reason_code || 'No explanation recorded.'}</p>
                </div>
            `).join('') || '<div class="detail-empty" style="padding:1rem;">No linked decision journal entries.</div>';

            panel.innerHTML = `
                <div class="detail-headline">
                    <div>
                        <span class="badge ${entry.event_type || 'placed'}">${(entry.event_type || 'event').toUpperCase()}</span>
                        <h3>${contractName}</h3>
                        <p>${entry.market_id || 'Unknown market'} · ${entry.strategy_tag || 'No strategy tag'}</p>
                    </div>
                    <div class="detail-amount">${entry.notional != null ? formatCurrency(entry.notional) : '--'}</div>
                </div>

                <div class="reason-box">
                    <h4>Why this happened</h4>
                    <strong>${entry.reason_title || 'Reason unavailable'}</strong>
                    <p>${entry.reason_explanation || 'No additional explanation was recorded for this event.'}</p>
                </div>

                <div class="grid-2">
                    <div class="kv"><label>Contract</label><span>${contractName}</span></div>
                    <div class="kv"><label>Market ID</label><span>${entry.market_id || '--'}</span></div>
                    <div class="kv"><label>Side</label><span>${(entry.side || '--').toUpperCase()}</span></div>
                    <div class="kv"><label>Token</label><span>${(entry.token_type || '--').toUpperCase()}</span></div>
                    <div class="kv"><label>Price</label><span>${entry.price != null ? Number(entry.price).toFixed(4) : '--'}</span></div>
                    <div class="kv"><label>Size</label><span>${entry.size != null ? Number(entry.size).toFixed(2) : '--'}</span></div>
                    <div class="kv"><label>Fee</label><span>${entry.fee != null ? formatCurrency(entry.fee) : '--'}</span></div>
                    <div class="kv"><label>PnL Source</label><span>${entry.pnl_source || (entry.is_paper ? 'hypothetical_paper' : 'live')}</span></div>
                    <div class="kv"><label>Order ID</label><span>${entry.order_id || '--'}</span></div>
                    <div class="kv"><label>Trade ID</label><span>${entry.trade_id || '--'}</span></div>
                </div>

                <div class="section-title">Lifecycle</div>
                <div class="timeline">${timeline || '<div class="detail-empty" style="padding:0.5rem 0;">Single event only.</div>'}</div>

                <div class="section-title">Decision Journal</div>
                ${decisions}

                <div class="footnote">
                    Times shown in Eastern Time (${displayTimezone}). Paper trades are simulated and do not represent real-money execution unless live mode is enabled.
                </div>
            `;

            document.getElementById('detailUpdated').textContent = `Updated ${formatDateTime(new Date().toISOString())}`;
        }

        function selectEntry(entryId) {
            selectedId = entryId;
            const entry = entries.find(item => item.entry_id === entryId);
            renderList();
            renderDetail(entry);
        }

        async function loadHistory() {
            try {
                const response = await fetch('/api/trade-history?limit=500');
                const payload = await response.json();
                entries = payload.entries || [];
                displayTimezone = payload.display_timezone || displayTimezone;
                document.getElementById('modePill').textContent = (payload.mode || 'dry_run').replace('_', ' ').toUpperCase();

                if (!selectedId && entries.length) {
                    selectedId = entries[0].entry_id;
                } else if (selectedId && !entries.some(item => item.entry_id === selectedId) && entries.length) {
                    selectedId = entries[0].entry_id;
                }

                renderList();
                renderDetail(entries.find(item => item.entry_id === selectedId));
            } catch (error) {
                document.getElementById('tradeList').innerHTML = '<div class="detail-empty">Failed to load trade history.</div>';
                console.error(error);
            }
        }

        document.getElementById('filters').addEventListener('click', (event) => {
            const button = event.target.closest('.filter-btn');
            if (!button) return;
            activeFilter = button.dataset.filter || 'all';
            document.querySelectorAll('.filter-btn').forEach(el => el.classList.remove('active'));
            button.classList.add('active');
            renderList();
        });

        document.getElementById('searchInput').addEventListener('input', renderList);

        document.getElementById('tradeList').addEventListener('click', (event) => {
            const row = event.target.closest('.trade-row');
            if (!row) return;
            selectEntry(row.dataset.entryId);
        });

        loadHistory();
        setInterval(loadHistory, 5000);
    </script>
</body>
</html>"""
