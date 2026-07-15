"""
Historical market data normalization helpers.

The fetchers keep provider-specific payloads intact under `raw`, while exposing
common top-level fields that are easy to replay later.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


def parse_timestamp(value: str | int | float | datetime) -> datetime:
    """Parse ISO or Unix timestamps and normalize to UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        text = value.strip()
        if text.isdigit():
            dt = datetime.fromtimestamp(int(text), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_unix_seconds(value: str | int | float | datetime) -> int:
    """Convert supported timestamp inputs to Unix seconds."""
    return int(parse_timestamp(value).timestamp())


def isoformat_utc(value: str | int | float | datetime) -> str:
    """Render a timestamp as an ISO-8601 UTC string."""
    return parse_timestamp(value).isoformat().replace("+00:00", "Z")


def _float_or_none(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close_value(bucket: Any) -> Optional[float]:
    """Read close price from Kalshi live or archived candlestick shapes."""
    if not isinstance(bucket, dict):
        return None
    for key in ("close_dollars", "close"):
        value = _float_or_none(bucket.get(key))
        if value is not None:
            return value
    return None


def normalize_polymarket_history(
    *,
    market_id: str,
    token: str,
    token_id: str,
    history: Iterable[dict],
) -> list[dict]:
    """Convert Polymarket /prices-history points to common records."""
    records = []
    for point in history:
        timestamp = point.get("t")
        price = _float_or_none(point.get("p"))
        if timestamp is None or price is None:
            continue
        records.append({
            "timestamp": isoformat_utc(timestamp),
            "platform": "polymarket",
            "market_id": str(market_id),
            "token": token,
            "price": price,
            "bid": None,
            "ask": None,
            "volume": None,
            "source": "prices-history",
            "raw": {
                "token_id": str(token_id),
                "point": point,
            },
        })
    return records


def normalize_kalshi_candlesticks(
    *,
    market_id: str,
    candlesticks: Iterable[dict],
    source: str,
) -> list[dict]:
    """Convert Kalshi candlestick payloads to common records."""
    records = []
    for candle in candlesticks:
        timestamp = candle.get("end_period_ts")
        if timestamp is None:
            continue

        bid = _close_value(candle.get("yes_bid"))
        ask = _close_value(candle.get("yes_ask"))
        price = _close_value(candle.get("price"))
        if price is None:
            price = ask if ask is not None else bid

        records.append({
            "timestamp": isoformat_utc(timestamp),
            "platform": "kalshi",
            "market_id": market_id,
            "token": "YES",
            "price": price,
            "bid": bid,
            "ask": ask,
            "volume": _float_or_none(candle.get("volume_fp", candle.get("volume"))),
            "source": source,
            "raw": candle,
        })
    return records


def write_jsonl(path: str | Path, records: Iterable[dict], append: bool = False) -> int:
    """Write normalized records to JSONL and return the number written."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"

    count = 0
    with output_path.open(mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            count += 1
    return count
