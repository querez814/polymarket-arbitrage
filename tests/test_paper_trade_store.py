from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from utils.paper_trade_store import PaperTradeStore


@pytest.fixture
def store(tmp_path):
    db = PaperTradeStore(str(tmp_path / "paper_trades.db"))
    yield db
    db.close()


def test_schema_requires_event_at_utc(store):
    event = store.record_event(event_type="placed", reason_code="paper_order_placed")
    assert event is not None
    assert event.event_at_utc.endswith("Z")
    assert event.id is not None


def test_placed_filled_cancelled_chain_linked_by_order_id(store):
    order_id = "order-chain-1"
    placed_at = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    filled_at = placed_at + timedelta(seconds=5)
    cancelled_at = filled_at + timedelta(seconds=10)

    store.record_event(
        event_type="placed",
        order_id=order_id,
        event_at=placed_at,
        market_id="market-1",
        market_question="Will this contract resolve yes?",
        side="buy",
        price=0.5,
        size=10.0,
        reason_code="paper_order_placed",
    )
    store.record_event(
        event_type="filled",
        order_id=order_id,
        trade_id="trade-1",
        event_at=filled_at,
        market_id="market-1",
        side="buy",
        price=0.5,
        size=10.0,
        reason_code="hypothetical_paper_fill",
    )
    store.record_event(
        event_type="cancelled",
        order_id=order_id,
        event_at=cancelled_at,
        reason_code="order_cancelled",
    )

    chain = store.events_for_order(order_id)
    assert [event.event_type for event in chain] == ["placed", "filled", "cancelled"]
    assert chain[0].event_at_utc == "2026-06-29T12:00:00Z"
    assert chain[0].market_question == "Will this contract resolve yes?"


def test_reject_without_order_id_persists_with_signal_id(store):
    event = store.record_event(
        event_type="rejected",
        signal_id="signal-abc",
        market_id="market-2",
        reason_code="risk_limit",
        reason_detail="max exposure exceeded",
    )
    assert event is not None
    assert event.order_id is None
    assert event.signal_id == "signal-abc"


def test_recent_events_ordering_and_filtering(store):
    base = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)
    store.record_event(event_type="placed", order_id="o1", event_at=base)
    store.record_event(
        event_type="rejected",
        signal_id="s1",
        event_at=base + timedelta(seconds=1),
        reason_code="slippage",
    )
    store.record_event(
        event_type="filled",
        order_id="o2",
        trade_id="t2",
        event_at=base + timedelta(seconds=2),
        reason_code="hypothetical_paper_fill",
    )

    recent = store.recent_events(limit=10)
    assert [event.event_type for event in recent] == ["filled", "rejected", "placed"]

    rejected_only = store.recent_events(limit=10, event_type="rejected")
    assert len(rejected_only) == 1
    assert rejected_only[0].reason_code == "slippage"


def test_run_session_numbers_launches_and_counts_filled_transactions(store):
    started_at = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

    run = store.start_run(
        starting_equity=1000.0,
        pnl_source="projected_locked_paper",
        started_at=started_at,
    )
    store.record_event(event_type="placed", order_id="order-1")
    store.record_event(event_type="filled", order_id="order-1", trade_id="trade-1")
    store.checkpoint_run(
        current_equity=1012.5,
        pnl=12.5,
        checkpoint_at=started_at + timedelta(minutes=5),
    )
    finished = store.finish_run(
        ending_equity=1012.5,
        pnl=12.5,
        ended_at=started_at + timedelta(minutes=10),
    )

    assert run.run_number == 1
    assert finished.status == "completed"
    assert finished.elapsed_seconds == 600.0
    assert finished.transaction_count == 1
    assert finished.placed_count == 1
    assert finished.filled_count == 1
    assert finished.pnl == 12.5
    assert finished.ending_equity == 1012.5
    assert store.recent_runs(limit=10) == [finished]


