"""Strict joins between archive candidates and externally evidenced event clocks.

An archive market's close, resolution, and end dates are all market lifecycle
metadata.  They must never become a proxy for when a political event occurred.
This module consequently accepts only a per-event ledger that names the exact
archive candidate and links it to a HTTPS primary/authoritative source.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

REQUIRED_LEDGER_FIELDS = ("event_id", "occurrence_at", "source_url", "source_name")


def _isoformat_utc(value: str) -> str:
    """Parse a ledger timestamp without importing optional application config."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("occurrence_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_entry(entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ValueError("occurrence ledger entries must be objects")
    missing = [field for field in REQUIRED_LEDGER_FIELDS if not str(entry.get(field, "")).strip()]
    if missing:
        raise ValueError("occurrence ledger entry missing " + ", ".join(missing))
    source_url = str(entry["source_url"]).strip()
    if urlparse(source_url).scheme != "https":
        raise ValueError("occurrence evidence source_url must use HTTPS")
    try:
        occurrence_at = _isoformat_utc(str(entry["occurrence_at"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("occurrence ledger entry has invalid occurrence_at") from exc
    return {
        "event_id": str(entry["event_id"]),
        "occurrence_at": occurrence_at,
        "source_url": source_url,
        "source_name": str(entry["source_name"]),
    }


def index_authoritative_occurrences(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Validate and index a ledger, rejecting ambiguous duplicate evidence."""
    indexed: dict[str, dict[str, str]] = {}
    for raw_entry in entries:
        entry = _validated_entry(raw_entry)
        if entry["event_id"] in indexed:
            raise ValueError("occurrence ledger has duplicate event_id " + entry["event_id"])
        indexed[entry["event_id"]] = entry
    return indexed


def attach_authoritative_occurrences(
    candidates: Iterable[dict[str, Any]], ledger_entries: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach only exact, externally evidenced event clocks to archive candidates.

    Unmatched candidates deliberately remain blocked.  This makes a partial
    source ledger safe to commit and keeps later price collection from silently
    treating a Gamma lifecycle date as an event occurrence.
    """
    ledger = index_authoritative_occurrences(ledger_entries)
    enriched: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = deepcopy(raw_candidate)
        event_id = str(candidate.get("event_id", ""))
        evidence = ledger.get(event_id)
        if evidence is not None:
            candidate["occurrence_at"] = evidence["occurrence_at"]
            candidate["occurrence_status"] = "verified_external_authority"
            candidate["occurrence_provenance"] = {
                "source_name": evidence["source_name"],
                "source_url": evidence["source_url"],
                "evidence_type": "exact_event_id_ledger",
            }
        enriched.append(candidate)
    return enriched
