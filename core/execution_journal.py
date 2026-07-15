"""Crash-safe event journal for two-leg execution state.

This module contains no exchange integration.  It durably records intent and
state transitions so a later restart reconciler can recover the exact stable
idempotency keys and determine which executions require authoritative reads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.two_leg_execution import (
    ExecutionPhase,
    LegIntent,
    LegPhase,
    LegSide,
    TwoLegExecution,
)

JOURNAL_SCHEMA_VERSION = "two-leg-execution-journal-v1"
MAX_EVENTS_PER_EXECUTION = 10_000
MAX_EVENT_BYTES = 64_000
_EMPTY_DIGEST = "0" * 64


class ExecutionJournal:
    """Append-only, integrity-checked SQLite journal owned by one process.

    SQLite serializes writers across processes.  Every public mutation obtains
    an immediate write transaction, replays and validates the existing chain,
    validates the requested transition in the domain model, and only then
    appends the event.  Callers must commit ``start_submission`` before making
    any future venue mutation.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_destination()
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5.0,
        )
        try:
            os.chmod(self.path, 0o600)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            journal_mode = self._connection.execute(
                "PRAGMA journal_mode = WAL"
            ).fetchone()
            if journal_mode is None or journal_mode[0] != "wal":
                raise ValueError("execution journal requires SQLite WAL mode")
            self._connection.execute("PRAGMA synchronous = FULL")
            synchronous = self._connection.execute("PRAGMA synchronous").fetchone()
            if synchronous is None or synchronous[0] != 2:
                raise ValueError("execution journal requires FULL synchronization")
            self._initialize_schema()
            integrity = self._connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ValueError("execution journal failed SQLite integrity check")
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> ExecutionJournal:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def create_execution(self, execution: TwoLegExecution) -> TwoLegExecution:
        """Persist a pristine immutable intent before either leg may submit."""
        if any(leg.phase is not LegPhase.PLANNED for leg in execution.legs.values()):
            raise ValueError("only a planned execution can be created")
        payload = {
            "event": "execution_created",
            "legs": [
                _intent_payload(execution.legs[leg_id].intent)
                for leg_id in sorted(execution.legs)
            ],
        }
        self._begin()
        try:
            existing = self._connection.execute(
                "SELECT 1 FROM execution_events WHERE execution_id = ? LIMIT 1",
                (execution.execution_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError("execution_id already exists in journal")
            self._append_event(execution.execution_id, 0, _EMPTY_DIGEST, payload)
            loaded = self._load_from_connection(execution.execution_id)
            self._connection.commit()
            return loaded
        except BaseException:
            self._connection.rollback()
            raise

    def start_submission(self, execution_id: str, leg_id: str) -> TwoLegExecution:
        """Durably mark a leg submitting before a caller may contact its venue."""
        return self._transition(
            execution_id,
            {"event": "submission_started", "leg_id": leg_id},
            lambda execution: execution.start_submission(leg_id),
        )

    def mark_submission_ambiguous(
        self, execution_id: str, leg_id: str
    ) -> TwoLegExecution:
        return self._transition(
            execution_id,
            {"event": "submission_ambiguous", "leg_id": leg_id},
            lambda execution: execution.mark_submission_ambiguous(leg_id),
        )

    def reconcile_leg(
        self,
        execution_id: str,
        leg_id: str,
        *,
        phase: LegPhase,
        cumulative_filled_size: float,
        venue_order_id: str | None = None,
    ) -> TwoLegExecution:
        payload = {
            "event": "leg_reconciled",
            "leg_id": leg_id,
            "phase": phase.value if isinstance(phase, LegPhase) else phase,
            "cumulative_filled_size": cumulative_filled_size,
            "venue_order_id": venue_order_id,
        }
        return self._transition(
            execution_id,
            payload,
            lambda execution: execution.reconcile_leg(
                leg_id,
                phase=phase,
                cumulative_filled_size=cumulative_filled_size,
                venue_order_id=venue_order_id,
            ),
        )

    def load_execution(self, execution_id: str) -> TwoLegExecution:
        """Replay one complete hash chain, rejecting any invalid event."""
        return self._load_from_connection(execution_id)

    def load_unfinished(self) -> tuple[TwoLegExecution, ...]:
        """Return all non-complete executions or fail on any corrupt chain."""
        executions = self.load_all()
        return tuple(
            execution
            for execution in executions
            if execution.phase is not ExecutionPhase.COMPLETE
        )

    def load_all(self) -> tuple[TwoLegExecution, ...]:
        """Return every execution after validating every complete hash chain."""
        rows = self._connection.execute(
            "SELECT DISTINCT execution_id FROM execution_events ORDER BY execution_id"
        ).fetchall()
        return tuple(
            self._load_from_connection(str(row["execution_id"])) for row in rows
        )

    def load_all_with_token(self) -> tuple[tuple[TwoLegExecution, ...], str]:
        """Load a consistent replay snapshot and its journal generation token."""
        self._connection.execute("BEGIN")
        try:
            executions = self.load_all()
            token = self.snapshot_token()
            self._connection.commit()
            return executions, token
        except BaseException:
            self._connection.rollback()
            raise

    def snapshot_token(self) -> str:
        """Return a digest that changes when any execution event is appended."""
        rows = self._connection.execute("""
            SELECT execution_id, sequence, event_sha256
            FROM execution_events
            ORDER BY execution_id, sequence
            """).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(str(row["execution_id"]).encode("utf-8"))
            digest.update(b"\x1f")
            digest.update(str(row["sequence"]).encode("ascii"))
            digest.update(b"\x1f")
            digest.update(str(row["event_sha256"]).encode("ascii"))
            digest.update(b"\x1e")
        return digest.hexdigest()

    def event_count(self, execution_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM execution_events WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return int(row["count"])

    def _validate_destination(self) -> None:
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("execution journal must be a regular file, not a symlink")

    def _initialize_schema(self) -> None:
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS journal_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS execution_events (
                execution_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                recorded_at TEXT NOT NULL,
                previous_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sha256 TEXT NOT NULL,
                PRIMARY KEY (execution_id, sequence)
            ) STRICT;
            CREATE TRIGGER IF NOT EXISTS execution_events_no_update
            BEFORE UPDATE ON execution_events BEGIN
                SELECT RAISE(ABORT, 'execution journal is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS execution_events_no_delete
            BEFORE DELETE ON execution_events BEGIN
                SELECT RAISE(ABORT, 'execution journal is append-only');
            END;
            """)
        row = self._connection.execute(
            "SELECT value FROM journal_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO journal_metadata (key, value) VALUES ('schema_version', ?)",
                (JOURNAL_SCHEMA_VERSION,),
            )
        elif row["value"] != JOURNAL_SCHEMA_VERSION:
            raise ValueError("execution journal schema version is unsupported")

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _transition(
        self,
        execution_id: str,
        payload: Mapping[str, Any],
        apply: Callable[[TwoLegExecution], None],
    ) -> TwoLegExecution:
        self._begin()
        try:
            execution, sequence, previous_digest = self._replay(execution_id)
            if sequence >= MAX_EVENTS_PER_EXECUTION:
                raise ValueError("execution journal event limit exceeded")
            apply(execution)
            self._append_event(execution_id, sequence, previous_digest, payload)
            verified = self._load_from_connection(execution_id)
            self._connection.commit()
            return verified
        except BaseException:
            self._connection.rollback()
            raise

    def _load_from_connection(self, execution_id: str) -> TwoLegExecution:
        execution, _sequence, _digest = self._replay(execution_id)
        return execution

    def _replay(self, execution_id: str) -> tuple[TwoLegExecution, int, str]:
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution_id must be a non-empty string")
        rows = self._connection.execute(
            """
            SELECT sequence, recorded_at, previous_sha256, payload_json, event_sha256
            FROM execution_events
            WHERE execution_id = ?
            ORDER BY sequence
            LIMIT ?
            """,
            (execution_id, MAX_EVENTS_PER_EXECUTION + 1),
        ).fetchall()
        if not rows:
            raise ValueError("execution_id is not present in journal")
        if len(rows) > MAX_EVENTS_PER_EXECUTION:
            raise ValueError("execution journal event limit exceeded")

        execution: TwoLegExecution | None = None
        previous_digest = _EMPTY_DIGEST
        for expected_sequence, row in enumerate(rows):
            if row["sequence"] != expected_sequence:
                raise ValueError("execution journal sequence is not contiguous")
            if not hmac.compare_digest(str(row["previous_sha256"]), previous_digest):
                raise ValueError("execution journal hash chain is broken")
            recorded_at = str(row["recorded_at"])
            _parse_recorded_at(recorded_at)
            payload_json = str(row["payload_json"])
            if len(payload_json.encode("utf-8")) > MAX_EVENT_BYTES:
                raise ValueError("execution journal event exceeds maximum size")
            payload = _parse_payload(payload_json)
            digest = _event_digest(
                execution_id,
                expected_sequence,
                recorded_at,
                previous_digest,
                payload,
            )
            stored_digest = str(row["event_sha256"])
            if not hmac.compare_digest(stored_digest, digest):
                raise ValueError("execution journal event checksum mismatch")
            execution = _apply_event(execution_id, execution, payload)
            previous_digest = digest
        assert execution is not None
        return execution, len(rows), previous_digest

    def _append_event(
        self,
        execution_id: str,
        sequence: int,
        previous_digest: str,
        payload: Mapping[str, Any],
    ) -> None:
        payload_json = _canonical_json(payload)
        if len(payload_json.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("execution journal event exceeds maximum size")
        recorded_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        digest = _event_digest(
            execution_id,
            sequence,
            recorded_at,
            previous_digest,
            payload,
        )
        self._connection.execute(
            """
            INSERT INTO execution_events (
                execution_id, sequence, recorded_at, previous_sha256,
                payload_json, event_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                sequence,
                recorded_at,
                previous_digest,
                payload_json,
                digest,
            ),
        )


def _intent_payload(intent: LegIntent) -> dict[str, Any]:
    return {
        "leg_id": intent.leg_id,
        "venue": intent.venue,
        "market_id": intent.market_id,
        "side": intent.side.value,
        "limit_price": intent.limit_price,
        "size": intent.size,
    }


def _apply_event(
    execution_id: str,
    execution: TwoLegExecution | None,
    payload: Mapping[str, Any],
) -> TwoLegExecution:
    event = payload.get("event")
    if event == "execution_created":
        _require_exact_keys(payload, {"event", "legs"})
        if execution is not None:
            raise ValueError("execution_created must be the first journal event")
        legs = payload["legs"]
        if not isinstance(legs, list) or len(legs) != 2:
            raise ValueError("execution_created requires exactly two legs")
        first, second = (_intent_from_payload(item) for item in legs)
        return TwoLegExecution(execution_id, first, second)
    if execution is None:
        raise ValueError("first journal event must create the execution")
    if event == "submission_started":
        _require_exact_keys(payload, {"event", "leg_id"})
        execution.start_submission(_require_string(payload["leg_id"], "leg_id"))
    elif event == "submission_ambiguous":
        _require_exact_keys(payload, {"event", "leg_id"})
        execution.mark_submission_ambiguous(
            _require_string(payload["leg_id"], "leg_id")
        )
    elif event == "leg_reconciled":
        _require_exact_keys(
            payload,
            {
                "event",
                "leg_id",
                "phase",
                "cumulative_filled_size",
                "venue_order_id",
            },
        )
        try:
            phase = LegPhase(_require_string(payload["phase"], "phase"))
        except ValueError as exc:
            raise ValueError("journal reconciliation phase is invalid") from exc
        order_id = payload["venue_order_id"]
        if order_id is not None:
            order_id = _require_string(order_id, "venue_order_id")
        execution.reconcile_leg(
            _require_string(payload["leg_id"], "leg_id"),
            phase=phase,
            cumulative_filled_size=_require_number(
                payload["cumulative_filled_size"], "cumulative_filled_size"
            ),
            venue_order_id=order_id,
        )
    else:
        raise ValueError("execution journal event type is unsupported")
    return execution


def _intent_from_payload(value: Any) -> LegIntent:
    payload = _require_mapping(value, "leg intent")
    _require_exact_keys(
        payload,
        {"leg_id", "venue", "market_id", "side", "limit_price", "size"},
    )
    try:
        side = LegSide(_require_string(payload["side"], "side"))
    except ValueError as exc:
        raise ValueError("journal leg side is invalid") from exc
    return LegIntent(
        leg_id=_require_string(payload["leg_id"], "leg_id"),
        venue=_require_string(payload["venue"], "venue"),
        market_id=_require_string(payload["market_id"], "market_id"),
        side=side,
        limit_price=_require_number(payload["limit_price"], "limit_price"),
        size=_require_number(payload["size"], "size"),
    )


def _parse_payload(payload_json: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            payload_json,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {constant}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ValueError("execution journal payload is not valid JSON") from exc
    payload = _require_mapping(value, "journal event")
    if _canonical_json(payload) != payload_json:
        raise ValueError("execution journal payload is not canonical")
    return payload


def _event_digest(
    execution_id: str,
    sequence: int,
    recorded_at: str,
    previous_digest: str,
    payload: Mapping[str, Any],
) -> str:
    envelope = {
        "execution_id": execution_id,
        "journal_schema_version": JOURNAL_SCHEMA_VERSION,
        "payload": payload,
        "previous_sha256": previous_digest,
        "recorded_at": recorded_at,
        "sequence": sequence,
    }
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("execution journal event is not canonical JSON") from exc


def _parse_recorded_at(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("execution journal timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("execution journal timestamp must be UTC")


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("execution journal event fields do not match schema")


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result
