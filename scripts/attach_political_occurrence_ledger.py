#!/usr/bin/env python3
"""Attach externally sourced occurrence evidence to political archive candidates.

The ledger is a JSON object with an ``occurrences`` array.  Each row must use
the exact candidate ``event_id`` plus ``occurrence_at``, ``source_url`` and
``source_name``.  This command does not download prices and does not infer an
event clock from Gamma dates.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS_ROOT = ROOT / "utils"
if str(UTILS_ROOT) not in sys.path:
    sys.path.insert(0, str(UTILS_ROOT))

from political_occurrence_ledger import attach_authoritative_occurrences


def _object_with_list(path: Path, key: str) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ValueError(f"{path} must contain a {key!r} list")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--occurrence-ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    candidate_payload = json.loads(args.candidates.read_text(encoding="utf-8"))
    candidates = candidate_payload.get("candidates") if isinstance(candidate_payload, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("candidate file must contain a candidates list")
    enriched = attach_authoritative_occurrences(
        candidates, _object_with_list(args.occurrence_ledger, "occurrences")
    )
    verified_count = sum(item.get("occurrence_status") == "verified_external_authority" for item in enriched)
    output = {
        "data_quality": {
            "classification": "archive_candidates_with_external_occurrence_ledger",
            "occurrence_times_verified": verified_count == len(enriched),
            "verified_event_count": verified_count,
            "blocked_event_count": len(enriched) - verified_count,
            "blocker": "Only verified_external_authority rows may enter a price-window study.",
        },
        "source_candidates": str(args.candidates),
        "source_occurrence_ledger": str(args.occurrence_ledger),
        "candidates": enriched,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {verified_count}/{len(enriched)} occurrence-verified candidates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
