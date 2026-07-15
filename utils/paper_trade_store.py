"""
Persistent SQLite ledger for paper trading lifecycle events.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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
        }


class PaperTradeStore:
    """SQLite-backed append-only paper trade event log."""

    def __init__(self, db_path: str = "data/paper_trades.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize_schema()

    def close(self) -> None:
        self._conn.close()

    def _initialize_schema(self) -> None:
        self._conn.executescript(
            """
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
                pnl_source TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_paper_trade_events_event_at
                ON paper_trade_events (event_at_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_trade_events_order_id
                ON paper_trade_events (order_id);
            CREATE INDEX IF NOT EXISTS idx_paper_trade_events_event_type
                ON paper_trade_events (event_type);
            """
        )
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(paper_trade_events)")
        }
        if "market_question" not in columns:
            self._conn.execute(
                "ALTER TABLE paper_trade_events ADD COLUMN market_question TEXT"
            )
        self._conn.commit()

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
        )

        try:
            cursor = self._conn.execute(
                """
                INSERT INTO paper_trade_events (
                    event_id, event_type, event_at_utc, order_id, trade_id, signal_id,
                    market_id, market_question, token_type, side, price, size, notional, fee,
                    strategy_tag, status, reason_code, reason_detail,
                    is_simulated, simulation_label, pnl_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            self._conn.commit()
            event.id = cursor.lastrowid
            return event
        except Exception as exc:
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
        )
