"""
Decision Journal
================

Structured, bounded in-memory journal for trading decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from utils.time_utils import to_utc_iso, utc_now


class DecisionOutcome(Enum):
    """High-level result of a trading decision."""

    TRADE = "trade"
    SKIP = "skip"
    REJECT = "reject"
    WAIT = "wait"
    ERROR = "error"


@dataclass
class DecisionRecord:
    """Human-readable evidence and rationale for one bot decision."""

    decision_id: str
    strategy: str
    outcome: DecisionOutcome | str
    reason_code: str
    explanation: str
    market_id: str = ""
    platform: str = ""
    market_question: str = ""
    category: str = ""
    timestamp: datetime = field(default_factory=utc_now)
    evidence: dict[str, Any] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    related_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-safe dictionary for the dashboard."""
        outcome = self.outcome.value if isinstance(self.outcome, DecisionOutcome) else str(self.outcome)
        return {
            "decision_id": self.decision_id,
            "strategy": self.strategy,
            "outcome": outcome,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "market_id": self.market_id,
            "platform": self.platform,
            "market_question": self.market_question,
            "category": self.category,
            "timestamp": to_utc_iso(self.timestamp),
            "evidence": _json_safe(self.evidence),
            "orders": _json_safe(self.orders),
            "related_id": self.related_id,
        }


class DecisionJournal:
    """Bounded append-only decision log."""

    def __init__(self, max_records: int = 500):
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.max_records = max_records
        self._records: list[DecisionRecord] = []

    def add(self, record: DecisionRecord) -> DecisionRecord:
        """Append a record and enforce bounded retention."""
        self._records.append(record)
        if len(self._records) > self.max_records:
            self._records = self._records[-self.max_records:]
        return record

    def add_decision(
        self,
        *,
        decision_id: str,
        strategy: str,
        outcome: DecisionOutcome | str,
        reason_code: str,
        explanation: str,
        market_id: str = "",
        platform: str = "",
        market_question: str = "",
        category: str = "",
        evidence: Optional[dict[str, Any]] = None,
        orders: Optional[list[dict[str, Any]]] = None,
        related_id: Optional[str] = None,
    ) -> DecisionRecord:
        """Build and append a decision record."""
        return self.add(DecisionRecord(
            decision_id=decision_id,
            strategy=strategy,
            outcome=outcome,
            reason_code=reason_code,
            explanation=explanation,
            market_id=market_id,
            platform=platform,
            market_question=market_question,
            category=category,
            evidence=evidence or {},
            orders=orders or [],
            related_id=related_id,
        ))

    def recent(self, limit: Optional[int] = None) -> list[DecisionRecord]:
        """Return recent records in chronological order."""
        if limit is None:
            return list(self._records)
        return self._records[-limit:]

    def to_dicts(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """Return recent records as JSON-safe dictionaries."""
        return [record.to_dict() for record in self.recent(limit)]

    def summary(self) -> dict[str, Any]:
        """Return counts by outcome and reason for dashboard metrics."""
        by_outcome: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        for record in self._records:
            outcome = record.outcome.value if isinstance(record.outcome, DecisionOutcome) else str(record.outcome)
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            by_reason[record.reason_code] = by_reason.get(record.reason_code, 0) + 1
            by_strategy[record.strategy] = by_strategy.get(record.strategy, 0) + 1

        return {
            "total": len(self._records),
            "by_outcome": by_outcome,
            "by_reason": by_reason,
            "by_strategy": by_strategy,
        }


def _json_safe(value: Any) -> Any:
    """Convert common Python objects into JSON-safe values."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value
