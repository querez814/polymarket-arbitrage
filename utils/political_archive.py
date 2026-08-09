"""Bounded selection of closed Polymarket politics-archive study candidates.

The Gamma politics tag establishes archive membership, but its lifecycle dates
are not proof of the real-world occurrence time.  This module intentionally
does not infer an occurrence timestamp from a market's close or end date.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable


_FAMILY_TERMS = {
    "speech": ("speech", "address", "remarks", "debate"),
    "vote": ("vote", "votes", "ballot", "referendum", "primary"),
    "election": ("election", "elect", "wins", "win the"),
    "approval": ("approval", "approved", "confirm", "confirmation"),
}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _past_or_equal(value: Any, *, cutoff: datetime) -> bool:
    """Return whether Gamma lifecycle metadata is parseable and already past.

    This deliberately establishes *archive eligibility*, not a real-world
    event clock.  A future event may have a prematurely closed market, but it
    cannot enter a historical study candidate ledger.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.astimezone(timezone.utc) <= cutoff


def classify_event_family(title: str) -> str | None:
    """Return a deliberately narrow study family for a tagged archive event."""
    normalized = title.casefold()
    for family, terms in _FAMILY_TERMS.items():
        if any(term in normalized for term in terms):
            return family
    return None


def select_archive_candidates(
    events: Iterable[dict[str, Any]], *, max_events: int, archive_cutoff: datetime
) -> list[dict[str, Any]]:
    """Select at most one binary market per tagged event without inventing timing."""
    if max_events <= 0:
        raise ValueError("max_events must be positive")
    if archive_cutoff.tzinfo is None:
        raise ValueError("archive_cutoff must include a timezone")
    cutoff = archive_cutoff.astimezone(timezone.utc)
    candidates: list[dict[str, Any]] = []
    for event in events:
        # Event end/market close dates establish only that this is a genuinely
        # past archive record.  They must never be copied into occurrence_at.
        if not _past_or_equal(event.get("endDate"), cutoff=cutoff):
            continue
        title = str(event.get("title", ""))
        family = classify_event_family(title)
        if family is None:
            continue
        markets = event.get("markets")
        if not isinstance(markets, list):
            continue
        binary_markets = []
        for market in markets:
            if not isinstance(market, dict) or not market.get("closed"):
                continue
            if not _past_or_equal(market.get("closedTime"), cutoff=cutoff):
                continue
            if _string_list(market.get("outcomes")) != ["Yes", "No"]:
                continue
            token_ids = _string_list(market.get("clobTokenIds"))
            if len(token_ids) != 2 or not all(token_ids):
                continue
            binary_markets.append((float(market.get("volumeNum") or market.get("volume") or 0), market, token_ids))
        if not binary_markets:
            continue
        _, market, token_ids = max(binary_markets, key=lambda row: row[0])
        candidates.append({
            "event_id": "polymarket-event-" + str(event.get("id", "")),
            "family": family,
            "title": title,
            "market_id": str(market.get("id", "")),
            "market_question": str(market.get("question", "")),
            "yes_token_id": token_ids[0],
            "no_token_id": token_ids[1],
            "archive_provenance": {
                "source": "gamma-api.polymarket.com/events",
                "tag_id": 2,
                "event_end_date": event.get("endDate"),
                "market_closed_time": market.get("closedTime"),
                "archive_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            },
            "occurrence_at": None,
            "occurrence_status": "requires_authoritative_event_source",
        })
        if len(candidates) >= max_events:
            break
    return candidates
