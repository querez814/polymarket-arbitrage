"""
Persistent SQLite ledger for paper trading lifecycle events.
"""

from __future__ import annotations

import logging
import os
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import fcntl

from utils.time_utils import to_utc_iso, utc_now_iso

logger = logging.getLogger(__name__)

EVENT_TYPES = frozenset({"placed", "filled", "rejected", "cancelled", "expired"})


@dataclass
class PaperTradeEvent:
    """One persisted paper trading lifecycle event."""

    event_id: str
    event_type: str
    event_at_utc: str
    order_id: Optional[str] = None
    trade_id: Optional[str] = None
    signal_id: Optional[str] = None
    market_id: str = ""
    market_question: str = ""
    token_type: str = ""
    side: str = ""
    price: Optional[float] = None
    size: Optional[float] = None
    notional: Optional[float] = None
    fee: Optional[float] = None
    strategy_tag: str = ""
    status: str = ""
    reason_code: str = ""
    reason_detail: str = ""
    is_simulated: bool = True
    simulation_label: str = "hypothetical_paper"
    pnl_source: str = "hypothetical_paper"
    run_id: Optional[str] = None
    id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_at_utc": self.event_at_utc,
            "order_id": self.order_id,
            "trade_id": self.trade_id,
            "signal_id": self.signal_id,
            "market_id": self.market_id,
            "market_question": self.market_question,
            "token_type": self.token_type,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "notional": self.notional,
            "fee": self.fee,
            "strategy_tag": self.strategy_tag,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "is_simulated": self.is_simulated,
            "simulation_label": self.simulation_label,
            "pnl_source": self.pnl_source,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class PaperRunSession:
    """Persistent summary for one application run."""

    run_number: int
    run_id: str
    status: str
    started_at_utc: str
    last_heartbeat_at_utc: str
    ended_at_utc: Optional[str]
    elapsed_seconds: float
    starting_equity: float
    ending_equity: float
    pnl: float
    pnl_source: str
    transaction_count: int
    placed_count: int
    filled_count: int
    rejected_count: int
    cancelled_count: int
    expired_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_number": self.run_number,
            "run_id": self.run_id,
            "status": self.status,
            "started_at_utc": self.started_at_utc,
            "last_heartbeat_at_utc": self.last_heartbeat_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "elapsed_seconds": self.elapsed_seconds,
            "starting_equity": self.starting_equity,
            "ending_equity": self.ending_equity,
            "pnl": self.pnl,
            "pnl_source": self.pnl_source,
            "transaction_count": self.transaction_count,
            "placed_count": self.placed_count,
            "filled_count": self.filled_count,
            "rejected_count": self.rejected_count,
            "cancelled_count": self.cancelled_count,
            "expired_count": self.expired_count,
        }


