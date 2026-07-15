#!/usr/bin/env python3
"""
Collect live matched-pair order-book snapshots for ML training.

This intentionally writes pair-level JSONL rows. Each row contains both venue
quotes plus computed edge directions, so later dataset builders can add labels
without re-fetching market data.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from scripts.backtest_cross_platform import _read_pairs_file
from utils.config_loader import load_config
from utils.historical_data import write_jsonl
from utils.training_data import build_training_snapshot, utc_now_iso


logger = logging.getLogger(__name__)


def _default_output() -> Path:
    return Path("data") / "training" / "live_snapshots.jsonl"


async def collect_once(
    *,
    pairs: list[tuple[str, str]],
    output: Path,
    config_path: str,
    polymarket_taker_fee: float,
    kalshi_taker_fee: float,
    gas_cost: float,
    sleep_between_pairs: float,
) -> int:
    """Collect one snapshot for each matched pair and append it to JSONL."""
    config = load_config(config_path)
    polymarket = PolymarketClient(
        rest_url=config.api.polymarket_rest_url,
        ws_url=config.api.polymarket_ws_url,
        gamma_url=config.api.gamma_api_url,
        timeout=config.api.timeout_seconds,
        max_retries=config.api.max_retries,
        retry_delay=config.api.retry_delay_seconds,
        dry_run=True,
    )
    await polymarket.connect()

    records: list[dict] = []
    try:
        async with KalshiClient(
            timeout=config.api.timeout_seconds,
            max_retries=config.api.max_retries,
            dry_run=True,
        ) as kalshi:
            for polymarket_id, kalshi_ticker in pairs:
                timestamp = utc_now_iso()
                try:
                    poly_market = await polymarket.get_market(polymarket_id)
                    kalshi_market = await kalshi.get_market(kalshi_ticker)
                    if not kalshi_market:
                        logger.warning("Skipping %s:%s; Kalshi market not found", polymarket_id, kalshi_ticker)
                        continue

                    poly_orderbook = await polymarket.get_orderbook(polymarket_id)
                    kalshi_orderbook = await kalshi.get_orderbook_unified(kalshi_ticker)
                    if not kalshi_orderbook:
                        logger.warning("Skipping %s:%s; Kalshi order book not available", polymarket_id, kalshi_ticker)
                        continue

                    records.append(build_training_snapshot(
                        timestamp=timestamp,
                        polymarket_id=polymarket_id,
                        kalshi_ticker=kalshi_ticker,
                        polymarket_orderbook=poly_orderbook,
                        kalshi_orderbook=kalshi_orderbook,
                        polymarket_question=poly_market.question,
                        kalshi_title=kalshi_market.title,
                        polymarket_taker_fee=polymarket_taker_fee,
                        kalshi_taker_fee=kalshi_taker_fee,
                        gas_cost=gas_cost,
                    ))
                except Exception as exc:
                    logger.warning("Failed to collect %s:%s: %s", polymarket_id, kalshi_ticker, exc)

                if sleep_between_pairs > 0:
                    await asyncio.sleep(sleep_between_pairs)
    finally:
        await polymarket.disconnect()

    count = write_jsonl(output, records, append=True)
    logger.info("Appended %s training snapshots to %s", count, output)
    return count


async def run(args: argparse.Namespace) -> int:
    pairs = _read_pairs_file(args.pairs_file)
    if not pairs:
        raise SystemExit("--pairs-file did not contain any matched pairs")

    output = Path(args.output) if args.output else _default_output()
    iterations_completed = 0
    total_written = 0

    while args.iterations == 0 or iterations_completed < args.iterations:
        written = await collect_once(
            pairs=pairs,
            output=output,
            config_path=args.config,
            polymarket_taker_fee=args.polymarket_taker_fee,
            kalshi_taker_fee=args.kalshi_taker_fee,
            gas_cost=args.gas_cost,
            sleep_between_pairs=args.sleep_between_pairs,
        )
        total_written += written
        iterations_completed += 1

        print(
            f"Iteration {iterations_completed}: wrote {written} snapshots "
            f"({total_written} total) to {output}"
        )

        if args.iterations != 0 and iterations_completed >= args.iterations:
            break
        await asyncio.sleep(args.interval_seconds)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect live matched Polymarket/Kalshi order-book snapshots for ML training."
    )
    parser.add_argument(
        "--pairs-file",
        required=True,
        help="Matched pairs file. Supports POLYMARKET_ID:KALSHI_TICKER or POLYMARKET_ID,KALSHI_TICKER.",
    )
    parser.add_argument(
        "--output",
        help="Output JSONL path. Defaults to data/training/live_snapshots.jsonl.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of collection loops. Use 0 to run forever.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=60.0,
        help="Delay between collection loops.",
    )
    parser.add_argument(
        "--sleep-between-pairs",
        type=float,
        default=0.2,
        help="Small delay between pair requests to reduce rate-limit pressure.",
    )
    parser.add_argument("--polymarket-taker-fee", type=float, default=0.015)
    parser.add_argument("--kalshi-taker-fee", type=float, default=0.01)
    parser.add_argument("--gas-cost", type=float, default=0.02)
    parser.add_argument("--config", default="config.yaml", help="Config file for endpoint settings.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")

    args = parser.parse_args()
    if args.iterations < 0:
        raise SystemExit("--iterations must be >= 0")
    if args.interval_seconds < 0:
        raise SystemExit("--interval-seconds must be >= 0")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
