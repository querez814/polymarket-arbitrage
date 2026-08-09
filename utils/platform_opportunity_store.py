"""Durable research ledger for platform-first opportunity discovery.

This database is deliberately separate from the locked-arbitrage ledger.  It
contains catalog history and shadow-only strategy evidence; nothing in this
module can submit an exchange order.
"""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import threading
import zlib
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class ReplayEvidenceCapacityError(RuntimeError):
    """A cohort cannot remain replay-valid after its durable evidence cap is hit."""


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)  # type: ignore[arg-type]
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: (
            item.isoformat() if isinstance(item, datetime) else str(item)
        ),
    )


class PlatformOpportunityStore:
    """Thread-safe SQLite store for catalog revisions and shadow evidence."""

    def __init__(self, path: str | Path, *, replay_byte_cap: int = 4 * 1024**3):
        if replay_byte_cap <= 0:
            raise ValueError("replay_byte_cap must be positive")
        self.path = Path(path)
        self.replay_byte_cap = int(replay_byte_cap)
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
                CREATE TABLE IF NOT EXISTS platform_observations (
                    cohort_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    observation_count INTEGER NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    last_request_started_at TEXT,
                    last_received_at TEXT,
                    PRIMARY KEY(cohort_id, contract_id)
                );
                CREATE INDEX IF NOT EXISTS idx_platform_observations_cohort
                    ON platform_observations(cohort_id);
                CREATE TABLE IF NOT EXISTS platform_observation_failures (
                    cohort_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    failure_count INTEGER NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    PRIMARY KEY(cohort_id, contract_id, reason_code)
                );
                CREATE INDEX IF NOT EXISTS idx_platform_observation_failures_cohort
                    ON platform_observation_failures(cohort_id);

                -- These payloads are deliberately normalized representations,
                -- not venue responses.  Hash-addressed rows let many durable
                -- observations refer to one replayable state/fee definition.
                CREATE TABLE IF NOT EXISTS normalized_book_states (
                    state_hash TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    compressed_payload BLOB NOT NULL,
                    captured_bytes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS normalized_fee_schedules (
                    fee_hash TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    compressed_payload BLOB NOT NULL,
                    captured_bytes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_replay_observation_events (
                    cohort_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    contract_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('change', 'heartbeat')),
                    lock_phase TEXT NOT NULL,
                    state_hash TEXT NOT NULL REFERENCES normalized_book_states(state_hash),
                    fee_hash TEXT NOT NULL REFERENCES normalized_fee_schedules(fee_hash),
                    request_started_at TEXT,
                    received_at TEXT NOT NULL,
                    venue_timestamp TEXT,
                    timestamp_provenance TEXT NOT NULL,
                    PRIMARY KEY(cohort_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_platform_replay_events_contract
                    ON platform_replay_observation_events(cohort_id, contract_id, sequence);
                CREATE TABLE IF NOT EXISTS platform_replay_evidence_status (
                    cohort_id TEXT PRIMARY KEY,
                    cohort_valid INTEGER NOT NULL CHECK(cohort_valid IN (0, 1)),
                    degraded_reason TEXT
                );
                """)
            # Existing research ledgers remain readable while timing evidence is
            # introduced. SQLite has no ADD COLUMN IF NOT EXISTS support.
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(platform_observations)"
                )
            }
            if "last_request_started_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE platform_observations "
                    "ADD COLUMN last_request_started_at TEXT"
                )
            if "last_received_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE platform_observations ADD COLUMN last_received_at TEXT"
                )
            if "first_observed_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE platform_observations "
                    "ADD COLUMN first_observed_at TEXT"
                )
                self._connection.execute(
                    "UPDATE platform_observations SET first_observed_at = last_observed_at "
                    "WHERE first_observed_at IS NULL"
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _canonical_replay_payload(
        payload: dict[str, Any], *, kind: str
    ) -> tuple[str, bytes]:
        """Validate and compact a bounded, normalized replay payload.

        This boundary intentionally accepts only the unified book shape.  It
        avoids a future accidental path which stores raw adapter JSON and
        silently calls its timestamps or level ordering canonical evidence.
        """
        if not isinstance(payload.get("schema_version"), int):
            raise ValueError(f"{kind} schema_version is required")
        if kind == "book":
            allowed = {"schema_version", "yes", "no"}
            if set(payload) != allowed:
                raise ValueError("normalized book has unsupported fields")
            for token in ("yes", "no"):
                token_payload = payload.get(token)
                if not isinstance(token_payload, dict) or set(token_payload) != {
                    "bids",
                    "asks",
                }:
                    raise ValueError("normalized book requires yes/no bids and asks")
                for side, descending in (("bids", True), ("asks", False)):
                    levels = token_payload[side]
                    if not isinstance(levels, list) or len(levels) > 50:
                        raise ValueError("normalized book level count is invalid")
                    prior: float | None = None
                    for level in levels:
                        if (
                            not isinstance(level, list)
                            or len(level) != 2
                            or not all(isinstance(item, (int, float)) for item in level)
                        ):
                            raise ValueError("normalized book levels must be numeric pairs")
                        price, size = float(level[0]), float(level[1])
                        if not all(math.isfinite(item) and item > 0 for item in (price, size)):
                            raise ValueError("normalized book levels must be finite positive")
                        if prior is not None and (
                            price > prior if descending else price < prior
                        ):
                            raise ValueError("normalized book levels are not best-to-worst")
                        prior = price
        encoded = _json(payload).encode("utf-8")
        compressed = zlib.compress(encoded, level=9)
        if len(compressed) > 16 * 1024:
            raise ValueError(f"{kind} normalized payload exceeds 16 KiB cap")
        return hashlib.sha256(encoded).hexdigest(), compressed

    def _record_replay_payload(
        self, *, table: str, hash_column: str, payload: dict[str, Any], kind: str
    ) -> str:
        payload_hash, compressed = self._canonical_replay_payload(payload, kind=kind)
        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT OR IGNORE INTO {table} "
                f"({hash_column}, schema_version, compressed_payload, captured_bytes) "
                "VALUES (?, ?, ?, ?)",
                (payload_hash, int(payload["schema_version"]), compressed, len(compressed)),
            )
        return payload_hash

    def record_normalized_book_state(self, *, normalized_book: dict[str, Any]) -> str:
        """Deduplicate one canonical unified-model book state for replay."""
        return self._record_replay_payload(
            table="normalized_book_states",
            hash_column="state_hash",
            payload=normalized_book,
            kind="book",
        )

    def record_fee_schedule_payload(self, *, fee_schedule: dict[str, Any]) -> str:
        """Deduplicate a canonical fee schedule referenced by replay events."""
        return self._record_replay_payload(
            table="normalized_fee_schedules",
            hash_column="fee_hash",
            payload=fee_schedule,
            kind="fee schedule",
        )

    def _replay_payload(self, *, table: str, hash_column: str, payload_hash: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                f"SELECT compressed_payload FROM {table} WHERE {hash_column} = ?",
                (payload_hash,),
            ).fetchone()
        if row is None:
            raise KeyError(payload_hash)
        return json.loads(zlib.decompress(bytes(row["compressed_payload"])).decode("utf-8"))

    def replay_book_state(self, state_hash: str) -> dict[str, Any]:
        return self._replay_payload(
            table="normalized_book_states", hash_column="state_hash", payload_hash=state_hash
        )

    def replay_fee_schedule(self, fee_hash: str) -> dict[str, Any]:
        return self._replay_payload(
            table="normalized_fee_schedules", hash_column="fee_hash", payload_hash=fee_hash
        )

    def replay_evidence_counts(self) -> dict[str, int]:
        with self._lock:
            books = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(captured_bytes), 0) FROM normalized_book_states"
            ).fetchone()
            fees = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(captured_bytes), 0) FROM normalized_fee_schedules"
            ).fetchone()
        return {
            "book_states": int(books[0]),
            "fee_schedules": int(fees[0]),
            "captured_bytes": int(books[1]) + int(fees[1]),
            "store_bytes": self._sqlite_store_bytes(),
            "byte_cap": self.replay_byte_cap,
        }

    def _sqlite_store_bytes(self) -> int:
        """Return a conservative physical footprint for the SQLite evidence store.

        The main database can have allocated pages that are larger than its
        logical payloads, and WAL mode keeps recently committed pages beside
        it.  Both consume the configured replay-storage budget.
        """
        page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        wal_path = Path(f"{self.path}-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        return page_count * page_size + wal_bytes

    def replay_evidence_status(self, *, cohort_id: str) -> dict[str, Any]:
        """Return the fail-closed validity of one cohort's replay evidence."""
        with self._lock:
            row = self._connection.execute(
                "SELECT cohort_valid, degraded_reason "
                "FROM platform_replay_evidence_status WHERE cohort_id = ?",
                (cohort_id,),
            ).fetchone()
        if row is None:
            return {"cohort_valid": True, "degraded_reason": None}
        return {
            "cohort_valid": bool(row["cohort_valid"]),
            "degraded_reason": row["degraded_reason"],
        }

    def record_replay_observation(
        self,
        *,
        cohort_id: str,
        contract_id: str,
        normalized_book: dict[str, Any],
        fee_schedule: dict[str, Any],
        lock_phase: str,
        observed_at: datetime,
        request_started_at: datetime | None,
        received_at: datetime | None,
    ) -> dict[str, Any]:
        """Atomically retain canonical evidence before a book is scored.

        Adapter timestamps are deliberately not copied into ``venue_timestamp``:
        public adapters currently provide no verified venue-origin timestamp.
        Unchanged state is still durable by hash, while its event is suppressed
        until the phase-specific heartbeat interval has elapsed.
        """
        if not cohort_id or not contract_id or not lock_phase:
            raise ValueError("replay observation identity and phase are required")
        state_hash, compressed_book = self._canonical_replay_payload(
            normalized_book, kind="book"
        )
        fee_hash, compressed_fee = self._canonical_replay_payload(
            fee_schedule, kind="fee schedule"
        )
        receipt = received_at or observed_at
        receipt_iso = _utc_iso(receipt)
        request_iso = _utc_iso(request_started_at) if request_started_at else None
        heartbeat_seconds = 30 if lock_phase in {"hot", "event_live"} else 300
        with self._lock, self._connection:
            connection = self._connection
            status = connection.execute(
                "SELECT cohort_valid, degraded_reason "
                "FROM platform_replay_evidence_status WHERE cohort_id = ?",
                (cohort_id,),
            ).fetchone()
            if status is not None and not bool(status["cohort_valid"]):
                raise ReplayEvidenceCapacityError(
                    "replay evidence cohort is already invalid: "
                    f"{status['degraded_reason'] or 'unknown'}"
                )
            existing_book = connection.execute(
                "SELECT captured_bytes FROM normalized_book_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            existing_fee = connection.execute(
                "SELECT captured_bytes FROM normalized_fee_schedules WHERE fee_hash = ?",
                (fee_hash,),
            ).fetchone()
            added_bytes = (
                (0 if existing_book is not None else len(compressed_book))
                + (0 if existing_fee is not None else len(compressed_fee))
            )
            # Account for the actual SQLite allocation and WAL, not just
            # compressed payload bytes. Two pages conservatively cover a new
            # event plus any payload/index page split before the next WAL
            # checkpoint, so we fail closed before exceeding the quota.
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            projected_store_bytes = (
                self._sqlite_store_bytes() + added_bytes + 2 * page_size
            )
            if projected_store_bytes > self.replay_byte_cap:
                connection.execute(
                    "INSERT INTO platform_replay_evidence_status "
                    "(cohort_id, cohort_valid, degraded_reason) VALUES (?, 0, ?) "
                    "ON CONFLICT(cohort_id) DO UPDATE SET cohort_valid = 0, "
                    "degraded_reason = excluded.degraded_reason",
                    (cohort_id, "replay_evidence_byte_cap_exceeded"),
                )
                # Commit the fail-closed status before raising so restart and
                # dashboard consumers cannot mistake this cohort for valid.
                connection.commit()
                raise ReplayEvidenceCapacityError(
                    "replay evidence byte cap exceeded before observation persistence"
                )
            connection.execute(
                "INSERT OR IGNORE INTO normalized_book_states "
                "(state_hash, schema_version, compressed_payload, captured_bytes) "
                "VALUES (?, ?, ?, ?)",
                (
                    state_hash,
                    int(normalized_book["schema_version"]),
                    compressed_book,
                    len(compressed_book),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO normalized_fee_schedules "
                "(fee_hash, schema_version, compressed_payload, captured_bytes) "
                "VALUES (?, ?, ?, ?)",
                (
                    fee_hash,
                    int(fee_schedule["schema_version"]),
                    compressed_fee,
                    len(compressed_fee),
                ),
            )
            prior = connection.execute(
                "SELECT state_hash, received_at FROM platform_replay_observation_events "
                "WHERE cohort_id = ? AND contract_id = ? ORDER BY sequence DESC LIMIT 1",
                (cohort_id, contract_id),
            ).fetchone()
            emit = prior is None or str(prior["state_hash"]) != state_hash
            if not emit:
                prior_at = datetime.fromisoformat(str(prior["received_at"]))
                emit = (receipt - prior_at).total_seconds() >= heartbeat_seconds
            event: dict[str, Any] | None = None
            if emit:
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 "
                        "FROM platform_replay_observation_events WHERE cohort_id = ?",
                        (cohort_id,),
                    ).fetchone()[0]
                )
                kind = "change" if prior is None or str(prior["state_hash"]) != state_hash else "heartbeat"
                connection.execute(
                    "INSERT INTO platform_replay_observation_events "
                    "(cohort_id, sequence, contract_id, kind, lock_phase, state_hash, fee_hash, "
                    "request_started_at, received_at, venue_timestamp, timestamp_provenance) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        cohort_id,
                        sequence,
                        contract_id,
                        kind,
                        lock_phase,
                        state_hash,
                        fee_hash,
                        request_iso,
                        receipt_iso,
                        "local_request_receipt" if request_iso else "local_observed_at",
                    ),
                )
                event = {
                    "sequence": sequence,
                    "contract_id": contract_id,
                    "kind": kind,
                    "lock_phase": lock_phase,
                    "state_hash": state_hash,
                    "fee_hash": fee_hash,
                    "request_started_at": request_iso,
                    "received_at": receipt_iso,
                    "venue_timestamp": None,
                    "timestamp_provenance": (
                        "local_request_receipt" if request_iso else "local_observed_at"
                    ),
                }
            self._record_successful_observation_row(
                connection,
                cohort_id=cohort_id,
                contract_id=contract_id,
                observed_at=observed_at,
                request_started_at=request_started_at,
                received_at=received_at,
            )
        return {"state_hash": state_hash, "fee_hash": fee_hash, "event": event}

    def replay_observation_events(self, *, cohort_id: str) -> list[dict[str, Any]]:
        """Return append-only replay events in their durable cohort sequence."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT sequence, contract_id, kind, lock_phase, state_hash, fee_hash, "
                "request_started_at, received_at, venue_timestamp, timestamp_provenance "
                "FROM platform_replay_observation_events WHERE cohort_id = ? "
                "ORDER BY sequence",
                (cohort_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def record_successful_observation(
        self,
        *,
        cohort_id: str,
        contract_id: str,
        observed_at: datetime,
        request_started_at: datetime | None = None,
        received_at: datetime | None = None,
    ) -> None:
        """Record a completed book observation, including valid empty depth.

        Request and receipt timestamps are local process facts.  They are
        deliberately optional for old callers, but never inferred from the
        adapter's book timestamp.
        """
        with self._lock, self._connection:
            self._record_successful_observation_row(
                self._connection,
                cohort_id=cohort_id,
                contract_id=contract_id,
                observed_at=observed_at,
                request_started_at=request_started_at,
                received_at=received_at,
            )

    @staticmethod
    def _record_successful_observation_row(
        connection: sqlite3.Connection,
        *,
        cohort_id: str,
        contract_id: str,
        observed_at: datetime,
        request_started_at: datetime | None,
        received_at: datetime | None,
    ) -> None:
        """Write success telemetry using the caller's already-open transaction."""
        connection.execute(
            "INSERT INTO platform_observations "
            "(cohort_id, contract_id, observation_count, first_observed_at, "
            "last_observed_at, last_request_started_at, last_received_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?) "
            "ON CONFLICT(cohort_id, contract_id) DO UPDATE SET "
            "observation_count=platform_observations.observation_count + 1, "
            "last_observed_at=excluded.last_observed_at, "
            "last_request_started_at=excluded.last_request_started_at, "
            "last_received_at=excluded.last_received_at",
            (
                cohort_id,
                contract_id,
                _utc_iso(observed_at),
                _utc_iso(observed_at),
                _utc_iso(request_started_at) if request_started_at else None,
                _utc_iso(received_at) if received_at else None,
            ),
        )

    def observation_telemetry(self, *, cohort_id: str) -> dict[str, dict[str, Any]]:
        """Return durable observation facts, deliberately scoped to one cohort."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT contract_id, observation_count, first_observed_at, "
                "last_observed_at, "
                "last_request_started_at, last_received_at "
                "FROM platform_observations WHERE cohort_id = ? ORDER BY contract_id",
                (cohort_id,),
            ).fetchall()
        return {
            str(row["contract_id"]): {
                "observation_count": int(row["observation_count"]),
                "first_observed_at": str(row["first_observed_at"]),
                "last_observed_at": str(row["last_observed_at"]),
                "last_request_started_at": row["last_request_started_at"],
                "last_received_at": row["last_received_at"],
            }
            for row in rows
        }

    def record_observation_failure(
        self,
        *,
        cohort_id: str,
        contract_id: str,
        reason_code: str,
        failed_at: datetime,
    ) -> None:
        """Persist a failed read without treating it as a successful observation."""
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO platform_observation_failures "
                "(cohort_id, contract_id, reason_code, failure_count, last_failed_at) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(cohort_id, contract_id, reason_code) DO UPDATE SET "
                "failure_count=platform_observation_failures.failure_count + 1, "
                "last_failed_at=excluded.last_failed_at",
                (cohort_id, contract_id, reason_code, _utc_iso(failed_at)),
            )

    def observation_failure_telemetry(
        self, *, cohort_id: str
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Return durable read failures, isolated from successful observation facts."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT contract_id, reason_code, failure_count, last_failed_at "
                "FROM platform_observation_failures WHERE cohort_id = ? "
                "ORDER BY contract_id, reason_code",
                (cohort_id,),
            ).fetchall()
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(str(row["contract_id"]), {})[str(row["reason_code"])] = {
                "failure_count": int(row["failure_count"]),
                "last_failed_at": str(row["last_failed_at"]),
            }
        return result

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
        return {
            str(row["contract_id"]): json.loads(row["payload_json"]) for row in rows
        }

    def current_contract_payloads_for_venue(
        self, venue: str
    ) -> dict[str, dict[str, Any]]:
        """Return the bounded current cohort for one venue only."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT contract_id, payload_json FROM platform_contract_current "
                "WHERE venue = ? ORDER BY contract_id",
                (venue,),
            ).fetchall()
        return {
            str(row["contract_id"]): json.loads(row["payload_json"]) for row in rows
        }

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

    def research_mark_summary(self, *, cohort_id: str | None = None) -> dict[str, Any]:
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
