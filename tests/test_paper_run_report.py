from datetime import datetime, timezone
import sqlite3

from core.paper_run_report import build_paper_run_report
from utils.paper_trade_store import PaperTradeStore


def _evaluation(**overrides):
    evaluation = {
        "pair_id": "poly:1|kalshi:KX-1",
        "polymarket_id": "condition-1",
        "kalshi_ticker": "KX-1",
        "polymarket_question": "Will Alice win?",
        "kalshi_title": "Alice wins?",
        "token": "YES",
        "buy_platform": "polymarket",
        "sell_platform": "kalshi",
        "buy_price": 0.50,
        "sell_price": 0.51,
        "buy_liquidity": 100.0,
        "sell_liquidity": 90.0,
        "polymarket_yes_bid": 0.49,
        "polymarket_yes_ask": 0.50,
        "polymarket_no_bid": 0.49,
        "polymarket_no_ask": 0.50,
        "polymarket_yes_bid_size": 100.0,
        "polymarket_yes_ask_size": 110.0,
        "polymarket_no_bid_size": 90.0,
        "polymarket_no_ask_size": 95.0,
        "kalshi_yes_bid": 0.51,
        "kalshi_yes_ask": 0.52,
        "kalshi_no_bid": 0.48,
        "kalshi_no_ask": 0.49,
        "kalshi_yes_bid_size": 80.0,
        "kalshi_yes_ask_size": 85.0,
        "kalshi_no_bid_size": 75.0,
        "kalshi_no_ask_size": 70.0,
        "polymarket_age_seconds": 0.10,
        "kalshi_age_seconds": 0.12,
        "gross_edge": 0.01,
        "fee_cost": 0.006,
        "slippage_reserve": 0.02,
        "net_edge": 0.004,
        "executable_net_edge": -0.016,
        "required_net_edge": 0.02,
        "suggested_size": 10.0,
        "outcome": "skipped",
        "reason_code": "edge_below_threshold",
    }
    evaluation.update(overrides)
    return evaluation


def test_report_reads_active_run_without_taking_writer_lock(tmp_path):
    db_path = tmp_path / "paper.db"
    store = PaperTradeStore(str(db_path))
    try:
        run = store.start_run(
            starting_equity=5000.0,
            pnl_source="projected_locked_paper",
        )
        store.record_cross_platform_evaluation_counts(
            {"pair_due": 1, "paired_snapshot_fresh": 1}
        )
        observed_at = datetime.now(timezone.utc)
        store.record_cross_platform_evaluations(
            [
                _evaluation(),
                _evaluation(
                    token="NO",
                    buy_platform="kalshi",
                    sell_platform="polymarket",
                    executable_net_edge=0.03,
                    outcome="opportunity",
                    reason_code="opportunity_detected",
                ),
            ],
            observed_at=observed_at,
        )

        report = build_paper_run_report(db_path, run_id=run.run_id)

        assert report["report_source"] == "sqlite_read_only"
        assert report["run"]["run_id"] == run.run_id
        assert report["run"]["starting_equity"] == 5000.0
        assert report["evidence_status"] == "evaluated_no_trade"
        assert report["evaluation_funnel"]["pair_due"] == 1
        assert report["direction_evidence"]["direction_evaluations"] == 2
        assert report["direction_evidence"]["paired_snapshots"] == 1
        assert report["direction_evidence"]["qualified_directions"] == 1
        assert report["top_near_misses"][0]["executable_net_edge"] == -0.016
        assert report["paper_performance"]["paper_trade_receipts"] == 0
        assert report["paper_performance"]["realized_settlement_pnl"] == 0.0
    finally:
        store.close()


def test_report_selects_latest_run_by_default(tmp_path):
    db_path = tmp_path / "paper.db"
    store = PaperTradeStore(str(db_path))
    try:
        first = store.start_run(starting_equity=1000, pnl_source="paper")
        store.finish_run(ending_equity=1000, pnl=0)
        second = store.start_run(starting_equity=5000, pnl_source="paper")

        report = build_paper_run_report(db_path)

        assert report["run"]["run_id"] == second.run_id
        assert report["run"]["run_id"] != first.run_id
        assert report["evidence_status"] == "no_direction_evidence"
    finally:
        store.close()


def test_report_marks_legacy_run_without_evaluation_tables_not_auditable(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("""
        CREATE TABLE paper_run_sessions (
            id INTEGER PRIMARY KEY, run_id TEXT, status TEXT,
            started_at_utc TEXT, last_heartbeat_at_utc TEXT,
            ended_at_utc TEXT, elapsed_seconds REAL,
            starting_equity REAL, ending_equity REAL, pnl REAL,
            pnl_source TEXT, transaction_count INTEGER, placed_count INTEGER,
            filled_count INTEGER, rejected_count INTEGER,
            cancelled_count INTEGER, expired_count INTEGER
        )
        """)
    connection.execute("""
        INSERT INTO paper_run_sessions VALUES (
            18, 'legacy-run', 'completed', '2026-08-05T00:00:00Z',
            '2026-08-05T07:30:00Z', '2026-08-05T07:30:00Z', 27000,
            5000, 5000, 0, 'legacy', 0, 0, 0, 0, 0, 0
        )
        """)
    connection.commit()
    connection.close()

    report = build_paper_run_report(db_path)

    assert report["run"]["run_number"] == 18
    assert report["evidence_status"] == "legacy_run_not_auditable"
    assert report["schema_capabilities"]["direction_evidence"] is False
    assert report["top_near_misses"] == []
