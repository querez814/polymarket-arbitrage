"""
Time utilities for consistent UTC storage and ISO serialization.
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current time as timezone-aware UTC."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 with Z suffix."""
    return to_utc_iso(utc_now())


def to_utc_iso(value: datetime) -> str:
    """Convert a datetime to ISO-8601 UTC with Z suffix."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    text = value.isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    if text.endswith("Z"):
        return text
    return text + "Z"
