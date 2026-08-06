"""Fresh, pair-scoped market observations for cross-venue evaluation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from core.cross_platform_arb import MarketPair
from polymarket_client.models import OrderBook
from utils.http_resilience import CircuitOpenError


@dataclass(frozen=True)
class PairSnapshot:
    """One bounded observation containing both venue books for a verified pair."""

    pair_id: str
    polymarket_book: OrderBook
    kalshi_book: OrderBook
    max_age_seconds: float


class PairSnapshotError(RuntimeError):
    """A reason-coded failure to construct an admissible paired observation."""

    def __init__(self, reason_code: str, *, evidence: dict[str, Any] | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.evidence = evidence or {}


def _require_fresh_orderbook(
    venue: str,
    orderbook: OrderBook | None,
    observed_at: datetime,
    max_age_seconds: float,
) -> None:
    if orderbook is None:
        raise PairSnapshotError(f"missing_{venue}_orderbook")
    if not any(
        side.levels
        for token_book in (orderbook.yes, orderbook.no)
        for side in (token_book.bids, token_book.asks)
    ):
        raise PairSnapshotError(f"empty_{venue}_orderbook")
    timestamp = orderbook.timestamp
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise PairSnapshotError(f"naive_{venue}_orderbook_timestamp")
    age_seconds = (
        observed_at.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)
    ).total_seconds()
    if age_seconds < 0:
        raise PairSnapshotError(
            f"future_{venue}_orderbook_timestamp",
            evidence={"age_seconds": age_seconds},
        )
    if age_seconds > max_age_seconds:
        raise PairSnapshotError(
            f"stale_{venue}_orderbook",
            evidence={
                "age_seconds": age_seconds,
                "max_age_seconds": max_age_seconds,
            },
        )


def require_pair_snapshot_fresh(
    snapshot: PairSnapshot, *, observed_at: datetime | None = None
) -> None:
    """Revalidate a cached paired snapshot immediately before evaluation."""
    checked_at = observed_at or datetime.now(timezone.utc)
    _require_fresh_orderbook(
        "polymarket",
        snapshot.polymarket_book,
        checked_at,
        snapshot.max_age_seconds,
    )
    _require_fresh_orderbook(
        "kalshi",
        snapshot.kalshi_book,
        checked_at,
        snapshot.max_age_seconds,
    )


class PairSnapshotSource:
    """Fetch both venue books concurrently through one scanner-facing interface."""

    def __init__(
        self,
        polymarket_client: Any,
        kalshi_client: Any,
        *,
        max_age_seconds: float,
        timeout_seconds: float,
        clock: Any | None = None,
    ):
        if max_age_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("pair snapshot age and timeout must be positive")
        self._polymarket_client = polymarket_client
        self._kalshi_client = kalshi_client
        self._max_age_seconds = float(max_age_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def fetch(self, pair: MarketPair) -> PairSnapshot:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                polymarket_book, kalshi_book = await asyncio.gather(
                    self._polymarket_client.get_orderbook(pair.polymarket_id),
                    self._kalshi_client.get_orderbook_unified(pair.kalshi_ticker),
                )
        except TimeoutError as exc:
            raise PairSnapshotError(
                "paired_snapshot_timeout",
                evidence={"timeout_seconds": self._timeout_seconds},
            ) from exc
        except (CircuitOpenError, httpx.HTTPError, ConnectionError, OSError) as exc:
            raise PairSnapshotError(
                "paired_snapshot_upstream_unavailable",
                evidence={"error_type": type(exc).__name__},
            ) from exc
        except Exception as exc:
            reason_code = getattr(exc, "reason_code", None)
            evidence = getattr(exc, "evidence", None)
            if isinstance(reason_code, str) and isinstance(evidence, dict):
                raise PairSnapshotError(reason_code, evidence=evidence) from exc
            raise
        observed_at = self._clock()
        self._require_fresh("polymarket", polymarket_book, observed_at)
        self._require_fresh("kalshi", kalshi_book, observed_at)
        return PairSnapshot(
            pair_id=pair.pair_id,
            polymarket_book=polymarket_book,
            kalshi_book=kalshi_book,
            max_age_seconds=self._max_age_seconds,
        )

    def _require_fresh(
        self,
        venue: str,
        orderbook: OrderBook | None,
        observed_at: datetime,
    ) -> None:
        _require_fresh_orderbook(venue, orderbook, observed_at, self._max_age_seconds)


def executable_top_capacity(snapshot: PairSnapshot) -> float:
    """Return the best complete buy/sell direction's top-level capacity."""
    polymarket = snapshot.polymarket_book
    kalshi = snapshot.kalshi_book
    directions = (
        (polymarket.yes.asks.best_size, kalshi.yes.bids.best_size),
        (kalshi.yes.asks.best_size, polymarket.yes.bids.best_size),
        (polymarket.no.asks.best_size, kalshi.no.bids.best_size),
        (kalshi.no.asks.best_size, polymarket.no.bids.best_size),
    )
    return max(
        (
            min(float(buy_size), float(sell_size))
            for buy_size, sell_size in directions
            if buy_size is not None and sell_size is not None
        ),
        default=0.0,
    )
