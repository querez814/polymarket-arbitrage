"""Bounded selection of closed Polymarket politics-archive study candidates.

The Gamma politics tag establishes archive membership, but its lifecycle dates
are not proof of the real-world occurrence time.  This module intentionally
does not infer an occurrence timestamp from a market's close or end date.
"""

from __future__ import annotations

import json
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


def classify_event_family(title: str) -> str | None:
    """Return a deliberately narrow study family for a tagged archive event."""
    normalized = title.casefold()
    for family, terms in _FAMILY_TERMS.items():
        if any(term in normalized for term in terms):
            return family
    return None


def select_archive_candidates(events: Iterable[dict[str, Any]], *, max_events: int) -> list[dict[str, Any]]:
    """Select at most one binary market per tagged event without inventing timing."""
    if max_events <= 0:
        raise ValueError("max_events must be positive")
    candidates: list[dict[str, Any]] = []
    for event in events:
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
            },
            "occurrence_at": None,
            "occurrence_status": "requires_authoritative_event_source",
        })
        if len(candidates) >= max_events:
            break
    return candidates
