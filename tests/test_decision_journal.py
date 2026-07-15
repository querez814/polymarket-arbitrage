from datetime import datetime

import pytest

from core.decision_journal import DecisionJournal, DecisionOutcome


def test_decision_journal_retains_bounded_recent_records():
    journal = DecisionJournal(max_records=2)

    journal.add_decision(
        decision_id="one",
        strategy="bundle_arb",
        outcome=DecisionOutcome.SKIP,
        reason_code="edge_below_threshold",
        explanation="first",
    )
    journal.add_decision(
        decision_id="two",
        strategy="bundle_arb",
        outcome=DecisionOutcome.TRADE,
        reason_code="net_edge_above_threshold",
        explanation="second",
    )
    journal.add_decision(
        decision_id="three",
        strategy="execution",
        outcome=DecisionOutcome.WAIT,
        reason_code="signal_queued",
        explanation="third",
    )

    assert [record.decision_id for record in journal.recent()] == ["two", "three"]
    assert journal.summary()["total"] == 2
    assert journal.summary()["by_outcome"] == {"trade": 1, "wait": 1}


def test_decision_record_serialization_is_json_safe():
    journal = DecisionJournal()
    timestamp = datetime(2026, 6, 29, 12, 0, 0)

    journal.add_decision(
        decision_id="dec",
        strategy="cross_platform",
        outcome=DecisionOutcome.TRADE,
        reason_code="cross_platform_edge",
        explanation="found edge",
        evidence={"seen_at": timestamp, "outcome": DecisionOutcome.TRADE},
    )

    serialized = journal.to_dicts()[0]
    assert serialized["outcome"] == "trade"
    assert serialized["evidence"]["seen_at"] == "2026-06-29T12:00:00"
    assert serialized["evidence"]["outcome"] == "trade"


def test_decision_journal_requires_positive_bound():
    with pytest.raises(ValueError):
        DecisionJournal(max_records=0)
