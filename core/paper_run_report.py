"""Read-only, ledger-backed reporting for a Nightwatch paper run."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _rows(
    connection: sqlite3.Connection, query: str, params: tuple[Any, ...]
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def build_paper_run_report(
    db_path: str | Path,
    *,
    run_id: str | None = None,
    near_miss_limit: int = 10,
    trade_limit: int = 50,
) -> dict[str, Any]:
    """Build an auditable report without acquiring the writer's process lock."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"paper database does not exist: {path}")
    if near_miss_limit <= 0 or trade_limit <= 0:
        raise ValueError("report limits must be positive")

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        if run_id is None:
            run = connection.execute(
                "SELECT id, * FROM paper_run_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            run = connection.execute(
                "SELECT id, * FROM paper_run_sessions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if run is None:
            raise ValueError("paper run was not found")
        selected_run_id = str(run["run_id"])
        run_summary = dict(run)
        run_summary["run_number"] = int(run_summary.pop("id"))

        capabilities = {
            "evaluation_funnel": _table_exists(
                connection, "cross_platform_evaluation_counts"
            ),
            "direction_evidence": _table_exists(
                connection, "cross_platform_evaluations"
            ),
            "discovery_cycles": _table_exists(connection, "semantic_discovery_cycles"),
            "paper_trade_receipts": _table_exists(
                connection, "paper_cross_platform_trades"
            )
            and _table_exists(connection, "paper_cross_platform_legs"),
        }
        funnel_rows = (
            connection.execute(
                """
                SELECT reason_code, observation_count
                FROM cross_platform_evaluation_counts
                WHERE run_id = ? ORDER BY reason_code
                """,
                (selected_run_id,),
            ).fetchall()
            if capabilities["evaluation_funnel"]
            else []
        )
        funnel = {
            str(row["reason_code"]): int(row["observation_count"])
            for row in funnel_rows
        }
        grouped_decisions = (
            _rows(
                connection,
                """
                SELECT outcome, reason_code, COUNT(*) AS count,
                       MAX(executable_net_edge) AS best_executable_net_edge
                FROM cross_platform_evaluations
                WHERE run_id = ?
                GROUP BY outcome, reason_code
                ORDER BY count DESC, outcome, reason_code
                """,
                (selected_run_id,),
            )
            if capabilities["direction_evidence"]
            else []
        )
        evidence = {
            "direction_evaluations": 0,
            "paired_snapshots": 0,
            "qualified_directions": 0,
            "age_stamped_directions": 0,
        }
        if capabilities["direction_evidence"]:
            evidence.update(
                dict(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS direction_evaluations,
                               COUNT(DISTINCT observed_at_utc || '|' || pair_id)
                                   AS paired_snapshots,
                               COALESCE(SUM(CASE WHEN outcome = 'opportunity'
                                                 THEN 1 ELSE 0 END), 0)
                                   AS qualified_directions,
                               COALESCE(SUM(
                                   CASE WHEN polymarket_age_seconds IS NOT NULL
                                             AND kalshi_age_seconds IS NOT NULL
                                        THEN 1 ELSE 0 END
                               ), 0) AS age_stamped_directions
                        FROM cross_platform_evaluations
                        WHERE run_id = ?
                        """,
                        (selected_run_id,),
                    ).fetchone()
                )
            )
        discovery = (
            connection.execute(
                """
                SELECT id AS cycle_id, completed_at_utc, reviewed_pair_count,
                       auto_approved_pair_count, metrics_json
                FROM semantic_discovery_cycles
                WHERE run_id = ? ORDER BY id DESC LIMIT 1
                """,
                (selected_run_id,),
            ).fetchone()
            if capabilities["discovery_cycles"]
            else None
        )

        near_misses = (
            _rows(
                connection,
                """
                SELECT observed_at_utc, pair_id, polymarket_question,
                       kalshi_title, token, buy_platform, sell_platform,
                       buy_price, sell_price, buy_liquidity, sell_liquidity,
                       gross_edge, fee_cost, slippage_reserve, net_edge,
                       executable_net_edge, required_net_edge, suggested_size,
                       reason_code
                FROM cross_platform_evaluations
                WHERE run_id = ? AND outcome = 'skipped'
                ORDER BY executable_net_edge DESC, observed_at_utc DESC, id DESC
                LIMIT ?
                """,
                (selected_run_id, min(near_miss_limit, 200)),
            )
            if capabilities["direction_evidence"]
            else []
        )
        trades = (
            _rows(
                connection,
                """
                SELECT * FROM paper_cross_platform_trades
                WHERE run_id = ?
                ORDER BY observed_at_utc DESC, trade_id DESC LIMIT ?
                """,
                (selected_run_id, min(trade_limit, 1000)),
            )
            if capabilities["paper_trade_receipts"]
            else []
        )
        for trade in trades:
            trade["legs"] = _rows(
                connection,
                """
                SELECT leg_role, platform, market_id, side, observed_price,
                       simulated_price, size
                FROM paper_cross_platform_legs
                WHERE trade_id = ?
                ORDER BY CASE leg_role WHEN 'buy' THEN 0 ELSE 1 END
                """,
                (trade["trade_id"],),
            )
        trade_totals = {
            "paper_trade_receipts": 0,
            "committed_capital": 0,
            "projected_locked_pnl": 0,
            "complete_two_leg_receipts": 0,
        }
        if capabilities["paper_trade_receipts"]:
            trade_totals.update(
                dict(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS paper_trade_receipts,
                               COALESCE(SUM(committed_capital), 0)
                                   AS committed_capital,
                               COALESCE(SUM(projected_locked_pnl), 0)
                                   AS projected_locked_pnl
                        FROM paper_cross_platform_trades WHERE run_id = ?
                        """,
                        (selected_run_id,),
                    ).fetchone()
                )
            )
            complete_receipts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM (
                    SELECT trade_id FROM paper_cross_platform_legs
                    WHERE trade_id IN (
                        SELECT trade_id FROM paper_cross_platform_trades
                        WHERE run_id = ?
                    )
                    GROUP BY trade_id HAVING COUNT(*) = 2
                )
                """,
                (selected_run_id,),
            ).fetchone()
            trade_totals["complete_two_leg_receipts"] = int(complete_receipts["count"])

        if not capabilities["direction_evidence"]:
            evidence_status = "legacy_run_not_auditable"
        elif trade_totals["paper_trade_receipts"]:
            evidence_status = "paper_trades_recorded"
        elif evidence["direction_evaluations"]:
            evidence_status = "evaluated_no_trade"
        else:
            evidence_status = "no_direction_evidence"

        return {
            "report_source": "sqlite_read_only",
            "database": str(path),
            "run": run_summary,
            "schema_capabilities": capabilities,
            "evidence_status": evidence_status,
            "evaluation_funnel": funnel,
            "direction_evidence": evidence,
            "decision_outcomes": grouped_decisions,
            "latest_discovery_cycle": dict(discovery) if discovery else None,
            "top_near_misses": near_misses,
            "paper_performance": {
                **trade_totals,
                "cash_starting_equity": float(run["starting_equity"]),
                "reported_ending_equity": float(run["ending_equity"]),
                "reported_pnl": float(run["pnl"]),
                "pnl_source": str(run["pnl_source"]),
                "realized_settlement_pnl": 0.0,
            },
            "paper_trade_receipts": trades,
        }
    finally:
        connection.close()
