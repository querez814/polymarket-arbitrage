#!/usr/bin/env python3
"""Run the deterministic, non-mutating paper lifecycle acceptance proof."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.paper_acceptance import run_deterministic_paper_acceptance


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/paper_acceptance.db")
    args = parser.parse_args(argv)
    result = run_deterministic_paper_acceptance(args.db)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