class PaperTradeStore:
    """SQLite-backed append-only paper trade event log."""

    def __init__(self, db_path: str = "data/paper_trades.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = Path(f"{self.db_path}.run.lock")
        self._lock_file = self._lock_path.open("a+")
        os.chmod(self._lock_path, 0o600)
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise RuntimeError(
                f"paper run database already has an active process: {self.db_path}"
            ) from exc
        try:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._active_run_id: Optional[str] = None
            self._initialize_schema()
        except BaseException:
            connection = getattr(self, "_conn", None)
            if connection is not None:
                connection.close()
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def _initialize_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trade_events (
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
                pnl_source TEXT,
                run_id TEXT REFERENCES paper_run_sessions(run_id)
            );

            CREATE TABLE IF NOT EXISTS paper_run_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                last_heartbeat_at_utc TEXT NOT NULL,
                ended_at_utc TEXT,
                elapsed_seconds REAL NOT NULL DEFAULT 0,
                starting_equity REAL NOT NULL,
                ending_equity REAL NOT NULL,
                pnl REAL NOT NULL DEFAULT 0,
                pnl_source TEXT NOT NULL,
                transaction_count INTEGER NOT NULL DEFAULT 0,
                placed_count INTEGER NOT NULL DEFAULT 0,
                filled_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                cancelled_count INTEGER NOT NULL DEFAULT 0,
                expired_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS semantic_pair_reviews (
                pair_id TEXT PRIMARY KEY,
                polymarket_id TEXT NOT NULL,
                kalshi_ticker TEXT NOT NULL,
                polymarket_question TEXT NOT NULL,
                kalshi_title TEXT NOT NULL,
                relation TEXT NOT NULL,
                retrieval_score REAL NOT NULL,
                verification_confidence REAL NOT NULL,
                verification_reasons_json TEXT NOT NULL,
                approval_status TEXT NOT NULL,
                first_seen_at_utc TEXT NOT NULL,
                last_seen_at_utc TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS news_catalyst_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                headline TEXT NOT NULL,
                summary TEXT NOT NULL,
                source_url TEXT NOT NULL,
                topic_category TEXT NOT NULL,
                entities_json TEXT NOT NULL,
                published_at_utc TEXT NOT NULL,
                scanned_at_utc TEXT NOT NULL,
                UNIQUE(source_url, published_at_utc)
            );

            CREATE TABLE IF NOT EXISTS news_market_relevance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_event_id INTEGER NOT NULL
                    REFERENCES news_catalyst_events(id) ON DELETE CASCADE,
                market_platform TEXT NOT NULL
                    CHECK (market_platform IN ('polymarket', 'kalshi')),
                market_id TEXT NOT NULL,
                relevance_score REAL NOT NULL
                    CHECK (relevance_score >= 0 AND relevance_score <= 1),
                matched_at_utc TEXT NOT NULL,
                UNIQUE(
                    news_event_id, market_platform, market_id, matched_at_utc
                )
            );

            CREATE TABLE IF NOT EXISTS news_catalyst_api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                error_detail TEXT
            );

            CREATE TABLE IF NOT EXISTS cross_platform_evaluation_counts (
                run_id TEXT NOT NULL
                    REFERENCES paper_run_sessions(run_id) ON DELETE CASCADE,
                reason_code TEXT NOT NULL,
                observation_count INTEGER NOT NULL DEFAULT 0
                    CHECK (observation_count >= 0),
                first_observed_at_utc TEXT NOT NULL,
                last_observed_at_utc TEXT NOT NULL,
                PRIMARY KEY (run_id, reason_code)
            );

            CREATE INDEX IF NOT EXISTS idx_paper_trade_events_event_at
                ON paper_trade_events (event_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_trade_events_order_id
                ON paper_trade_events (order_id);
            CREATE INDEX IF NOT EXISTS idx_paper_trade_events_event_type
                ON paper_trade_events (event_type);
            CREATE INDEX IF NOT EXISTS idx_paper_run_sessions_started_at
                ON paper_run_sessions (started_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_semantic_pair_reviews_status
                ON semantic_pair_reviews (approval_status, verification_confidence DESC);
            CREATE INDEX IF NOT EXISTS idx_news_catalyst_events_scanned
                ON news_catalyst_events (scanned_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_news_market_relevance_market
                ON news_market_relevance (
                    market_platform, market_id, matched_at_utc DESC
                );
            CREATE INDEX IF NOT EXISTS idx_news_catalyst_api_calls_called
                ON news_catalyst_api_calls (called_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_cross_platform_evaluation_run
                ON cross_platform_evaluation_counts (run_id);
            """)
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(paper_trade_events)")
        }
        if "market_question" not in columns:
            self._conn.execute(
                "ALTER TABLE paper_trade_events ADD COLUMN market_question TEXT"
            )
        if "run_id" not in columns:
            self._conn.execute("ALTER TABLE paper_trade_events ADD COLUMN run_id TEXT")
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_paper_trade_events_run_id
            ON paper_trade_events (run_id)
            """)
        self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS paper_trade_events_run_id_insert
            BEFORE INSERT ON paper_trade_events
            WHEN NEW.run_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM paper_run_sessions WHERE run_id = NEW.run_id
              )
            BEGIN
                SELECT RAISE(ABORT, 'unknown paper run_id');
            END
            """)
        self._conn.commit()

    def reserve_news_api_call(
        self,
        *,
        max_daily_calls: int,
        called_at: Optional[datetime] = None,
    ) -> Optional[int]:
        """Atomically reserve one call against the durable UTC daily cap."""
        if max_daily_calls <= 0:
            raise ValueError("max_daily_calls must be positive")
        moment = self._as_utc(called_at)
        day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            count = self._conn.execute(
                """
                SELECT COUNT(*) FROM news_catalyst_api_calls
                WHERE called_at_utc >= ? AND called_at_utc < ?
                """,
                (to_utc_iso(day_start), to_utc_iso(day_end)),
            ).fetchone()[0]
            if int(count) >= max_daily_calls:
                self._conn.rollback()
                return None
            cursor = self._conn.execute(
                """
                INSERT INTO news_catalyst_api_calls (
                    called_at_utc, status, item_count
                ) VALUES (?, 'reserved', 0)
                """,
                (to_utc_iso(moment),),
            )
            self._conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("failed to reserve news API call")
            return int(cursor.lastrowid)
        except BaseException:
            self._conn.rollback()
            raise

    def reserve_news_api_calls(
        self,
        *,
        call_count: int,
        max_daily_calls: int,
        called_at: Optional[datetime] = None,
    ) -> list[int]:
        """Reserve a whole scan's worst-case API units or reserve none."""
        if call_count <= 0:
            raise ValueError("call_count must be positive")
        if max_daily_calls <= 0:
            raise ValueError("max_daily_calls must be positive")
        moment = self._as_utc(called_at)
        day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            count = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM news_catalyst_api_calls
                    WHERE called_at_utc >= ? AND called_at_utc < ?
                    """,
                    (to_utc_iso(day_start), to_utc_iso(day_end)),
                ).fetchone()[0]
            )
            if count + call_count > max_daily_calls:
                self._conn.rollback()
                return []
            call_ids: list[int] = []
            for _ in range(call_count):
                cursor = self._conn.execute(
                    """
                    INSERT INTO news_catalyst_api_calls (
                        called_at_utc, status, item_count
                    ) VALUES (?, 'reserved', 0)
                    """,
                    (to_utc_iso(moment),),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("failed to reserve news API call")
                call_ids.append(int(cursor.lastrowid))
            self._conn.commit()
            return call_ids
        except BaseException:
            self._conn.rollback()
            raise

    def complete_news_api_call(
        self,
        call_id: int,
        *,
        status: str,
        item_count: int = 0,
        error_detail: str = "",
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("news API call status must be succeeded or failed")
        if item_count < 0:
            raise ValueError("item_count must be non-negative")
        cursor = self._conn.execute(
            """
            UPDATE news_catalyst_api_calls
            SET status=?, item_count=?, error_detail=?
            WHERE id=? AND status='reserved'
            """,
            (status, item_count, error_detail[:1000] or None, call_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise ValueError("unknown or already completed news API call")
        self._conn.commit()

    def news_api_calls_today(self, *, at: Optional[datetime] = None) -> int:
        moment = self._as_utc(at)
        day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return int(
            self._conn.execute(
                """
                SELECT COUNT(*) FROM news_catalyst_api_calls
                WHERE called_at_utc >= ? AND called_at_utc < ?
                """,
                (to_utc_iso(day_start), to_utc_iso(day_end)),
            ).fetchone()[0]
        )

    def record_news_catalysts(
        self,
        items: list[Any],
        matches: list[Any],
        *,
        scanned_at: Optional[datetime] = None,
    ) -> None:
        """Persist source-backed events and their all-market relevance scores."""
        moment = self._as_utc(scanned_at)
        scanned_iso = to_utc_iso(moment)
        event_ids: list[int] = []
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for item in items:
                published = self._as_utc(item.published_at)
                source_url = str(item.source_url)
                self._conn.execute(
                    """
                    INSERT INTO news_catalyst_events (
                        headline, summary, source_url, topic_category,
                        entities_json, published_at_utc, scanned_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_url, published_at_utc) DO UPDATE SET
                        headline=excluded.headline,
                        summary=excluded.summary,
                        topic_category=excluded.topic_category,
                        entities_json=excluded.entities_json,
                        scanned_at_utc=excluded.scanned_at_utc
                    """,
                    (
                        item.headline,
                        item.summary,
                        source_url,
                        item.topic_category,
                        json.dumps(list(item.entities), separators=(",", ":")),
                        to_utc_iso(published),
                        scanned_iso,
                    ),
                )
                row = self._conn.execute(
                    """
                    SELECT id FROM news_catalyst_events
                    WHERE source_url=? AND published_at_utc=?
                    """,
                    (source_url, to_utc_iso(published)),
                ).fetchone()
                event_ids.append(int(row[0]))
            for match in matches:
                if match.news_index < 0 or match.news_index >= len(event_ids):
                    raise ValueError("news match references an unknown item")
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO news_market_relevance (
                        news_event_id, market_platform, market_id,
                        relevance_score, matched_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event_ids[match.news_index],
                        match.market_platform,
                        match.market_id,
                        float(match.relevance_score),
                        scanned_iso,
                    ),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def recent_news_catalysts(self, limit: int = 40) -> list[dict[str, Any]]:
        event_rows = self._conn.execute(
            """
            SELECT * FROM news_catalyst_events
            ORDER BY scanned_at_utc DESC, published_at_utc DESC
            LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for event_row in event_rows:
            event = dict(event_row)
            event["entities"] = json.loads(event.pop("entities_json"))
            event["matches"] = [
                dict(row)
                for row in self._conn.execute(
                    """
                    SELECT market_platform, market_id, relevance_score,
                           matched_at_utc
                    FROM news_market_relevance
                    WHERE news_event_id=?
                    ORDER BY relevance_score DESC, market_platform, market_id
                    """,
                    (event["id"],),
                ).fetchall()
            ]
            results.append(event)
        return results

    def record_pair_review(
        self,
        *,
        pair_id: str,
        polymarket_id: str,
        kalshi_ticker: str,
        polymarket_question: str,
        kalshi_title: str,
        relation: str,
        retrieval_score: float,
        verification_confidence: float,
        verification_reasons: tuple[str, ...],
        approval_status: str,
    ) -> None:
        """Upsert one semantic verification result into the operator review queue."""
        if relation not in {
            "equivalent",
            "subset",
            "superset",
            "independent",
            "unverified",
        }:
            raise ValueError("unknown semantic relation")
        if approval_status not in {"auto_approved", "manual_review", "rejected"}:
            raise ValueError("unknown semantic approval status")
        if not 0 <= retrieval_score <= 1 or not 0 <= verification_confidence <= 1:
            raise ValueError("semantic scores must be in [0, 1]")
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO semantic_pair_reviews (
                pair_id, polymarket_id, kalshi_ticker, polymarket_question,
                kalshi_title, relation, retrieval_score, verification_confidence,
                verification_reasons_json, approval_status, first_seen_at_utc,
                last_seen_at_utc, seen_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(pair_id) DO UPDATE SET
                polymarket_id=excluded.polymarket_id,
                kalshi_ticker=excluded.kalshi_ticker,
                polymarket_question=excluded.polymarket_question,
                kalshi_title=excluded.kalshi_title,
                relation=excluded.relation,
                retrieval_score=excluded.retrieval_score,
                verification_confidence=excluded.verification_confidence,
                verification_reasons_json=excluded.verification_reasons_json,
                approval_status=excluded.approval_status,
                last_seen_at_utc=excluded.last_seen_at_utc,
                seen_count=semantic_pair_reviews.seen_count + 1
            """,
            (
                pair_id,
                polymarket_id,
                kalshi_ticker,
                polymarket_question,
                kalshi_title,
                relation,
                retrieval_score,
                verification_confidence,
                json.dumps(list(verification_reasons), separators=(",", ":")),
                approval_status,
                now,
                now,
            ),
        )
        self._conn.commit()

    def recent_pair_reviews(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM semantic_pair_reviews
            ORDER BY last_seen_at_utc DESC, verification_confidence DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            result["verification_reasons"] = json.loads(
                result.pop("verification_reasons_json")
            )
            results.append(result)
        return results

    def record_cross_platform_evaluation_counts(
        self,
        counts: dict[str, int],
    ) -> None:
        """Atomically add one scanner cycle's compact reason-code counts."""
        if not counts:
            return
        run_id = self._active_run_id
        if run_id is None:
            raise RuntimeError("cross-platform evaluation requires an active paper run")
        normalized: dict[str, int] = {}
        for reason_code, count in counts.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason_code):
                raise ValueError("invalid cross-platform evaluation reason code")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("cross-platform evaluation counts must be nonnegative integers")
            if count:
                normalized[reason_code] = count
        if not normalized:
            return
        now = utc_now_iso()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for reason_code, count in normalized.items():
                self._conn.execute(
                    """
                    INSERT INTO cross_platform_evaluation_counts (
                        run_id, reason_code, observation_count,
                        first_observed_at_utc, last_observed_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, reason_code) DO UPDATE SET
                        observation_count=(
                            cross_platform_evaluation_counts.observation_count
                            + excluded.observation_count
                        ),
                        last_observed_at_utc=excluded.last_observed_at_utc
                    """,
                    (run_id, reason_code, count, now, now),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def cross_platform_evaluation_funnel(
        self,
        run_id: Optional[str] = None,
    ) -> dict[str, int]:
        """Return persisted scanner outcome counts for one paper run."""
        selected_run_id = run_id or self._active_run_id
        if selected_run_id is None:
            row = self._conn.execute(
                "SELECT run_id FROM paper_run_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {}
            selected_run_id = str(row["run_id"])
        rows = self._conn.execute(
            """
            SELECT reason_code, observation_count
            FROM cross_platform_evaluation_counts
            WHERE run_id = ?
            ORDER BY reason_code
            """,
            (selected_run_id,),
        ).fetchall()
        return {
            str(row["reason_code"]): int(row["observation_count"])
            for row in rows
        }

    @staticmethod
    def _as_utc(value: Optional[datetime]) -> datetime:
        moment = value or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("run session timestamps must be timezone-aware")
        return moment.astimezone(timezone.utc)

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    def start_run(
        self,
        *,
        starting_equity: float,
        pnl_source: str,
        started_at: Optional[datetime] = None,
    ) -> PaperRunSession:
        """Start a numbered run and mark any unclosed predecessor interrupted."""
        if starting_equity < 0:
            raise ValueError("starting_equity must be non-negative")
        if not pnl_source.strip():
            raise ValueError("pnl_source must be non-empty")
        started = self._as_utc(started_at)
        started_iso = to_utc_iso(started)
        run_id = f"run_{uuid.uuid4().hex[:20]}"
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("""
                UPDATE paper_run_sessions
                SET status = 'interrupted', ended_at_utc = last_heartbeat_at_utc
                WHERE status = 'active'
                """)
            self._conn.execute(
                """
                INSERT INTO paper_run_sessions (
                    run_id, status, started_at_utc, last_heartbeat_at_utc,
                    starting_equity, ending_equity, pnl, pnl_source
                ) VALUES (?, 'active', ?, ?, ?, ?, 0, ?)
                """,
                (
                    run_id,
                    started_iso,
                    started_iso,
                    starting_equity,
                    starting_equity,
                    pnl_source.strip(),
                ),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._active_run_id = run_id
        run = self.active_run()
        if run is None:
            raise RuntimeError("failed to load newly created paper run")
        return run

    def checkpoint_run(
        self,
        *,
        current_equity: Optional[float] = None,
        pnl: Optional[float] = None,
        checkpoint_at: Optional[datetime] = None,
    ) -> Optional[PaperRunSession]:
        """Persist timer and current performance for the active run."""
        if self._active_run_id is None:
            return None
        checkpoint = self._as_utc(checkpoint_at)
        row = self._conn.execute(
            "SELECT started_at_utc FROM paper_run_sessions WHERE run_id = ? AND status = 'active'",
            (self._active_run_id,),
        ).fetchone()
        if row is None:
            self._active_run_id = None
            return None
        elapsed = max(
            0.0,
            (checkpoint - self._parse_utc(row["started_at_utc"])).total_seconds(),
        )
        assignments = [
            "last_heartbeat_at_utc = ?",
            "elapsed_seconds = ?",
        ]
        params: list[Any] = [to_utc_iso(checkpoint), elapsed]
        if current_equity is not None:
            assignments.append("ending_equity = ?")
            params.append(float(current_equity))
        if pnl is not None:
            assignments.append("pnl = ?")
            params.append(float(pnl))
        params.append(self._active_run_id)
        self._conn.execute(
            f"UPDATE paper_run_sessions SET {', '.join(assignments)} WHERE run_id = ?",
            params,
        )
        self._conn.commit()
        return self.active_run()

    def finish_run(
        self,
        *,
        ending_equity: float,
        pnl: float,
        ended_at: Optional[datetime] = None,
        status: str = "completed",
    ) -> PaperRunSession:
        """Close the active run with a final timer and performance snapshot."""
        if status not in {"completed", "failed"}:
            raise ValueError("finished run status must be completed or failed")
        active = self.active_run()
        if active is None or self._active_run_id is None:
            raise RuntimeError("no active paper run")
        ended = self._as_utc(ended_at)
        elapsed = max(
            0.0,
            (ended - self._parse_utc(active.started_at_utc)).total_seconds(),
        )
        run_id = self._active_run_id
        self._conn.execute(
            """
            UPDATE paper_run_sessions
            SET status = ?, last_heartbeat_at_utc = ?, ended_at_utc = ?,
                elapsed_seconds = ?, ending_equity = ?, pnl = ?
            WHERE run_id = ? AND status = 'active'
            """,
            (
                status,
                to_utc_iso(ended),
                to_utc_iso(ended),
                elapsed,
                float(ending_equity),
                float(pnl),
                run_id,
            ),
        )
        self._conn.commit()
        self._active_run_id = None
        row = self._conn.execute(
            "SELECT * FROM paper_run_sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("finished paper run disappeared")
        return self._row_to_run(row)

    def active_run(self) -> Optional[PaperRunSession]:
        if self._active_run_id is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM paper_run_sessions WHERE run_id = ? AND status = 'active'",
            (self._active_run_id,),
        ).fetchone()
        return self._row_to_run(row) if row else None

    def recent_runs(self, limit: int = 50) -> list[PaperRunSession]:
        rows = self._conn.execute(
            "SELECT * FROM paper_run_sessions ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def record_event(
        self,
        *,
        event_type: str,
        event_at: Optional[datetime] = None,
        event_id: Optional[str] = None,
        order_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        signal_id: Optional[str] = None,
        market_id: str = "",
        market_question: str = "",
        token_type: str = "",
        side: str = "",
        price: Optional[float] = None,
        size: Optional[float] = None,
        notional: Optional[float] = None,
        fee: Optional[float] = None,
        strategy_tag: str = "",
        status: str = "",
        reason_code: str = "",
        reason_detail: str = "",
        is_simulated: bool = True,
        simulation_label: str = "hypothetical_paper",
        pnl_source: str = "hypothetical_paper",
        run_equity: Optional[float] = None,
        run_pnl: Optional[float] = None,
    ) -> Optional[PaperTradeEvent]:
        """Persist one paper trade event. Best-effort; never raises to callers."""
        if event_type not in EVENT_TYPES:
            logger.warning("Ignoring unknown paper trade event_type=%s", event_type)
            return None

        event = PaperTradeEvent(
            id=None,
            event_id=event_id or f"evt_{uuid.uuid4().hex[:16]}",
            event_type=event_type,
            event_at_utc=to_utc_iso(event_at) if event_at else utc_now_iso(),
            order_id=order_id,
            trade_id=trade_id,
            signal_id=signal_id,
            market_id=market_id,
            market_question=market_question,
            token_type=token_type,
            side=side,
            price=price,
            size=size,
            notional=notional,
            fee=fee,
            strategy_tag=strategy_tag,
            status=status,
            reason_code=reason_code,
            reason_detail=reason_detail,
            is_simulated=is_simulated,
            simulation_label=simulation_label,
            pnl_source=pnl_source,
            run_id=self._active_run_id,
        )

        try:
            cursor = self._conn.execute(
                """
                INSERT INTO paper_trade_events (
                    event_id, event_type, event_at_utc, order_id, trade_id, signal_id,
                    market_id, market_question, token_type, side, price, size, notional, fee,
                    strategy_tag, status, reason_code, reason_detail,
                    is_simulated, simulation_label, pnl_source, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.event_at_utc,
                    event.order_id,
                    event.trade_id,
                    event.signal_id,
                    event.market_id,
                    event.market_question,
                    event.token_type,
                    event.side,
                    event.price,
                    event.size,
                    event.notional,
                    event.fee,
                    event.strategy_tag,
                    event.status,
                    event.reason_code,
                    event.reason_detail,
                    int(event.is_simulated),
                    event.simulation_label,
                    event.pnl_source,
                    event.run_id,
                ),
            )
            if event.run_id:
                counter_column = {
                    "placed": "placed_count",
                    "filled": "filled_count",
                    "rejected": "rejected_count",
                    "cancelled": "cancelled_count",
                    "expired": "expired_count",
                }[event.event_type]
                transaction_increment = 1 if event.event_type == "filled" else 0
                self._conn.execute(
                    f"""
                    UPDATE paper_run_sessions
                    SET {counter_column} = {counter_column} + 1,
                        transaction_count = transaction_count + ?
                    WHERE run_id = ? AND status = 'active'
                    """,
                    (transaction_increment, event.run_id),
                )
                if self._conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise sqlite3.IntegrityError(
                        "paper event run counter did not update an active run"
                    )
                if run_equity is not None or run_pnl is not None:
                    assignments = []
                    values: list[Any] = []
                    if run_equity is not None:
                        assignments.append("ending_equity = ?")
                        values.append(float(run_equity))
                    if run_pnl is not None:
                        assignments.append("pnl = ?")
                        values.append(float(run_pnl))
                    values.append(event.run_id)
                    self._conn.execute(
                        f"UPDATE paper_run_sessions SET {', '.join(assignments)} WHERE run_id = ? AND status = 'active'",
                        values,
                    )
            self._conn.commit()
            event.id = cursor.lastrowid
            return event
        except Exception as exc:
            self._conn.rollback()
            logger.warning("Failed to record paper trade event: %s", exc)
            return None

    def recent_events(
        self,
        limit: int = 200,
        event_type: Optional[str] = None,
    ) -> list[PaperTradeEvent]:
        if event_type and event_type not in EVENT_TYPES:
            return []

        query = "SELECT * FROM paper_trade_events"
        params: list[Any] = []
        if event_type:
            query += " WHERE event_type = ?"
            params.append(event_type)
        query += " ORDER BY event_at_utc DESC, id DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def events_for_order(self, order_id: str) -> list[PaperTradeEvent]:
        rows = self._conn.execute(
            """
            SELECT * FROM paper_trade_events
            WHERE order_id = ?
            ORDER BY event_at_utc ASC, id ASC
            """,
            (order_id,),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> PaperTradeEvent:
        return PaperTradeEvent(
            id=row["id"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            event_at_utc=row["event_at_utc"],
            order_id=row["order_id"],
            trade_id=row["trade_id"],
            signal_id=row["signal_id"],
            market_id=row["market_id"] or "",
            market_question=row["market_question"] or "",
            token_type=row["token_type"] or "",
            side=row["side"] or "",
            price=row["price"],
            size=row["size"],
            notional=row["notional"],
            fee=row["fee"],
            strategy_tag=row["strategy_tag"] or "",
            status=row["status"] or "",
            reason_code=row["reason_code"] or "",
            reason_detail=row["reason_detail"] or "",
            is_simulated=bool(row["is_simulated"]),
            simulation_label=row["simulation_label"] or "",
            pnl_source=row["pnl_source"] or "",
            run_id=row["run_id"],
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> PaperRunSession:
        elapsed_seconds = float(row["elapsed_seconds"])
        if row["status"] == "active":
            started = PaperTradeStore._parse_utc(row["started_at_utc"])
            elapsed_seconds = max(
                elapsed_seconds,
                (datetime.now(timezone.utc) - started).total_seconds(),
            )
        return PaperRunSession(
            run_number=row["id"],
            run_id=row["run_id"],
            status=row["status"],
            started_at_utc=row["started_at_utc"],
            last_heartbeat_at_utc=row["last_heartbeat_at_utc"],
            ended_at_utc=row["ended_at_utc"],
            elapsed_seconds=elapsed_seconds,
            starting_equity=float(row["starting_equity"]),
            ending_equity=float(row["ending_equity"]),
            pnl=float(row["pnl"]),
            pnl_source=row["pnl_source"],
            transaction_count=int(row["transaction_count"]),
            placed_count=int(row["placed_count"]),
            filled_count=int(row["filled_count"]),
            rejected_count=int(row["rejected_count"]),
            cancelled_count=int(row["cancelled_count"]),
            expired_count=int(row["expired_count"]),
        )
