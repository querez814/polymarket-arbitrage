#!/usr/bin/env python3
"""Evaluate one manually verified pair using public books; never place orders."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Sequence

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.live_pair_evaluation import (
    PairApprovalRequired,
    canonical_live_pair,
    evaluate_live_pair_readonly,
)
from core.pair_snapshot import PairSnapshotError
from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient


async def _run(args: argparse.Namespace) -> dict:
    # Both clients are hard-pinned to dry-run. This command imports no execution runtime.
    async with PolymarketClient(dry_run=True) as polymarket, KalshiClient(
        dry_run=True
    ) as kalshi:
        polymarket_market, kalshi_market = await asyncio.gather(
            polymarket.get_market(args.polymarket_id),
            kalshi.get_market(args.kalshi_ticker),
        )
        if kalshi_market is None:
            raise ValueError("Kalshi market was not found")
        if (
            args.polymarket_condition_id
            and polymarket_market.condition_id != args.polymarket_condition_id
        ):
            raise ValueError("Polymarket condition ID does not match market metadata")
        pair, canonical, pair_hash = canonical_live_pair(
            polymarket_market,
            kalshi_market,
            approved_pair_hash=args.approve_pair_hash,
        )
        result = await evaluate_live_pair_readonly(
            pair,
            polymarket,
            kalshi,
            max_age_seconds=args.max_age_seconds,
            timeout_seconds=args.timeout_seconds,
            min_edge=args.min_edge,
            slippage_reserve_per_contract=args.slippage_reserve_per_contract,
        )
        result["canonical_pair"] = canonical
        result["approved_pair_hash"] = pair_hash
        return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--polymarket-id", required=True)
    parser.add_argument("--polymarket-condition-id")
    parser.add_argument("--kalshi-ticker", required=True)
    parser.add_argument("--approve-pair-hash")
    parser.add_argument("--max-age-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--min-edge", type=float, default=0.02)
    parser.add_argument("--slippage-reserve-per-contract", type=float, default=0.02)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except PairApprovalRequired as exc:
        print(
            json.dumps(
                {
                    "status": "approval_required",
                    "canonical_pair": exc.canonical_pair,
                    "approve_pair_hash": exc.pair_hash,
                    "venue_mutations": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    except PairSnapshotError as exc:
        print(
            json.dumps(
                {
                    "status": "unusable_snapshot",
                    "reason_code": exc.reason_code,
                    "evidence": exc.evidence,
                    "venue_mutations": 0,
                },
                sort_keys=True,
            )
        )
        return 2
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": str(exc),
                    "venue_mutations": 0,
                },
                sort_keys=True,
            )
        )
        return 2
    except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "venue_mutations": 0,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
