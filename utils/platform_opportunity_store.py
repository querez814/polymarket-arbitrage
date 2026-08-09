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
from typing import Any, Iterable, Mapping


class ReplayEvidenceCapacityError(RuntimeError):
    """A cohort cannot remain replay-valid after its durable evidence cap is hit."""


class ReplayEvidenceIntegrityError(RuntimeError):
    """A hash-addressed replay payload no longer matches its stored digest."""


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
                    book_received_at TEXT,
                    book_latency_ms INTEGER,
                    fee_request_started_at TEXT,
                    fee_received_at TEXT,
                    fee_latency_ms INTEGER,
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

                -- The political experimental paper ledger is purpose-built
                -- for causal shadow fills.  It is intentionally separate from
                -- generic paper trading and records integer micros only.
                CREATE TABLE IF NOT EXISTS political_experimental_paper_accounts (
                    cohort_id TEXT PRIMARY KEY,
                    starting_cash_micros INTEGER NOT NULL CHECK(starting_cash_micros >= 0),
                    policy_json TEXT,
                    policy_hash TEXT,
                    cash_micros INTEGER NOT NULL CHECK(cash_micros >= 0),
                    reserved_micros INTEGER NOT NULL CHECK(reserved_micros >= 0),
                    realized_pnl_micros INTEGER NOT NULL,
                    initialized_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS political_experimental_paper_events (
                    cohort_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    cash_micros INTEGER NOT NULL CHECK(cash_micros >= 0),
                    reserved_micros INTEGER NOT NULL CHECK(reserved_micros >= 0),
                    realized_pnl_micros INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(cohort_id, sequence)
                );
                -- A reaction signal is not an order.  Retaining it separately
                -- from the legacy shadow intents gives the experimental ledger
                -- a restart-safe, causal hand-off to a later replay book.
                CREATE TABLE IF NOT EXISTS political_experimental_pending_signals (
                    signal_id TEXT PRIMARY KEY,
                    cohort_id TEXT NOT NULL,
                    replay_sequence INTEGER NOT NULL CHECK(replay_sequence > 0),
                    event_id TEXT NOT NULL,
                    milestone_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('yes', 'no')),
                    base_lane TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN ('hot', 'event_live')),
                    signal_request_started_at TEXT NOT NULL,
                    signal_received_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    fee_hash TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_political_pending_signals_due
                    ON political_experimental_pending_signals(cohort_id, contract_id, expires_at);
                -- A fill attempt consumes a pending signal exactly once.  Keeping
                -- no-fills here makes causal failures auditable rather than an
                -- absence of a trade row.
                CREATE TABLE IF NOT EXISTS political_experimental_fill_attempts (
                    signal_id TEXT PRIMARY KEY REFERENCES political_experimental_pending_signals(signal_id),
                    cohort_id TEXT NOT NULL,
                    replay_sequence INTEGER NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('filled', 'no_fill')),
                    reason TEXT,
                    attempted_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                -- A resolver can be asked to consume a signal that was never
                -- durably created (for example after an interrupted runtime
                -- handoff).  It cannot use the normal fill-attempt table: that
                -- table correctly foreign-keys real signals.  Preserve this
                -- rejection in a separate append-only audit row instead of
                -- attempting an invalid child insert.
                CREATE TABLE IF NOT EXISTS political_experimental_orphan_fill_attempts (
                    cohort_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    replay_sequence INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(cohort_id, signal_id)
                );
                CREATE TABLE IF NOT EXISTS political_experimental_positions (
                    position_id TEXT PRIMARY KEY,
                    cohort_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL UNIQUE REFERENCES political_experimental_pending_signals(signal_id),
                    event_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    base_lane TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('yes', 'no')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    cost_basis_micros INTEGER NOT NULL CHECK(cost_basis_micros > 0),
                    opened_at TEXT NOT NULL,
                    UNIQUE(cohort_id, contract_id),
                    UNIQUE(cohort_id, event_id, base_lane)
                );
                -- Exit attempts are keyed by the immutable replay observation
                -- used to value an open position.  This makes retrying a worker
                -- after a crash idempotent without permitting the same book to
                -- release capital twice.
                CREATE TABLE IF NOT EXISTS political_experimental_exit_attempts (
                    cohort_id TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    replay_sequence INTEGER NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('closed', 'partial', 'no_exit')),
                    reason TEXT,
                    attempted_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(cohort_id, position_id, replay_sequence)
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
            replay_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(platform_replay_observation_events)"
                )
            }
            for name, definition in (
                ("book_received_at", "TEXT"),
                ("book_latency_ms", "INTEGER"),
                ("fee_request_started_at", "TEXT"),
                ("fee_received_at", "TEXT"),
                ("fee_latency_ms", "INTEGER"),
            ):
                if name not in replay_columns:
                    self._connection.execute(
                        "ALTER TABLE platform_replay_observation_events "
                        f"ADD COLUMN {name} {definition}"
                    )
            position_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(political_experimental_positions)"
                )
            }
            if "event_id" not in position_columns:
                # Earlier iterations incorrectly made base lanes globally
                # exclusive.  Preserve existing positions while deriving the
                # reviewed event identity from their immutable pending signal.
                self._connection.execute(
                    "ALTER TABLE political_experimental_positions "
                    "RENAME TO political_experimental_positions_legacy"
                )
                self._connection.execute(
                    "CREATE TABLE political_experimental_positions ("
                    "position_id TEXT PRIMARY KEY, "
                    "cohort_id TEXT NOT NULL, "
                    "signal_id TEXT NOT NULL UNIQUE REFERENCES "
                    "political_experimental_pending_signals(signal_id), "
                    "event_id TEXT NOT NULL, contract_id TEXT NOT NULL, "
                    "base_lane TEXT NOT NULL, "
                    "side TEXT NOT NULL CHECK(side IN ('yes', 'no')), "
                    "quantity INTEGER NOT NULL CHECK(quantity > 0), "
                    "cost_basis_micros INTEGER NOT NULL CHECK(cost_basis_micros > 0), "
                    "opened_at TEXT NOT NULL, "
                    "UNIQUE(cohort_id, contract_id), "
                    "UNIQUE(cohort_id, event_id, base_lane))"
                )
                self._connection.execute(
                    "INSERT INTO political_experimental_positions "
                    "(position_id, cohort_id, signal_id, event_id, contract_id, "
                    "base_lane, side, quantity, cost_basis_micros, opened_at) "
                    "SELECT position_id, p.cohort_id, p.signal_id, s.event_id, "
                    "p.contract_id, p.base_lane, p.side, p.quantity, "
                    "p.cost_basis_micros, p.opened_at "
                    "FROM political_experimental_positions_legacy AS p "
                    "JOIN political_experimental_pending_signals AS s "
                    "ON s.signal_id = p.signal_id"
                )
                self._connection.execute(
                    "DROP TABLE political_experimental_positions_legacy"
                )
            account_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(political_experimental_paper_accounts)"
                )
            }
            for name in ("policy_json", "policy_hash"):
                if name not in account_columns:
                    self._connection.execute(
                        "ALTER TABLE political_experimental_paper_accounts "
                        f"ADD COLUMN {name} TEXT"
                    )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def initialize_political_experimental_paper_account(
        self,
        *,
        cohort_id: str,
        starting_cash_micros: int,
        initialized_at: datetime,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, int | bool]:
        """Create one durable political-paper account without resetting restarts.

        The initialized after-state is append-only evidence.  Future signal and
        fill transitions will use the same account/event transaction and retain
        the invariant ``cash + reserved == starting_cash + realized_pnl``.
        """
        if not cohort_id.strip():
            raise ValueError("cohort_id must be non-empty")
        if (
            not isinstance(starting_cash_micros, int)
            or isinstance(starting_cash_micros, bool)
            or starting_cash_micros < 0
        ):
            raise ValueError("starting_cash_micros must be a non-negative integer")
        policy_payload = dict(policy or {})
        policy_payload.setdefault("schema_version", 1)
        policy_payload["starting_cash_micros"] = starting_cash_micros
        policy_json = _json(policy_payload)
        policy_hash = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
        initialized_iso = _utc_iso(initialized_at)
        # A process-local lock is not enough: restart recovery and workers may
        # use separate store connections.  Take SQLite's writer reservation
        # before reading so exactly one initializer can create the account and
        # its first append-only after-state.
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT starting_cash_micros, policy_json, policy_hash, cash_micros, reserved_micros, "
                    "realized_pnl_micros FROM political_experimental_paper_accounts "
                    "WHERE cohort_id = ?",
                    (cohort_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO political_experimental_paper_accounts "
                        "(cohort_id, starting_cash_micros, policy_json, policy_hash, cash_micros, reserved_micros, "
                        "realized_pnl_micros, initialized_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)",
                        (
                            cohort_id,
                            starting_cash_micros,
                            policy_json,
                            policy_hash,
                            starting_cash_micros,
                            initialized_iso,
                            initialized_iso,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO political_experimental_paper_events "
                        "(cohort_id, sequence, event_type, occurred_at, cash_micros, "
                        "reserved_micros, realized_pnl_micros, payload_json) "
                        "VALUES (?, 1, 'account_initialized', ?, ?, 0, 0, ?)",
                        (
                            cohort_id,
                            initialized_iso,
                            starting_cash_micros,
                            _json({"starting_cash_micros": starting_cash_micros}),
                        ),
                    )
                    existing = connection.execute(
                        "SELECT starting_cash_micros, policy_json, policy_hash, cash_micros, reserved_micros, "
                        "realized_pnl_micros FROM political_experimental_paper_accounts "
                        "WHERE cohort_id = ?",
                        (cohort_id,),
                    ).fetchone()
                assert existing is not None
                if int(existing["starting_cash_micros"]) != starting_cash_micros:
                    raise ValueError(
                        "political experimental paper account policy drift: "
                        "starting_cash_micros differs for cohort"
                    )
                if (
                    existing["policy_json"] is None
                    or existing["policy_hash"] is None
                    or str(existing["policy_json"]) != policy_json
                    or str(existing["policy_hash"]) != policy_hash
                ):
                    raise ValueError(
                        "political experimental paper account policy drift: "
                        "immutable policy differs for cohort"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            assert existing is not None
            account = {
                key: int(existing[key])
                for key in (
                    "starting_cash_micros",
                    "cash_micros",
                    "reserved_micros",
                    "realized_pnl_micros",
                )
            }
        if account["cash_micros"] + account["reserved_micros"] != (
            account["starting_cash_micros"] + account["realized_pnl_micros"]
        ):
            raise RuntimeError("political experimental paper account identity violated")
        return {
            **account,
            "open_positions": 0,
            "valuation_complete": True,
        }

    def political_experimental_paper_policy(self, *, cohort_id: str) -> dict[str, Any]:
        """Return the immutable canonical policy bound to this paper account."""
        with self._lock:
            row = self._connection.execute(
                "SELECT policy_json, policy_hash "
                "FROM political_experimental_paper_accounts WHERE cohort_id = ?",
                (cohort_id,),
            ).fetchone()
        if row is None or row["policy_json"] is None or row["policy_hash"] is None:
            raise RuntimeError("political experimental paper policy is unavailable")
        policy_json = str(row["policy_json"])
        policy_hash = str(row["policy_hash"])
        if hashlib.sha256(policy_json.encode("utf-8")).hexdigest() != policy_hash:
            raise RuntimeError("political experimental paper policy integrity violated")
        return {"policy": json.loads(policy_json), "policy_hash": policy_hash}

    def political_experimental_paper_account(self, *, cohort_id: str) -> dict[str, int]:
        """Read the current account state used to size a conservative prefix."""
        with self._lock:
            row = self._connection.execute(
                "SELECT starting_cash_micros, cash_micros, reserved_micros, "
                "realized_pnl_micros FROM political_experimental_paper_accounts "
                "WHERE cohort_id = ?",
                (cohort_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "political experimental paper account is not initialized"
            )
        account = {key: int(row[key]) for key in row.keys()}
        if account["cash_micros"] + account["reserved_micros"] != (
            account["starting_cash_micros"] + account["realized_pnl_micros"]
        ):
            raise RuntimeError("political experimental paper account identity violated")
        return account

    def political_experimental_paper_events(
        self, *, cohort_id: str
    ) -> list[dict[str, Any]]:
        """Read append-only political-paper after-states in sequence order."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT sequence, event_type, occurred_at, cash_micros, reserved_micros, "
                "realized_pnl_micros, payload_json "
                "FROM political_experimental_paper_events WHERE cohort_id = ? "
                "ORDER BY sequence",
                (cohort_id,),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "event_type": str(row["event_type"]),
                "occurred_at": str(row["occurred_at"]),
                "cash_micros": int(row["cash_micros"]),
                "reserved_micros": int(row["reserved_micros"]),
                "realized_pnl_micros": int(row["realized_pnl_micros"]),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def record_political_experimental_pending_signal(
        self,
        *,
        signal_id: str,
        cohort_id: str,
        replay_sequence: int,
        event_id: str,
        milestone_id: str,
        contract_id: str,
        side: str,
        base_lane: str,
        phase: str,
        signal_request_started_at: datetime,
        signal_received_at: datetime,
        expires_at: datetime,
        model_version: str,
        config_hash: str,
        state_hash: str,
        fee_hash: str,
        features: dict[str, Any],
    ) -> bool:
        """Persist one causal reaction signal, idempotently and fail-closed.

        This deliberately has no fill semantics: only a later replay event may
        consume it.  Requiring the account and valid evidence cohort prevents a
        caller from creating an orphan signal outside the paper boundary.
        """
        required = (
            signal_id,
            cohort_id,
            event_id,
            milestone_id,
            contract_id,
            base_lane,
            model_version,
            config_hash,
            state_hash,
            fee_hash,
        )
        if not all(isinstance(value, str) and value.strip() for value in required):
            raise ValueError("pending signal identity and provenance are required")
        if (
            not isinstance(replay_sequence, int)
            or isinstance(replay_sequence, bool)
            or replay_sequence <= 0
        ):
            raise ValueError(
                "pending signal replay_sequence must be a positive integer"
            )
        if side not in {"yes", "no"}:
            raise ValueError("pending signal side must be yes or no")
        if phase not in {"hot", "event_live"}:
            raise ValueError("pending signal phase must be hot or event_live")
        request_iso = _utc_iso(signal_request_started_at)
        received_iso = _utc_iso(signal_received_at)
        expiry_iso = _utc_iso(expires_at)
        if request_iso >= received_iso or received_iso >= expiry_iso:
            raise ValueError("pending signal timestamps must be causally ordered")
        with self._lock, self._connection:
            connection = self._connection
            account = connection.execute(
                "SELECT 1 FROM political_experimental_paper_accounts WHERE cohort_id = ?",
                (cohort_id,),
            ).fetchone()
            if account is None:
                raise RuntimeError(
                    "political experimental paper account is not initialized"
                )
            status = connection.execute(
                "SELECT cohort_valid, degraded_reason FROM platform_replay_evidence_status "
                "WHERE cohort_id = ?",
                (cohort_id,),
            ).fetchone()
            if status is not None and not bool(status["cohort_valid"]):
                raise ReplayEvidenceCapacityError(
                    "replay evidence cohort is invalid: "
                    f"{status['degraded_reason'] or 'unknown'}"
                )
            replay_event = connection.execute(
                "SELECT contract_id, lock_phase, state_hash, fee_hash, "
                "request_started_at, received_at "
                "FROM platform_replay_observation_events "
                "WHERE cohort_id = ? AND sequence = ?",
                (cohort_id, replay_sequence),
            ).fetchone()
            if replay_event is None:
                raise ValueError("pending signal must reference a durable replay event")
            expected_base_lane = {
                "hot": "hot_pre_event",
                "event_live": "event_live",
            }.get(str(replay_event["lock_phase"]))
            if expected_base_lane is None or any(
                (
                    str(replay_event["contract_id"]) != contract_id,
                    str(replay_event["lock_phase"]) != phase,
                    expected_base_lane != base_lane,
                    str(replay_event["state_hash"]) != state_hash,
                    str(replay_event["fee_hash"]) != fee_hash,
                    replay_event["request_started_at"] != request_iso,
                    str(replay_event["received_at"]) != received_iso,
                )
            ):
                raise ValueError(
                    "pending signal provenance does not match replay event"
                )
            values = (
                signal_id,
                cohort_id,
                replay_sequence,
                event_id,
                milestone_id,
                contract_id,
                side,
                base_lane,
                phase,
                request_iso,
                received_iso,
                expiry_iso,
                model_version,
                config_hash,
                state_hash,
                fee_hash,
                _json(features),
            )
            existing = connection.execute(
                "SELECT signal_id, cohort_id, replay_sequence, event_id, milestone_id, "
                "contract_id, side, base_lane, phase, signal_request_started_at, "
                "signal_received_at, expires_at, model_version, config_hash, state_hash, "
                "fee_hash, features_json FROM political_experimental_pending_signals "
                "WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) == values:
                    return False
                raise ValueError("pending signal id collision has different payload")
            connection.execute(
                "INSERT INTO political_experimental_pending_signals "
                "(signal_id, cohort_id, replay_sequence, event_id, milestone_id, contract_id, "
                "side, base_lane, phase, signal_request_started_at, signal_received_at, "
                "expires_at, model_version, config_hash, state_hash, fee_hash, features_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *values,
                    received_iso,
                ),
            )
        return True

    def political_experimental_pending_signals(
        self, *, cohort_id: str
    ) -> list[dict[str, Any]]:
        """Read only unconsumed causal signals in durable sequence order."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT p.signal_id, p.replay_sequence, p.event_id, p.milestone_id, "
                "p.contract_id, p.side, p.base_lane, p.phase, p.signal_request_started_at, "
                "p.signal_received_at, p.expires_at, p.model_version, p.config_hash, "
                "p.state_hash, p.fee_hash, p.features_json "
                "FROM political_experimental_pending_signals AS p "
                "LEFT JOIN political_experimental_fill_attempts AS f "
                "ON f.signal_id = p.signal_id "
                "WHERE p.cohort_id = ? AND f.signal_id IS NULL "
                "ORDER BY p.replay_sequence, p.signal_id",
                (cohort_id,),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "features_json"},
                "replay_sequence": int(row["replay_sequence"]),
                "features": json.loads(str(row["features_json"])),
            }
            for row in rows
        ]

    def expire_political_experimental_pending_signals(
        self, *, cohort_id: str, as_of: datetime
    ) -> list[dict[str, Any]]:
        """Terminally no-fill every due signal without requiring a later book.

        A signal's replay sequence remains its immutable provenance when no
        later observation exists.  The expiry attempt itself is timestamped by
        the sweeper clock and consumes the signal in the same immediate
        transaction, making retries and restarts idempotent.
        """
        as_of_iso = _utc_iso(as_of)
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT p.signal_id, p.replay_sequence, p.expires_at "
                    "FROM political_experimental_pending_signals AS p "
                    "LEFT JOIN political_experimental_fill_attempts AS f "
                    "ON f.signal_id = p.signal_id "
                    "WHERE p.cohort_id = ? AND f.signal_id IS NULL "
                    "AND p.expires_at <= ? "
                    "ORDER BY p.replay_sequence, p.signal_id",
                    (cohort_id, as_of_iso),
                ).fetchall()
                expired: list[dict[str, Any]] = []
                for row in rows:
                    signal_id = str(row["signal_id"])
                    replay_sequence = int(row["replay_sequence"])
                    payload = {
                        "replay_sequence": replay_sequence,
                        "expires_at": str(row["expires_at"]),
                        "expired_as_of": as_of_iso,
                    }
                    connection.execute(
                        "INSERT INTO political_experimental_fill_attempts "
                        "(signal_id, cohort_id, replay_sequence, outcome, reason, "
                        "attempted_at, payload_json) VALUES (?, ?, ?, 'no_fill', ?, ?, ?)",
                        (
                            signal_id,
                            cohort_id,
                            replay_sequence,
                            "ttl_expired_without_later_book",
                            as_of_iso,
                            _json(payload),
                        ),
                    )
                    expired.append(
                        {
                            "signal_id": signal_id,
                            "replay_sequence": replay_sequence,
                            "reason": "ttl_expired_without_later_book",
                            "payload": payload,
                        }
                    )
                connection.commit()
                return expired
            except Exception:
                connection.rollback()
                raise

    def political_experimental_orphan_fill_attempts(
        self, *, cohort_id: str
    ) -> list[dict[str, Any]]:
        """Return durable no-fills that have no signal row to reference."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT signal_id, replay_sequence, reason, attempted_at, payload_json "
                "FROM political_experimental_orphan_fill_attempts "
                "WHERE cohort_id = ? ORDER BY attempted_at, signal_id",
                (cohort_id,),
            ).fetchall()
        return [
            {
                "signal_id": str(row["signal_id"]),
                "replay_sequence": int(row["replay_sequence"]),
                "reason": str(row["reason"]),
                "attempted_at": str(row["attempted_at"]),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def political_experimental_positions(
        self, *, cohort_id: str
    ) -> list[dict[str, Any]]:
        """Return the remaining open positions; closed state remains in events."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT position_id, signal_id, event_id, contract_id, base_lane, side, "
                "quantity, cost_basis_micros, opened_at FROM political_experimental_positions "
                "WHERE cohort_id = ? ORDER BY opened_at, position_id",
                (cohort_id,),
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def political_experimental_paper_snapshot(
        self, *, cohort_id: str
    ) -> dict[str, Any]:
        """Read one invariant-checked, durable political-paper ledger view.

        This is deliberately a read model, not a second accounting path.  It
        gives the runtime and dashboard one coherent way to expose the
        experimental ledger without mixing it with standard paper or research
        marks.  The short SQLite read transaction prevents a writer on this
        connection from interleaving the account, position, and lifecycle
        counters used by one snapshot.
        """
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN")
            try:
                account_row = connection.execute(
                    "SELECT starting_cash_micros, cash_micros, reserved_micros, "
                    "realized_pnl_micros FROM political_experimental_paper_accounts "
                    "WHERE cohort_id = ?",
                    (cohort_id,),
                ).fetchone()
                if account_row is None:
                    raise RuntimeError(
                        "political experimental paper account is not initialized"
                    )
                account = {key: int(account_row[key]) for key in account_row.keys()}
                positions = connection.execute(
                    "SELECT position_id, signal_id, event_id, contract_id, base_lane, "
                    "side, quantity, cost_basis_micros, opened_at "
                    "FROM political_experimental_positions WHERE cohort_id = ? "
                    "ORDER BY opened_at, position_id",
                    (cohort_id,),
                ).fetchall()
                open_positions = [
                    {
                        **{key: row[key] for key in row.keys()},
                        "quantity": int(row["quantity"]),
                        "cost_basis_micros": int(row["cost_basis_micros"]),
                    }
                    for row in positions
                ]
                signal_counts = connection.execute(
                    "SELECT COUNT(*) AS signals, "
                    "SUM(CASE WHEN f.signal_id IS NULL THEN 1 ELSE 0 END) AS pending, "
                    "SUM(CASE WHEN f.outcome = 'filled' THEN 1 ELSE 0 END) AS filled, "
                    "SUM(CASE WHEN f.outcome = 'no_fill' THEN 1 ELSE 0 END) AS no_fill "
                    "FROM political_experimental_pending_signals AS p "
                    "LEFT JOIN political_experimental_fill_attempts AS f "
                    "ON f.signal_id = p.signal_id WHERE p.cohort_id = ?",
                    (cohort_id,),
                ).fetchone()
                event_counts = connection.execute(
                    "SELECT "
                    "SUM(CASE WHEN event_type = 'position_closed' THEN 1 ELSE 0 END) "
                    "AS closed, "
                    "SUM(CASE WHEN event_type = 'position_partially_closed' THEN 1 ELSE 0 END) "
                    "AS partial_exits "
                    "FROM political_experimental_paper_events WHERE cohort_id = ?",
                    (cohort_id,),
                ).fetchone()
                orphan_no_fills = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM political_experimental_orphan_fill_attempts "
                        "WHERE cohort_id = ?",
                        (cohort_id,),
                    ).fetchone()[0]
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if account["cash_micros"] + account["reserved_micros"] != (
            account["starting_cash_micros"] + account["realized_pnl_micros"]
        ):
            raise RuntimeError("political experimental paper account identity violated")
        reserved_from_positions = sum(
            int(position["cost_basis_micros"]) for position in open_positions
        )
        if account["reserved_micros"] != reserved_from_positions:
            raise RuntimeError(
                "political experimental paper reserved basis invariant violated"
            )
        return {
            "account": account,
            "open_positions": open_positions,
            "counts": {
                "signals": int(signal_counts["signals"] or 0),
                "pending": int(signal_counts["pending"] or 0),
                "filled": int(signal_counts["filled"] or 0),
                "no_fill": int(signal_counts["no_fill"] or 0) + orphan_no_fills,
                "open": len(open_positions),
                "closed": int(event_counts["closed"] or 0),
                "partial_exits": int(event_counts["partial_exits"] or 0),
            },
        }

    def exit_political_experimental_position(
        self,
        *,
        cohort_id: str,
        position_id: str,
        replay_sequence: int,
        attempted_at: datetime,
        quantity: int,
        credit_micros: int,
        basis_release_micros: int,
        economics: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically release a replay-backed whole-contract political position.

        The ledger derives ``quantity`` and ``credit_micros`` from the exact
        persisted bid book.  This transaction proves the book is later than
        the opening replay, applies proportional basis release, and records an
        append-only account after-state.
        """
        if (
            not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity <= 0
            or not isinstance(credit_micros, int)
            or credit_micros < 0
            or not isinstance(basis_release_micros, int)
            or basis_release_micros <= 0
        ):
            raise ValueError(
                "political paper exit values must be positive integer micros"
            )
        attempted_iso = _utc_iso(attempted_at)
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT outcome, reason, payload_json FROM political_experimental_exit_attempts "
                    "WHERE cohort_id = ? AND position_id = ? AND replay_sequence = ?",
                    (cohort_id, position_id, replay_sequence),
                ).fetchone()
                if prior is not None:
                    connection.commit()
                    return {
                        "outcome": str(prior["outcome"]),
                        "reason": prior["reason"],
                        "idempotent": True,
                        "payload": json.loads(str(prior["payload_json"])),
                    }
                position = connection.execute(
                    "SELECT p.*, f.replay_sequence AS opened_replay_sequence FROM political_experimental_positions AS p "
                    "JOIN political_experimental_fill_attempts AS f ON f.signal_id = p.signal_id "
                    "WHERE p.cohort_id = ? AND p.position_id = ?",
                    (cohort_id, position_id),
                ).fetchone()
                event = connection.execute(
                    "SELECT contract_id FROM platform_replay_observation_events WHERE cohort_id = ? AND sequence = ?",
                    (cohort_id, replay_sequence),
                ).fetchone()
                payload = {
                    "replay_sequence": replay_sequence,
                    "quantity": quantity,
                    "credit_micros": credit_micros,
                    "basis_release_micros": basis_release_micros,
                    "economics": economics,
                }
                reason: str | None = None
                if position is None:
                    reason = "missing_open_position"
                elif event is None:
                    reason = "missing_replay_event"
                elif replay_sequence <= int(position["opened_replay_sequence"]):
                    reason = "not_later_replay_sequence"
                elif str(event["contract_id"]) != str(position["contract_id"]):
                    reason = "contract_mismatch"
                elif quantity > int(position["quantity"]):
                    reason = "exit_quantity_exceeds_position"
                elif basis_release_micros > int(position["cost_basis_micros"]):
                    reason = "basis_release_exceeds_position"
                if reason is not None:
                    connection.execute(
                        "INSERT INTO political_experimental_exit_attempts "
                        "(cohort_id, position_id, replay_sequence, outcome, reason, attempted_at, payload_json) VALUES (?, ?, ?, 'no_exit', ?, ?, ?)",
                        (
                            cohort_id,
                            position_id,
                            replay_sequence,
                            reason,
                            attempted_iso,
                            _json(payload),
                        ),
                    )
                    connection.commit()
                    return {
                        "outcome": "no_exit",
                        "reason": reason,
                        "idempotent": False,
                        "payload": payload,
                    }
                assert position is not None
                account = connection.execute(
                    "SELECT * FROM political_experimental_paper_accounts WHERE cohort_id = ?",
                    (cohort_id,),
                ).fetchone()
                if account is None:
                    raise RuntimeError(
                        "political experimental paper account is not initialized"
                    )
                remaining_quantity = int(position["quantity"]) - quantity
                remaining_basis = (
                    int(position["cost_basis_micros"]) - basis_release_micros
                )
                if remaining_quantity == 0 and remaining_basis != 0:
                    # The last close must release every remaining micro, even
                    # after earlier floor-proportional partial exits.
                    basis_release_micros += remaining_basis
                    remaining_basis = 0
                    payload["basis_release_micros"] = basis_release_micros
                if remaining_quantity > 0 and remaining_basis <= 0:
                    raise RuntimeError("partial exit must retain positive cost basis")
                cash = int(account["cash_micros"]) + credit_micros
                reserved = int(account["reserved_micros"]) - basis_release_micros
                realized = (
                    int(account["realized_pnl_micros"])
                    + credit_micros
                    - basis_release_micros
                )
                if (
                    reserved < 0
                    or cash < 0
                    or cash + reserved
                    != int(account["starting_cash_micros"]) + realized
                ):
                    raise RuntimeError(
                        "political experimental paper account identity violated"
                    )
                if remaining_quantity == 0:
                    connection.execute(
                        "DELETE FROM political_experimental_positions WHERE position_id = ?",
                        (position_id,),
                    )
                    outcome, event_type = "closed", "position_closed"
                else:
                    connection.execute(
                        "UPDATE political_experimental_positions SET quantity = ?, cost_basis_micros = ? WHERE position_id = ?",
                        (remaining_quantity, remaining_basis, position_id),
                    )
                    outcome, event_type = "partial", "position_partially_closed"
                connection.execute(
                    "UPDATE political_experimental_paper_accounts SET cash_micros = ?, reserved_micros = ?, realized_pnl_micros = ?, updated_at = ? WHERE cohort_id = ?",
                    (cash, reserved, realized, attempted_iso, cohort_id),
                )
                payload.update(
                    {
                        "position_id": position_id,
                        "remaining_quantity": remaining_quantity,
                        "remaining_basis_micros": remaining_basis,
                    }
                )
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM political_experimental_paper_events WHERE cohort_id = ?",
                        (cohort_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO political_experimental_paper_events (cohort_id, sequence, event_type, occurred_at, cash_micros, reserved_micros, realized_pnl_micros, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cohort_id,
                        sequence,
                        event_type,
                        attempted_iso,
                        cash,
                        reserved,
                        realized,
                        _json(payload),
                    ),
                )
                connection.execute(
                    "INSERT INTO political_experimental_exit_attempts (cohort_id, position_id, replay_sequence, outcome, reason, attempted_at, payload_json) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (
                        cohort_id,
                        position_id,
                        replay_sequence,
                        outcome,
                        attempted_iso,
                        _json(payload),
                    ),
                )
                connection.commit()
                return {
                    "outcome": outcome,
                    "reason": None,
                    "idempotent": False,
                    "payload": payload,
                }
            except Exception:
                connection.rollback()
                raise

    def resolve_political_experimental_pending_signal(
        self,
        *,
        cohort_id: str,
        signal_id: str,
        replay_sequence: int,
        attempted_at: datetime,
        quantity: int,
        debit_micros: int,
        max_total_reserved_micros: int,
        max_position_reserved_micros: int,
        max_open_positions: int,
        economics: dict[str, Any],
    ) -> dict[str, Any]:
        """Consume one signal only from a strictly later causal replay event.

        This is deliberately the narrow signal-to-open transition: the caller
        has already quoted conservative economics from the persisted book and
        fee payload, while this transaction proves causality, reserves capital,
        creates the position, and records the after-state together.
        """
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("political paper fill quantity must be whole and positive")
        if not isinstance(debit_micros, int) or debit_micros <= 0:
            raise ValueError(
                "political paper fill debit must be positive integer micros"
            )
        if (
            min(
                max_total_reserved_micros,
                max_position_reserved_micros,
                max_open_positions,
            )
            <= 0
        ):
            raise ValueError("political paper limits must be positive")
        attempted_iso = _utc_iso(attempted_at)
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT outcome, reason, payload_json FROM political_experimental_fill_attempts "
                    "WHERE signal_id = ?",
                    (signal_id,),
                ).fetchone()
                if prior is not None:
                    connection.commit()
                    return {
                        "outcome": str(prior["outcome"]),
                        "reason": prior["reason"],
                        "idempotent": True,
                        "payload": json.loads(str(prior["payload_json"])),
                    }
                signal = connection.execute(
                    "SELECT * FROM political_experimental_pending_signals WHERE signal_id = ? AND cohort_id = ?",
                    (signal_id, cohort_id),
                ).fetchone()
                event = connection.execute(
                    "SELECT * FROM platform_replay_observation_events WHERE cohort_id = ? AND sequence = ?",
                    (cohort_id, replay_sequence),
                ).fetchone()
                payload = {
                    "replay_sequence": replay_sequence,
                    "quantity": quantity,
                    "debit_micros": debit_micros,
                    "economics": economics,
                }
                if signal is None:
                    prior_orphan = connection.execute(
                        "SELECT reason, payload_json FROM political_experimental_orphan_fill_attempts "
                        "WHERE cohort_id = ? AND signal_id = ?",
                        (cohort_id, signal_id),
                    ).fetchone()
                    if prior_orphan is not None:
                        connection.commit()
                        return {
                            "outcome": "no_fill",
                            "reason": str(prior_orphan["reason"]),
                            "idempotent": True,
                            "payload": json.loads(str(prior_orphan["payload_json"])),
                        }
                    connection.execute(
                        "INSERT INTO political_experimental_orphan_fill_attempts "
                        "(cohort_id, signal_id, replay_sequence, reason, attempted_at, payload_json) "
                        "VALUES (?, ?, ?, 'missing_signal', ?, ?)",
                        (
                            cohort_id,
                            signal_id,
                            replay_sequence,
                            attempted_iso,
                            _json(payload),
                        ),
                    )
                    connection.commit()
                    return {
                        "outcome": "no_fill",
                        "reason": "missing_signal",
                        "idempotent": False,
                        "payload": payload,
                    }
                reason: str | None = None
                if event is None:
                    reason = "missing_replay_event"
                elif int(event["sequence"]) <= int(signal["replay_sequence"]):
                    reason = "not_later_replay_sequence"
                elif event["contract_id"] != signal["contract_id"]:
                    reason = "contract_mismatch"
                elif event["lock_phase"] != signal["phase"]:
                    reason = "phase_crossed"
                elif (
                    event["request_started_at"] is None
                    or event["request_started_at"] <= signal["signal_received_at"]
                ):
                    reason = "request_not_strictly_after_signal_receipt"
                elif event["received_at"] > signal["expires_at"]:
                    reason = "signal_ttl_expired"
                elif isinstance(economics.get("preflight_reason"), str):
                    reason = str(economics["preflight_reason"])
                status = connection.execute(
                    "SELECT cohort_valid FROM platform_replay_evidence_status WHERE cohort_id = ?",
                    (cohort_id,),
                ).fetchone()
                if (
                    reason is None
                    and status is not None
                    and not bool(status["cohort_valid"])
                ):
                    reason = "replay_evidence_invalid"
                if reason is not None:
                    connection.execute(
                        "INSERT INTO political_experimental_fill_attempts "
                        "(signal_id, cohort_id, replay_sequence, outcome, reason, attempted_at, payload_json) "
                        "VALUES (?, ?, ?, 'no_fill', ?, ?, ?)",
                        (
                            signal_id,
                            cohort_id,
                            replay_sequence,
                            reason,
                            attempted_iso,
                            _json(payload),
                        ),
                    )
                    connection.commit()
                    return {
                        "outcome": "no_fill",
                        "reason": reason,
                        "idempotent": False,
                        "payload": payload,
                    }
                assert signal is not None
                account = connection.execute(
                    "SELECT * FROM political_experimental_paper_accounts WHERE cohort_id = ?",
                    (cohort_id,),
                ).fetchone()
                if account is None:
                    raise RuntimeError(
                        "political experimental paper account is not initialized"
                    )
                open_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM political_experimental_positions WHERE cohort_id = ?",
                        (cohort_id,),
                    ).fetchone()[0]
                )
                # These are separate constraints. A contract may not be
                # re-opened through a different reporting lane anywhere in a
                # cohort, while a base lane is exclusive only inside the same
                # reviewed event. Query them under the same immediate
                # transaction that opens the position so concurrent resolvers
                # cannot pass either check before the first insert commits.
                contract_position = connection.execute(
                    "SELECT 1 FROM political_experimental_positions "
                    "WHERE cohort_id = ? AND contract_id = ?",
                    (cohort_id, signal["contract_id"]),
                ).fetchone()
                lane_position = connection.execute(
                    "SELECT 1 FROM political_experimental_positions "
                    "WHERE cohort_id = ? AND event_id = ? AND base_lane = ?",
                    (cohort_id, signal["event_id"], signal["base_lane"]),
                ).fetchone()
                if debit_micros > max_position_reserved_micros:
                    reason = "per_position_reserved_cap"
                elif (
                    int(account["reserved_micros"]) + debit_micros
                    > max_total_reserved_micros
                ):
                    reason = "total_reserved_cap"
                elif debit_micros > int(account["cash_micros"]):
                    reason = "insufficient_cash"
                elif open_count >= max_open_positions:
                    reason = "max_open_positions"
                elif contract_position is not None:
                    reason = "contract_overlap"
                elif lane_position is not None:
                    reason = "base_lane_overlap"
                if reason is not None:
                    connection.execute(
                        "INSERT INTO political_experimental_fill_attempts "
                        "(signal_id, cohort_id, replay_sequence, outcome, reason, attempted_at, payload_json) VALUES (?, ?, ?, 'no_fill', ?, ?, ?)",
                        (
                            signal_id,
                            cohort_id,
                            replay_sequence,
                            reason,
                            attempted_iso,
                            _json(payload),
                        ),
                    )
                    connection.commit()
                    return {
                        "outcome": "no_fill",
                        "reason": reason,
                        "idempotent": False,
                        "payload": payload,
                    }
                cash = int(account["cash_micros"]) - debit_micros
                reserved = int(account["reserved_micros"]) + debit_micros
                position_id = f"position:{signal_id}"
                connection.execute(
                    "INSERT INTO political_experimental_positions (position_id, cohort_id, signal_id, event_id, contract_id, base_lane, side, quantity, cost_basis_micros, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        position_id,
                        cohort_id,
                        signal_id,
                        signal["event_id"],
                        signal["contract_id"],
                        signal["base_lane"],
                        signal["side"],
                        quantity,
                        debit_micros,
                        attempted_iso,
                    ),
                )
                connection.execute(
                    "UPDATE political_experimental_paper_accounts SET cash_micros = ?, reserved_micros = ?, updated_at = ? WHERE cohort_id = ?",
                    (cash, reserved, attempted_iso, cohort_id),
                )
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM political_experimental_paper_events WHERE cohort_id = ?",
                        (cohort_id,),
                    ).fetchone()[0]
                )
                payload["position_id"] = position_id
                connection.execute(
                    "INSERT INTO political_experimental_paper_events (cohort_id, sequence, event_type, occurred_at, cash_micros, reserved_micros, realized_pnl_micros, payload_json) VALUES (?, ?, 'position_opened', ?, ?, ?, ?, ?)",
                    (
                        cohort_id,
                        sequence,
                        attempted_iso,
                        cash,
                        reserved,
                        int(account["realized_pnl_micros"]),
                        _json(payload),
                    ),
                )
                connection.execute(
                    "INSERT INTO political_experimental_fill_attempts (signal_id, cohort_id, replay_sequence, outcome, reason, attempted_at, payload_json) VALUES (?, ?, ?, 'filled', NULL, ?, ?)",
                    (
                        signal_id,
                        cohort_id,
                        replay_sequence,
                        attempted_iso,
                        _json(payload),
                    ),
                )
                connection.commit()
                return {
                    "outcome": "filled",
                    "reason": None,
                    "idempotent": False,
                    "payload": payload,
                }
            except Exception:
                connection.rollback()
                raise

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
                            raise ValueError(
                                "normalized book levels must be numeric pairs"
                            )
                        price, size = float(level[0]), float(level[1])
                        if not all(
                            math.isfinite(item) and item > 0 for item in (price, size)
                        ):
                            raise ValueError(
                                "normalized book levels must be finite positive"
                            )
                        if prior is not None and (
                            price > prior if descending else price < prior
                        ):
                            raise ValueError(
                                "normalized book levels are not best-to-worst"
                            )
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
                (
                    payload_hash,
                    int(payload["schema_version"]),
                    compressed,
                    len(compressed),
                ),
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

    def _replay_payload(
        self, *, table: str, hash_column: str, payload_hash: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                f"SELECT compressed_payload FROM {table} WHERE {hash_column} = ?",
                (payload_hash,),
            ).fetchone()
        if row is None:
            raise KeyError(payload_hash)
        try:
            encoded = zlib.decompress(bytes(row["compressed_payload"]))
        except zlib.error as exc:
            self._invalidate_replay_payload_references(
                hash_column=hash_column, payload_hash=payload_hash
            )
            raise ReplayEvidenceIntegrityError(
                f"replay {table} payload cannot be decompressed"
            ) from exc
        actual_hash = hashlib.sha256(encoded).hexdigest()
        if actual_hash != payload_hash:
            self._invalidate_replay_payload_references(
                hash_column=hash_column, payload_hash=payload_hash
            )
            raise ReplayEvidenceIntegrityError(
                f"replay {table} payload digest does not match {hash_column}"
            )
        try:
            return json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._invalidate_replay_payload_references(
                hash_column=hash_column, payload_hash=payload_hash
            )
            raise ReplayEvidenceIntegrityError(
                f"replay {table} payload cannot be decoded"
            ) from exc

    def _invalidate_replay_payload_references(
        self, *, hash_column: str, payload_hash: str
    ) -> None:
        """Permanently invalidate every cohort that cites corrupt evidence.

        A replay hash is shared across cohorts, so treating a failed decode as
        a local reader error could allow another cohort to continue scoring the
        exact same corrupted payload.  The status transitions and all affected
        cohort lookups share one SQLite transaction so restart cannot observe a
        partially invalidated evidence graph.
        """
        if hash_column not in {"state_hash", "fee_hash"}:
            raise ValueError("unsupported replay payload hash column")
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT DISTINCT cohort_id FROM platform_replay_observation_events "
                f"WHERE {hash_column} = ?",
                (payload_hash,),
            ).fetchall()
            for row in rows:
                self._connection.execute(
                    "INSERT INTO platform_replay_evidence_status "
                    "(cohort_id, cohort_valid, degraded_reason) VALUES (?, 0, ?) "
                    "ON CONFLICT(cohort_id) DO UPDATE SET cohort_valid = 0, "
                    "degraded_reason = CASE "
                    "WHEN platform_replay_evidence_status.cohort_valid = 1 "
                    "THEN excluded.degraded_reason "
                    "ELSE platform_replay_evidence_status.degraded_reason END",
                    (str(row["cohort_id"]), "replay_evidence_integrity_failure"),
                )

    def replay_book_state(self, state_hash: str) -> dict[str, Any]:
        return self._replay_payload(
            table="normalized_book_states",
            hash_column="state_hash",
            payload_hash=state_hash,
        )

    def replay_fee_schedule(self, fee_hash: str) -> dict[str, Any]:
        return self._replay_payload(
            table="normalized_fee_schedules",
            hash_column="fee_hash",
            payload_hash=fee_hash,
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
        book_received_at: datetime | None = None,
        fee_request_started_at: datetime | None = None,
        fee_received_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically retain canonical evidence before a book is scored.

        Adapter timestamps are deliberately not copied into ``venue_timestamp``:
        public adapters currently provide no verified venue-origin timestamp.
        Every completed read has a sequence-ordered replay event before its
        caller can score it.  Unchanged evidence is a heartbeat; a state, fee,
        or phase transition is a change boundary.
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
        book_receipt_iso = _utc_iso(book_received_at) if book_received_at else None
        fee_request_iso = (
            _utc_iso(fee_request_started_at) if fee_request_started_at else None
        )
        fee_receipt_iso = _utc_iso(fee_received_at) if fee_received_at else None

        def latency_ms(start: datetime | None, end: datetime | None) -> int | None:
            if start is None or end is None:
                return None
            milliseconds = int((end - start).total_seconds() * 1000)
            if milliseconds < 0:
                raise ValueError("replay request receipt timing is not causal")
            return milliseconds

        book_latency_ms = latency_ms(
            request_started_at, book_received_at or received_at
        )
        fee_latency_ms = latency_ms(fee_request_started_at, fee_received_at)
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
            added_bytes = (0 if existing_book is not None else len(compressed_book)) + (
                0 if existing_fee is not None else len(compressed_fee)
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
                "SELECT state_hash, fee_hash, lock_phase "
                "FROM platform_replay_observation_events "
                "WHERE cohort_id = ? AND contract_id = ? ORDER BY sequence DESC LIMIT 1",
                (cohort_id, contract_id),
            ).fetchone()
            changed = prior is None or any(
                (
                    str(prior["state_hash"]) != state_hash,
                    str(prior["fee_hash"]) != fee_hash,
                    str(prior["lock_phase"]) != lock_phase,
                )
            )
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 "
                    "FROM platform_replay_observation_events WHERE cohort_id = ?",
                    (cohort_id,),
                ).fetchone()[0]
            )
            kind = "change" if changed else "heartbeat"
            connection.execute(
                "INSERT INTO platform_replay_observation_events "
                "(cohort_id, sequence, contract_id, kind, lock_phase, state_hash, fee_hash, "
                "request_started_at, received_at, book_received_at, book_latency_ms, "
                "fee_request_started_at, fee_received_at, fee_latency_ms, venue_timestamp, "
                "timestamp_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
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
                    book_receipt_iso,
                    book_latency_ms,
                    fee_request_iso,
                    fee_receipt_iso,
                    fee_latency_ms,
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
                "book_received_at": book_receipt_iso,
                "book_latency_ms": book_latency_ms,
                "fee_request_started_at": fee_request_iso,
                "fee_received_at": fee_receipt_iso,
                "fee_latency_ms": fee_latency_ms,
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
                "request_started_at, received_at, book_received_at, book_latency_ms, "
                "fee_request_started_at, fee_received_at, fee_latency_ms, venue_timestamp, "
                "timestamp_provenance "
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
        """Return active locks using the dashboard/store's mapping contract.

        Early v2 rows encoded the dataclass tuple as a JSON list of pairs.
        Normalize only that legacy representation at the persistence boundary so
        callers never need to guess whether ``selected_contract`` is a list or
        mapping.  The underlying stored payload remains readable during this
        deliberate migration path.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM political_event_locks WHERE locked_until >= ? "
                "ORDER BY occurrence_at, event_id",
                (_utc_iso(now),),
            ).fetchall()
        locks: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            selected_contract = payload.get("selected_contract")
            if isinstance(selected_contract, list) and all(
                isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
                for item in selected_contract
            ):
                payload["selected_contract"] = dict(selected_contract)
            locks.append(payload)
        return locks

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
