#!/usr/bin/env python3
"""Print a read-only evidence and PnL report for one paper run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paper_run_report import build_paper_run_report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/paper_performance.db")
    parser.add_argument("--run-id")
    parser.add_argument("--near-misses", type=int, default=10)
    parser.add_argument("--trades", type=int, default=50)
    args = parser.parse_args(argv)
    report = build_paper_run_report(
        args.db,
        run_id=args.run_id,
        near_miss_limit=args.near_misses,
        trade_limit=args.trades,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
