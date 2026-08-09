#!/usr/bin/env python3
"""Collect a bounded closed-politics archive candidate ledger from Gamma.

This writes candidates, not a backtest manifest: occurrence timestamps require
an explicit authoritative source and must never be copied from settlement or
market lifecycle dates.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep this archive-only command usable in an evidence checkout that lacks the
# application's optional YAML configuration dependency.  The selector itself
# is intentionally standard-library-only.
from utils.political_archive import select_archive_candidates

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def _fetch_page(*, offset: int, limit: int) -> list[dict]:
    query = urlencode(
        {
            "closed": "true",
            "tag_id": "2",
            "limit": str(limit),
            "offset": str(offset),
            "order": "endDate",
            "ascending": "false",
        }
    )
    request = Request(
        f"{GAMMA_EVENTS_URL}?{query}",
        headers={"User-Agent": "nightwatch-political-archive-research/1.0"},
    )
    with urlopen(request, timeout=30) as response:  # nosec B310: fixed HTTPS host
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError("Gamma events response must be a list")
    return [item for item in payload if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-events", type=int, default=40)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.page_size <= 100 or args.max_events <= 0 or args.max_pages <= 0:
        parser.error(
            "max-events/max-pages must be positive and page-size must be 1..100"
        )

    collected: list[dict] = []
    for page in range(args.max_pages):
        records = _fetch_page(offset=page * args.page_size, limit=args.page_size)
        collected.extend(records)
        if len(records) < args.page_size:
            break
    collection_cutoff = datetime.now(timezone.utc)
    candidates = select_archive_candidates(
        collected, max_events=args.max_events, archive_cutoff=collection_cutoff
    )
    payload = {
        "data_quality": {
            "classification": "archive_candidate_ledger",
            "occurrence_times_verified": False,
            "blocker": "Gamma archive lifecycle dates are not authoritative real-world occurrence times.",
        },
        "collection": {
            "source": GAMMA_EVENTS_URL,
            "tag_id": 2,
            "pages_fetched": page + 1,
            "events_fetched": len(collected),
            "archive_cutoff": collection_cutoff.isoformat().replace("+00:00", "Z"),
        },
        "candidates": candidates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(candidates)} bounded archive candidates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
