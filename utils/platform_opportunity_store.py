"""Durable research ledger for platform-first opportunity discovery.

This database is deliberately separate from the locked-arbitrage ledger.  It
contains catalog history and shadow-only strategy evidence; nothing in this
module can submit an exchange order.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)  # type: ignore[arg-type]
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class PlatformOpportunityStore:
    """Thread-safe SQLite store for catalog revisions and shadow evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS platform_contract_current (
                    contract_id TEXT PRIMARY KEY,
                    venue TEXT NOT NULL,
                    native_id TEXT NOT NULL,
                    revision_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_contract_native
                    ON platform_contract_current(venue, native_id);

                CREATE TABLE IF NOT EXISTS platform_contract_revisions (
                    contract_id TEXT NOT NULL,
                    revision_hash TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(contract_id, revision_hash)
                );

                CREATE TABLE IF NOT EXISTS political_event_locks (
                    event_id TEXT PRIMARY KEY,
                    event_title TEXT NOT NULL,
                    occurrence_at TEXT NOT NULL,
                    locked_until TEXT NOT NULL,
                    selected_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_political_event_locks_until
                    ON political_event_locks(locked_until);

                CREATE TABLE IF NOT EXISTS structural_relations (
                    relation_id TEXT PRIMARY KEY,
                    relation_type TEXT NOT NULL,
                    invariant TEXT NOT NULL,
                    residual_basis_risk TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shadow_intents (
                    intent_id TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    event_cluster_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    relation_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_intents_due
                    ON shadow_intents(contract_id, expires_at);

                CREATE TABLE IF NOT EXISTS shadow_marks (
                    intent_id TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    capacity_fraction REAL NOT NULL,
                    max_notional REAL NOT NULL,
                    net_return REAL,
                    capacity_pnl REAL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(intent_id, horizon_seconds, capacity_fraction)
                );
                """)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def upsert_contracts(
        self,
        contracts: Iterable[Any],
        *,
        observed_at: datetime,
        retire_absent: bool = True,
    ) -> int:
        """Upsert current rows and append only materially new revisions."""
        timestamp = _utc_iso(observed_at)
        revisions = 0
        contract_rows = list(contracts)
        current_ids = {contract.contract_id for contract in contract_rows}
        with self._lock, self._connection:
            for contract in contract_rows:
                payload = _json(contract)
                existing = self._connection.execute(
                    "SELECT revision_hash, first_seen_at FROM platform_contract_current "
                    "WHERE contract_id = ?",
                    (contract.contract_id,),
                ).fetchone()
                first_seen = existing["first_seen_at"] if existing else timestamp
                if not existing or existing["revision_hash"] != contract.revision_hash:
                    inserted = self._connection.execute(
                        "INSERT OR IGNORE INTO platform_contract_revisions "
                        "(contract_id, revision_hash, observed_at, payload_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            contract.contract_id,
                            contract.revision_hash,
                            timestamp,
                            payload,
                        ),
                    ).rowcount
                    revisions += int(inserted > 0)
                self._connection.execute(
                    "INSERT INTO platform_contract_current "
                    "(contract_id, venue, native_id, revision_hash, payload_json, "
                    "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(contract_id) DO UPDATE SET "
                    "revision_hash=excluded.revision_hash, "
                    "payload_json=excluded.payload_json, "
                    "last_seen_at=excluded.last_seen_at",
                    (
                        contract.contract_id,
                        contract.venue,
                        contract.native_id,
                        contract.revision_hash,
                        payload,
                        first_seen,
                        timestamp,
                    ),
                )
            if retire_absent:
                existing_ids = {
                    str(row[0])
                    for row in self._connection.execute(
                        "SELECT contract_id FROM platform_contract_current"
                    ).fetchall()
                }
                self._connection.executemany(
                    "DELETE FROM platform_contract_current WHERE contract_id = ?",
                    ((contract_id,) for contract_id in existing_ids - current_ids),
                )
        return revisions

    def catalog_counts(self) -> dict[str, int]:
        with self._lock:
            current = self._connection.execute(
                "SELECT COUNT(*) FROM platform_contract_current"
            ).fetchone()[0]
            revisions = self._connection.execute(
                "SELECT COUNT(*) FROM platform_contract_revisions"
            ).fetchone()[0]
        return {"current": int(current), "revisions": int(revisions)}

    def latest_contract_payloads(
        self, contract_ids: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        """Return the newest immutable revision for explicitly retained contracts.

        A truncated catalog response is never evidence that an ordinary
        contract remains eligible.  Political locks are the narrow exception:
        their selected contracts must survive a process restart through the
        lock window, even when the latest bounded page did not include them.
        Read those contracts from revision history rather than keeping an
        ever-growing ``current`` catalog alive.
        """
        ids = tuple(dict.fromkeys(str(contract_id) for contract_id in contract_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.contract_id, r.payload_json "
                "FROM platform_contract_revisions r "
                "JOIN ("
                "  SELECT contract_id, MAX(observed_at) AS observed_at "
                "  FROM platform_contract_revisions "
                f"  WHERE contract_id IN ({placeholders}) "
                "  GROUP BY contract_id"
                ") latest ON latest.contract_id = r.contract_id "
                "AND latest.observed_at = r.observed_at",
                ids,
            ).fetchall()
        return {str(row["contract_id"]): json.loads(row["payload_json"]) for row in rows}

    def upsert_political_event_lock(self, lock: Any) -> None:
        """Persist a selected event through its occurrence/cooldown window."""
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO political_event_locks "
                "(event_id, event_title, occurrence_at, locked_until, selected_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET "
                "event_title=excluded.event_title, occurrence_at=excluded.occurrence_at, "
                "locked_until=excluded.locked_until, payload_json=excluded.payload_json",
                (
                    lock.event_id,
                    lock.event_title,
                    _utc_iso(lock.occurrence_at),
                    _utc_iso(lock.locked_until),
                    _utc_iso(lock.selected_at),
                    _json(lock),
                ),
            )

    def active_political_event_locks(self, *, now: datetime) -> list[dict[str, Any]]:
        """Return only locks that still own their full observation window."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM political_event_locks WHERE locked_until >= ? "
                "ORDER BY occurrence_at, event_id",
                (_utc_iso(now),),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def record_relations(
        self,
        relations: Iterable[Any],
        *,
        observed_at: datetime,
        retire_absent: bool = True,
    ) -> int:
        timestamp = _utc_iso(observed_at)
        written = 0
        relation_rows = list(relations)
        current_ids = {relation.relation_id for relation in relation_rows}
        with self._lock, self._connection:
            for relation in relation_rows:
                written += int(
                    self._connection.execute(
                        "INSERT INTO structural_relations "
                        "(relation_id, relation_type, invariant, residual_basis_risk, "
                        "payload_json, first_seen_at, last_seen_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(relation_id) DO UPDATE SET "
                        "payload_json=excluded.payload_json, "
                        "residual_basis_risk=excluded.residual_basis_risk, "
                        "last_seen_at=excluded.last_seen_at",
                        (
                            relation.relation_id,
                            relation.relation_type,
                            relation.invariant,
                            relation.residual_basis_risk,
                            _json(relation),
                            timestamp,
                            timestamp,
                        ),
                    ).rowcount
                    > 0
                )
            if retire_absent:
                existing_ids = {
                    str(row[0])
                    for row in self._connection.execute(
                        "SELECT relation_id FROM structural_relations"
                    ).fetchall()
                }
                self._connection.executemany(
                    "DELETE FROM structural_relations WHERE relation_id = ?",
                    ((relation_id,) for relation_id in existing_ids - current_ids),
                )
        return written

    def record_intent(self, intent: Any) -> bool:
        with self._lock, self._connection:
            return bool(
                self._connection.execute(
                    "INSERT OR IGNORE INTO shadow_intents "
                    "(intent_id, lane, event_cluster_id, contract_id, relation_id, "
                    "created_at, expires_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        intent.intent_id,
                        intent.lane,
                        intent.event_cluster_id,
                        intent.contract_id,
                        intent.relation_id,
                        _utc_iso(intent.created_at),
                        _utc_iso(intent.expires_at),
                        _json(intent),
                    ),
                ).rowcount
            )

    def record_marks(self, marks: Iterable[Any]) -> int:
        written = 0
        with self._lock, self._connection:
            for mark in marks:
                written += int(
                    self._connection.execute(
                        "INSERT OR IGNORE INTO shadow_marks "
                        "(intent_id, horizon_seconds, observed_at, capacity_fraction, "
                        "max_notional, net_return, capacity_pnl, reason, payload_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            mark.intent_id,
                            mark.horizon_seconds,
                            _utc_iso(mark.observed_at),
                            mark.capacity_fraction,
                            mark.max_notional,
                            mark.net_return,
                            mark.capacity_pnl,
                            mark.reason,
                            _json(mark),
                        ),
                    ).rowcount
                    > 0
                )
        return written

    def intent_rows(
        self, *, lane: str | None = None, cohort_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM shadow_intents"
        params: tuple[Any, ...] = ()
        if lane is not None:
            query += " WHERE lane = ?"
            params = (lane,)
        query += " ORDER BY created_at"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        payloads = [json.loads(row[0]) for row in rows]
        if cohort_id is not None:
            payloads = [
                payload for payload in payloads if payload.get("cohort_id") == cohort_id
            ]
        return payloads

    def mark_rows(
        self,
        *,
        lane: str | None = None,
        horizon_seconds: int = 600,
        cohort_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT m.payload_json, i.payload_json AS intent_payload_json, "
            "i.event_cluster_id, i.lane FROM shadow_marks m "
            "JOIN shadow_intents i ON i.intent_id = m.intent_id "
            "WHERE m.horizon_seconds = ? AND m.capacity_fraction = 0.1"
        )
        params: list[Any] = [horizon_seconds]
        if lane is not None:
            query += " AND i.lane = ?"
            params.append(lane)
        query += " ORDER BY m.observed_at"
        with self._lock:
            rows = self._connection.execute(query, tuple(params)).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["event_cluster_id"] = row["event_cluster_id"]
            payload["lane"] = row["lane"]
            intent_payload = json.loads(row["intent_payload_json"])
            if cohort_id is not None and intent_payload.get("cohort_id") != cohort_id:
                continue
            payload["market_family"] = intent_payload.get("market_family")
            payload["created_at"] = intent_payload.get("created_at")
            result.append(payload)
        return result

    def mark_keys(self, *, cohort_id: str) -> set[tuple[str, int, float]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT m.intent_id, m.horizon_seconds, m.capacity_fraction, "
                "i.payload_json FROM shadow_marks m "
                "JOIN shadow_intents i ON i.intent_id = m.intent_id"
            ).fetchall()
        return {
            (
                str(row["intent_id"]),
                int(row["horizon_seconds"]),
                float(row["capacity_fraction"]),
            )
            for row in rows
            if json.loads(row["payload_json"]).get("cohort_id") == cohort_id
        }

    def summary(self, *, cohort_id: str | None = None) -> dict[str, Any]:
        """Summarize catalog evidence and shadow outcomes for one cohort.

        Catalog and structural-relation rows describe the shared bounded
        inventory.  Intents and marks are experiment evidence, so they must
        be scoped through their intent's immutable cohort payload.
        """
        with self._lock:
            relations = self._connection.execute(
                "SELECT COUNT(*) FROM structural_relations"
            ).fetchone()[0]
            intent_query = "SELECT lane, COUNT(*) AS count FROM shadow_intents"
            intent_params: tuple[Any, ...] = ()
            if cohort_id is not None:
                intent_query += " WHERE json_extract(payload_json, '$.cohort_id') = ?"
                intent_params = (cohort_id,)
            intents = self._connection.execute(
                intent_query + " GROUP BY lane", intent_params
            ).fetchall()
            mark_query = "SELECT COUNT(*) FROM shadow_marks m"
            mark_params: tuple[Any, ...] = ()
            if cohort_id is not None:
                mark_query += (
                    " JOIN shadow_intents i ON i.intent_id = m.intent_id"
                    " WHERE json_extract(i.payload_json, '$.cohort_id') = ?"
                )
                mark_params = (cohort_id,)
            marks = self._connection.execute(mark_query, mark_params).fetchone()[0]
        return {
            **self.catalog_counts(),
            "relations": int(relations),
            "intents": {row["lane"]: int(row["count"]) for row in intents},
            "marks": int(marks),
        }

    def research_mark_summary(
        self, *, cohort_id: str | None = None
    ) -> dict[str, Any]:
        """Return research-only marks split by actual exit and fixed horizon."""
        query = (
            "SELECT m.horizon_seconds, COUNT(*) AS marks, "
            "COUNT(m.capacity_pnl) AS scored_marks, "
            "COALESCE(SUM(m.capacity_pnl), 0.0) AS capacity_pnl "
            "FROM shadow_marks m"
        )
        params: tuple[Any, ...] = ()
        if cohort_id is not None:
            query += (
                " JOIN shadow_intents i ON i.intent_id = m.intent_id"
                " WHERE json_extract(i.payload_json, '$.cohort_id') = ?"
            )
            params = (cohort_id,)
        else:
            query += " WHERE 1 = 1"
        query += " AND m.capacity_fraction = 0.1 GROUP BY m.horizon_seconds ORDER BY m.horizon_seconds"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        by_horizon = {
            str(int(row["horizon_seconds"])): {
                "marks": int(row["marks"]),
                "scored_marks": int(row["scored_marks"]),
                "capacity_pnl": float(row["capacity_pnl"]),
            }
            for row in rows
        }
        return {
            "authority": "shadow_research_only",
            "actual_exit": by_horizon.pop(
                "-1", {"marks": 0, "scored_marks": 0, "capacity_pnl": 0.0}
            ),
            "horizons": by_horizon,
        }
