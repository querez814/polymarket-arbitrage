from datetime import datetime, timedelta, timezone

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
