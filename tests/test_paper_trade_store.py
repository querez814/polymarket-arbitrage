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