def test_new_run_marks_unclosed_predecessor_interrupted(tmp_path):
    db_path = tmp_path / "paper_trades.db"
    first_store = PaperTradeStore(str(db_path))
    first = first_store.start_run(
        starting_equity=1000.0,
        pnl_source="projected_locked_paper",
        started_at=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
    )
    first_store.checkpoint_run(
        current_equity=1004.0,
        pnl=4.0,
        checkpoint_at=datetime(2026, 7, 22, 12, 3, tzinfo=timezone.utc),
    )
    first_store.close()

    second_store = PaperTradeStore(str(db_path))
    second = second_store.start_run(
        starting_equity=1000.0,
        pnl_source="projected_locked_paper",
        started_at=datetime(2026, 7, 22, 12, 10, tzinfo=timezone.utc),
    )
    runs = second_store.recent_runs(limit=10)
    second_store.close()

    assert second.run_number == first.run_number + 1
    assert runs[1].status == "interrupted"
    assert runs[1].ended_at_utc == "2026-07-22T12:03:00Z"
    assert runs[1].elapsed_seconds == 180.0
    assert runs[1].pnl == 4.0


def test_existing_event_database_migrates_to_run_sessions_in_place(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("""
        CREATE TABLE paper_trade_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            event_at_utc TEXT NOT NULL,
            order_id TEXT,
            trade_id TEXT,
            signal_id TEXT,
            market_id TEXT,
            market_question TEXT,
            token_type TEXT,
            side TEXT,
            price REAL,
            size REAL,
            notional REAL,
            fee REAL,
            strategy_tag TEXT,
            status TEXT,
            reason_code TEXT,
            reason_detail TEXT,
            is_simulated INTEGER NOT NULL DEFAULT 1,
            simulation_label TEXT,
            pnl_source TEXT
        )
        """)
    connection.commit()
    connection.close()

    migrated = PaperTradeStore(str(db_path))
    run = migrated.start_run(
        starting_equity=1000.0,
        pnl_source="projected_locked_paper",
    )
    event = migrated.record_event(event_type="placed", order_id="order-legacy")
    migrated.close()

    assert run.run_number == 1
    assert event is not None
    assert event.run_id == run.run_id


def test_database_allows_only_one_run_writer_process(tmp_path):
    db_path = tmp_path / "paper.db"
    first = PaperTradeStore(str(db_path))
    try:
        with pytest.raises(RuntimeError, match="already has an active process"):
            PaperTradeStore(str(db_path))
    finally:
        first.close()

    reopened = PaperTradeStore(str(db_path))
    reopened.close()


def test_semantic_pair_review_queue_is_persistent_and_upserted(tmp_path):
    store = PaperTradeStore(str(tmp_path / "paper.db"))

    for score, confidence, reasons in (
        (0.91, 0.96, ("same cutoff",)),
        (0.93, 0.97, ("same cutoff", "same resolution")),
    ):
        store.record_pair_review(
            pair_id="poly:1|kalshi:KX-1",
            polymarket_id="condition-1",
            kalshi_ticker="KX-1",
            polymarket_question="Will Alice win?",
            kalshi_title="Alice wins?",
            relation="equivalent",
            retrieval_score=score,
            verification_confidence=confidence,
            verification_reasons=reasons,
            approval_status="auto_approved",
        )

    rows = store.recent_pair_reviews()
    assert len(rows) == 1
    assert rows[0]["verification_confidence"] == pytest.approx(0.97)
    assert rows[0]["approval_status"] == "auto_approved"
    assert rows[0]["seen_count"] == 2
    store.close()


def test_semantic_discovery_cycle_persists_every_reviewed_pair_atomically(tmp_path):
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        run = store.start_run(
            starting_equity=5000.0,
            pnl_source="projected_locked_paper",
        )
        pairs = [
            {
                "pair_id": f"poly:{index}|kalshi:KX-{index}",
                "polymarket_id": f"condition-{index}",
                "kalshi_ticker": f"KX-{index}",
                "polymarket_question": f"Will candidate {index} win?",
                "kalshi_title": f"Candidate {index} wins?",
                "relation": "unverified",
                "retrieval_score": 0.80,
                "verification_confidence": 0.65,
                "verification_reasons": ("manual review required",),
                "approval_status": "manual_review",
            }
            for index in range(125)
        ]

        cycle_id = store.record_semantic_discovery_cycle(
            pairs=pairs,
            metrics={"retrieved_candidates": 125, "verified_pairs": 0},
        )
        cycle = store.latest_semantic_discovery_cycle(run_id=run.run_id)

        assert cycle_id > 0
        assert cycle is not None
        assert cycle["reviewed_pair_count"] == 125
        assert cycle["metrics"]["retrieved_candidates"] == 125
        assert len(cycle["pairs"]) == 125
        assert len(store.recent_pair_reviews(limit=200)) == 125
    finally:
        store.close()


def test_cross_platform_evaluation_funnel_persists_compact_reason_counts(tmp_path):
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        run = store.start_run(
            starting_equity=1000.0,
            pnl_source="projected_locked_paper",
        )

        store.record_cross_platform_evaluation_counts(
            {
                "pair_due": 3,
                "paired_snapshot_fresh": 2,
                "stale_polymarket_orderbook": 1,
                "edge_below_threshold": 2,
            }
        )
        store.record_cross_platform_evaluation_counts(
            {"pair_due": 1, "paired_snapshot_fresh": 1}
        )

        assert store.cross_platform_evaluation_funnel(run.run_id) == {
            "pair_due": 4,
            "paired_snapshot_fresh": 3,
            "stale_polymarket_orderbook": 1,
            "edge_below_threshold": 2,
        }
    finally:
        store.close()


def test_cross_platform_evaluations_persist_exact_direction_evidence(tmp_path):
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        run = store.start_run(
            starting_equity=5000.0,
            pnl_source="projected_locked_paper",
        )
        common = {
            "pair_id": "poly:1|kalshi:KX-1",
            "polymarket_id": "condition-1",
            "kalshi_ticker": "KX-1",
            "polymarket_question": "Will Alice win?",
            "kalshi_title": "Alice wins?",
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
            "slippage_reserve": 0.02,
        }
        store.record_cross_platform_evaluations(
            [
                {
                    **common,
                    "token": "YES",
                    "buy_platform": "polymarket",
                    "sell_platform": "kalshi",
                    "buy_price": 0.50,
                    "sell_price": 0.51,
                    "buy_liquidity": 100.0,
                    "sell_liquidity": 90.0,
                    "gross_edge": 0.01,
                    "fee_cost": 0.006,
                    "net_edge": 0.004,
                    "executable_net_edge": -0.016,
                    "required_net_edge": 0.02,
                    "suggested_size": 10.0,
                    "outcome": "skipped",
                    "reason_code": "edge_below_threshold",
                },
                {
                    **common,
                    "token": "NO",
                    "buy_platform": "kalshi",
                    "sell_platform": "polymarket",
                    "buy_price": 0.49,
                    "sell_price": 0.49,
                    "buy_liquidity": 80.0,
                    "sell_liquidity": 70.0,
                    "gross_edge": 0.0,
                    "fee_cost": 0.006,
                    "net_edge": -0.006,
                    "executable_net_edge": -0.026,
                    "required_net_edge": 0.02,
                    "suggested_size": 10.0,
                    "outcome": "skipped",
                    "reason_code": "edge_below_threshold",
                },
            ]
        )

        rows = store.recent_cross_platform_evaluations(run_id=run.run_id, limit=10)

        assert len(rows) == 2
        assert {row["token"] for row in rows} == {"YES", "NO"}
        yes = next(row for row in rows if row["token"] == "YES")
        assert yes["gross_edge"] == pytest.approx(0.01)
        assert yes["fee_cost"] == pytest.approx(0.006)
        assert yes["executable_net_edge"] == pytest.approx(-0.016)
        assert yes["polymarket_yes_ask_size"] == pytest.approx(110.0)
        assert yes["kalshi_yes_bid_size"] == pytest.approx(80.0)
        assert yes["reason_code"] == "edge_below_threshold"
        assert store.cross_platform_evaluation_count(run.run_id) == 2
        near_misses = store.top_cross_platform_near_misses(
            run_id=run.run_id,
            limit=1,
        )
        assert near_misses[0]["token"] == "YES"
    finally:
        store.close()
