"""Durable, authenticated operator controls and external critical alerts."""

from __future__ import annotations

import hmac
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol

import httpx


class OperatorAuthenticationError(RuntimeError):
    """An operator mutation did not carry the configured bearer secret."""


class TradingHaltedError(RuntimeError):
    """Durable operator state currently forbids a new execution."""


class AlertDeliveryError(RuntimeError):
    """An operator transition could not be announced safely."""


class AdmissionLimitError(RuntimeError):
    """A durable order-attempt budget would be exceeded."""


class AlertSink(Protocol):
    async def deliver(self, event: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class OperatorStatus:
    halted: bool
    reason: str
    source: str
    updated_at: str
    last_alert_error: str


class WebhookAlertSink:
    """Deliver critical events to an HTTPS endpoint with bounded I/O."""

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not url.startswith("https://"):
            raise ValueError("alert webhook must use HTTPS")
        if timeout_seconds <= 0:
            raise ValueError("alert timeout must be positive")
        self._url = url
        self._bearer_token = bearer_token
        self._timeout = timeout_seconds

    async def deliver(self, event: dict[str, str]) -> None:
        headers = {"content-type": "application/json"}
        if self._bearer_token:
            headers["authorization"] = f"Bearer {self._bearer_token}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._url, headers=headers, json=event)
            response.raise_for_status()


class PersistentOperatorControls:
    """Persist panic/armed state; a new database always starts halted.

    Internal safety paths may trip without a token. Only an authenticated
    operator can clear the halt, and clearing it does not bypass runtime
    recovery/economics/private-stream gates.
    """

    def __init__(
        self,
        path: Path,
        *,
        auth_token: str,
        alert_sink: AlertSink | None = None,
    ) -> None:
        if len(auth_token) < 32:
            raise ValueError("operator auth token must contain at least 32 characters")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_destination()
        self._auth_token = auth_token
        self._alert_sink = alert_sink
        path_guard = self._open_path_guard()
        try:
            self._connection = sqlite3.connect(
                self.path, isolation_level=None, timeout=5.0
            )
            opened = self.path.stat()
            guarded = os.fstat(path_guard)
            if (opened.st_dev, opened.st_ino) != (guarded.st_dev, guarded.st_ino):
                self._connection.close()
                raise ValueError("operator state changed while opening")
        finally:
            os.close(path_guard)
        os.chmod(self.path, 0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS operator_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                halted INTEGER NOT NULL CHECK (halted IN (0, 1)),
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_alert_error TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS order_attempt_admissions (
                id INTEGER PRIMARY KEY,
                admitted_at TEXT NOT NULL
            ) STRICT;
            CREATE INDEX IF NOT EXISTS idx_order_attempt_admissions_at
                ON order_attempt_admissions(admitted_at);
            """
        )
        now = _utc_now()
        self._connection.execute(
            """
            INSERT OR IGNORE INTO operator_state
                (singleton, halted, reason, source, updated_at, last_alert_error)
            VALUES (1, 1, 'operator arming required', 'startup', ?, '')
            """,
            (now,),
        )
        integrity = self._connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            self.close()
            raise ValueError("operator control database failed integrity check")

    def _validate_destination(self) -> None:
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("operator state must be a regular file, not a symlink")

    def _open_path_guard(self) -> int:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise ValueError("operator state must be a regular file")
        os.fchmod(fd, 0o600)
        return fd

    def status(self) -> OperatorStatus:
        row = self._connection.execute(
            """
            SELECT halted, reason, source, updated_at, last_alert_error
            FROM operator_state WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("operator state is missing")
        return OperatorStatus(
            halted=bool(row["halted"]),
            reason=str(row["reason"]),
            source=str(row["source"]),
            updated_at=str(row["updated_at"]),
            last_alert_error=str(row["last_alert_error"]),
        )

    def require_armed(self) -> None:
        state = self.status()
        if state.halted:
            raise TradingHaltedError(f"trading is halted: {state.reason}")

    def authenticated_status(self, token: str) -> OperatorStatus:
        self._authenticate(token)
        return self.status()

    def reserve_order_attempts(
        self,
        count: int,
        *,
        max_per_minute: int,
        max_per_day: int,
    ) -> None:
        """Atomically reserve conservative order attempts across restarts."""
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (count, max_per_minute, max_per_day)
        ):
            raise ValueError("order-attempt limits must be positive integers")
        now = datetime.now(timezone.utc)
        minute_start = now.timestamp() - 60
        day_start = now.timestamp() - 86_400
        admitted_at = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self._connection.execute(
                "SELECT admitted_at FROM order_attempt_admissions"
            ).fetchall()
            timestamps = [
                datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).timestamp()
                for row in rows
            ]
            minute_count = sum(value >= minute_start for value in timestamps)
            day_count = sum(value >= day_start for value in timestamps)
            if minute_count + count > max_per_minute:
                raise AdmissionLimitError("per-minute order-attempt limit reached")
            if day_count + count > max_per_day:
                raise AdmissionLimitError("daily order-attempt limit reached")
            self._connection.executemany(
                "INSERT INTO order_attempt_admissions(admitted_at) VALUES (?)",
                [(admitted_at,)] * count,
            )
            self._connection.execute(
                "DELETE FROM order_attempt_admissions WHERE admitted_at < ?",
                (
                    datetime.fromtimestamp(day_start, timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                ),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    async def startup_halt(self) -> OperatorStatus:
        """Require explicit re-arming after every process start."""
        return await self._set_state(
            halted=True,
            reason="operator arming required after process start",
            source="startup",
            kind="startup_halted",
        )

    async def trip(self, *, reason: str, source: str) -> OperatorStatus:
        return await self._set_state(
            halted=True,
            reason=_bounded_text(reason, "reason"),
            source=_bounded_text(source, "source"),
            kind="panic",
        )

    async def panic(self, token: str, *, reason: str) -> OperatorStatus:
        self._authenticate(token)
        return await self.trip(reason=reason, source="operator")

    async def resume(self, token: str, *, reason: str) -> OperatorStatus:
        self._authenticate(token)
        return await self._set_state(
            halted=False,
            reason=_bounded_text(reason, "reason"),
            source="operator",
            kind="operator_resumed",
        )

    async def _set_state(
        self,
        *,
        halted: bool,
        reason: str,
        source: str,
        kind: str,
    ) -> OperatorStatus:
        updated_at = _utc_now()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                UPDATE operator_state
                SET halted = ?, reason = ?, source = ?, updated_at = ?, last_alert_error = ''
                WHERE singleton = 1
                """,
                (int(halted), reason, source, updated_at),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

        event = {
            "kind": kind,
            "reason": reason,
            "source": source,
            "occurred_at": updated_at,
        }
        if self._alert_sink is not None:
            try:
                await self._alert_sink.deliver(event)
            except Exception as exc:
                error = type(exc).__name__
                if halted:
                    self._connection.execute(
                        "UPDATE operator_state SET last_alert_error = ? WHERE singleton = 1",
                        (error,),
                    )
                else:
                    self._connection.execute(
                        """
                        UPDATE operator_state
                        SET halted = 1,
                            reason = 'operator resume alert delivery failed',
                            source = 'operator_controls',
                            updated_at = ?,
                            last_alert_error = ?
                        WHERE singleton = 1
                        """,
                        (_utc_now(), error),
                    )
                    raise AlertDeliveryError(
                        "operator resume alert delivery failed"
                    ) from exc
        return self.status()

    def _authenticate(self, token: str) -> None:
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode("utf-8"), self._auth_token.encode("utf-8")
        ):
            raise OperatorAuthenticationError("operator authentication failed")

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            self._connection = None  # type: ignore[assignment]
            connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _bounded_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > 1_024:
        raise ValueError(f"{field} is too long")
    return normalized
