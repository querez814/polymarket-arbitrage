#!/usr/bin/env python3
"""
Replay normalized historical data through the cross-platform arbitrage engine.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.cross_platform_backtest import (
    ReplayOpportunity,
    load_historical_records,
    make_all_pairs,
    run_cross_platform_replay,
)


def _parse_pair(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("pairs must be POLYMARKET_ID:KALSHI_TICKER")
    poly_id, kalshi_ticker = value.split(":", 1)
    if not poly_id or not kalshi_ticker:
        raise argparse.ArgumentTypeError("pairs must be POLYMARKET_ID:KALSHI_TICKER")
    if poly_id == "POLYMARKET_ID" or kalshi_ticker == "KALSHI_TICKER":
        raise argparse.ArgumentTypeError(
            "replace POLYMARKET_ID:KALSHI_TICKER with a real matched pair, "
            "for example 123456:KXEXAMPLE-..."
        )
    return poly_id, kalshi_ticker


def _read_pairs_file(path: str | Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pairs_path = Path(path)
    with pairs_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            if "," in value:
                poly_id, kalshi_ticker = [part.strip() for part in value.split(",", 1)]
                value = f"{poly_id}:{kalshi_ticker}"
            try:
                pairs.append(_parse_pair(value))
            except argparse.ArgumentTypeError as exc:
                raise SystemExit(f"{pairs_path}:{line_number}: {exc}") from exc
    return pairs


def _write_opportunities_csv(path: str | Path, opportunities: list[ReplayOpportunity]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp",
                "polymarket_id",
                "kalshi_ticker",
                "token",
                "buy_platform",
                "sell_platform",
                "buy_price",
                "sell_price",
                "gross_edge",
                "net_edge",
                "edge_pct",
                "max_size",
            ],
        )
        writer.writeheader()
        for opportunity in opportunities:
            writer.writerow({
                "timestamp": opportunity.timestamp.isoformat().replace("+00:00", "Z"),
                "polymarket_id": opportunity.polymarket_id,
                "kalshi_ticker": opportunity.kalshi_ticker,
                "token": opportunity.token,
                "buy_platform": opportunity.buy_platform,
                "sell_platform": opportunity.sell_platform,
                "buy_price": opportunity.buy_price,
                "sell_price": opportunity.sell_price,
                "gross_edge": opportunity.gross_edge,
                "net_edge": opportunity.net_edge,
                "edge_pct": opportunity.edge_pct,
                "max_size": opportunity.max_size,
            })


def _record_range(records) -> tuple[object, object] | tuple[None, None]:
    if not records:
        return None, None
    timestamps = [record.timestamp for record in records]
    return min(timestamps), max(timestamps)


def _ranges_overlap(poly_records, kalshi_records, max_delta: timedelta) -> bool:
    poly_start, poly_end = _record_range(poly_records)
    kalshi_start, kalshi_end = _record_range(kalshi_records)
    if not all([poly_start, poly_end, kalshi_start, kalshi_end]):
        return False
    return poly_start <= kalshi_end + max_delta and kalshi_start <= poly_end + max_delta


def _format_range(records) -> str:
    start, end = _record_range(records)
    if not start or not end:
        return "empty"
    return f"{start.isoformat().replace('+00:00', 'Z')} -> {end.isoformat().replace('+00:00', 'Z')}"


def _count_missing_quotes(records) -> int:
    return sum(1 for record in records if record.bid is None or record.ask is None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest cross-platform arbitrage on normalized historical JSONL files."
    )
    parser.add_argument("--polymarket-file", required=True, help="Polymarket historical JSONL file.")
    parser.add_argument("--kalshi-file", required=True, help="Kalshi historical JSONL file.")
    parser.add_argument(
        "--pair",
        action="append",
        type=_parse_pair,
        default=[],
        help="Matched pair as POLYMARKET_ID:KALSHI_TICKER. Can be repeated.",
    )
    parser.add_argument(
        "--pairs-file",
        help=(
            "File of matched pairs, one per line. Supports POLYMARKET_ID:KALSHI_TICKER "
            "or POLYMARKET_ID,KALSHI_TICKER."
        ),
    )
    parser.add_argument(
        "--all-pairs",
        action="store_true",
        help=(
            "Mechanical smoke test only: evaluate every Polymarket market against every "
            "Kalshi market in the files, even when questions are unrelated."
        ),
    )
    parser.add_argument("--min-edge", type=float, default=0.01, help="Minimum net edge to count as an opportunity.")
    parser.add_argument(
        "--max-time-delta-minutes",
        type=float,
        default=30.0,
        help="Maximum timestamp gap allowed when aligning snapshots.",
    )
    parser.add_argument(
        "--assumed-spread",
        type=float,
        default=0.02,
        help="Absolute spread used when records only have a traded/mid price.",
    )
    parser.add_argument(
        "--default-liquidity",
        type=float,
        default=100.0,
        help="Fallback size used when historical records do not include depth.",
    )
    parser.add_argument("--polymarket-taker-fee", type=float, default=0.015)
    parser.add_argument("--kalshi-taker-fee", type=float, default=0.01)
    parser.add_argument("--gas-cost", type=float, default=0.02)
    parser.add_argument("--output-csv", help="Optional CSV path for opportunity rows.")
    parser.add_argument("--top", type=int, default=10, help="Number of top opportunities to print.")

    args = parser.parse_args()
    polymarket_records = load_historical_records(args.polymarket_file)
    kalshi_records = load_historical_records(args.kalshi_file)

    if args.all_pairs:
        pairs = make_all_pairs(polymarket_records, kalshi_records)
    else:
        pairs = list(args.pair)
        if args.pairs_file:
            pairs.extend(_read_pairs_file(args.pairs_file))

    if not pairs:
        raise SystemExit("Provide at least one --pair, --pairs-file, or use --all-pairs for a smoke test.")

    result = run_cross_platform_replay(
        polymarket_records=polymarket_records,
        kalshi_records=kalshi_records,
        pairs=pairs,
        min_edge=args.min_edge,
        max_time_delta=timedelta(minutes=args.max_time_delta_minutes),
        assumed_spread=args.assumed_spread,
        default_liquidity=args.default_liquidity,
        polymarket_taker_fee=args.polymarket_taker_fee,
        kalshi_taker_fee=args.kalshi_taker_fee,
        gas_cost=args.gas_cost,
    )

    print("=== Cross-Platform Historical Replay ===")
    print(
        "Replay mode: "
        + ("EXPLORATORY ALL-PAIRS SMOKE TEST (not a validated arbitrage backtest)" if args.all_pairs else "MATCHED PAIRS")
    )
    print(f"Polymarket records: {len(polymarket_records)} ({_format_range(polymarket_records)})")
    print(f"Kalshi records: {len(kalshi_records)} ({_format_range(kalshi_records)})")
    print(
        "Pricing model: historical quote replay where available; "
        f"{_count_missing_quotes(polymarket_records)} Polymarket records and "
        f"{_count_missing_quotes(kalshi_records)} Kalshi records used --assumed-spread={args.assumed_spread:.4f}."
    )
    print(f"Pairs requested: {result.pairs_requested}")
    print(f"Pairs evaluated: {result.pairs_evaluated}")
    print(f"Evaluations: {result.evaluations}")
    print(f"Opportunities: {result.opportunity_count}")
    print(f"Best net edge: {result.best_net_edge:.4f}")
    print(f"Average opportunity net edge: {result.avg_net_edge:.4f}")

    if not _ranges_overlap(
        polymarket_records,
        kalshi_records,
        timedelta(minutes=args.max_time_delta_minutes),
    ):
        print(
            "\nNo overlapping time range between these files. "
            "Fetch Polymarket and Kalshi data for the same window before interpreting strategy performance."
        )

    if args.all_pairs:
        print(
            "\nWarning: --all-pairs compares unrelated markets too. "
            "Do not read these opportunities as strategy/model performance. "
            "Use --pair or --pairs-file with semantically matched Polymarket/Kalshi markets."
        )

    if result.skipped_pairs:
        print("\nSkipped pairs:")
        for pair_key, reason in sorted(result.skipped_pairs.items()):
            print(f"  {pair_key}: {reason}")

    if result.opportunities:
        print(f"\nTop {min(args.top, len(result.opportunities))} opportunities:")
        top_opportunities = sorted(result.opportunities, key=lambda opp: opp.net_edge, reverse=True)[:args.top]
        for opportunity in top_opportunities:
            print(
                f"  {opportunity.timestamp.isoformat().replace('+00:00', 'Z')} | "
                f"{opportunity.polymarket_id}:{opportunity.kalshi_ticker} | "
                f"{opportunity.token} | buy {opportunity.buy_platform} {opportunity.buy_price:.4f}, "
                f"sell {opportunity.sell_platform} {opportunity.sell_price:.4f} | "
                f"net {opportunity.net_edge:.4f} ({opportunity.edge_pct:.2%})"
            )

    if args.output_csv:
        _write_opportunities_csv(args.output_csv, result.opportunities)
        print(f"\nWrote opportunities CSV to {args.output_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
