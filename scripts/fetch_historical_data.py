#!/usr/bin/env python3
"""
Fetch historical Polymarket and Kalshi data into normalized JSONL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from utils.config_loader import load_config
from utils.historical_data import (
    normalize_kalshi_candlesticks,
    normalize_polymarket_history,
    to_unix_seconds,
    write_jsonl,
)


logger = logging.getLogger(__name__)


def _default_output(platform: str, start_ts: int, end_ts: int) -> Path:
    start = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("data") / "historical" / f"{platform}_{start}_{end}.jsonl"


def _market_args(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _filter_history_range(history: list[dict], start_ts: int, end_ts: int) -> list[dict]:
    return [
        point for point in history
        if start_ts <= int(point.get("t", 0)) <= end_ts
    ]


async def fetch_polymarket(
    *,
    config_path: str,
    market_ids: list[str],
    max_markets: int,
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int,
) -> list[dict]:
    config = load_config(config_path)
    client = PolymarketClient(
        rest_url=config.api.polymarket_rest_url,
        ws_url=config.api.polymarket_ws_url,
        gamma_url=config.api.gamma_api_url,
        timeout=config.api.timeout_seconds,
        max_retries=config.api.max_retries,
        retry_delay=config.api.retry_delay_seconds,
        dry_run=True,
    )
    await client.connect()

    try:
        if market_ids:
            markets = [await client.get_market(market_id) for market_id in market_ids]
        else:
            markets = await client.list_markets({"closed": "false", "limit": max_markets})

        records: list[dict] = []
        for market in markets:
            token_pairs = [
                ("YES", market.yes_token_id),
                ("NO", market.no_token_id),
            ]
            for token, token_id in token_pairs:
                if not token_id:
                    continue
                history = await client.get_prices_history(
                    token_id=token_id,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    fidelity=fidelity_minutes,
                )
                history = _filter_history_range(history, start_ts, end_ts)
                records.extend(normalize_polymarket_history(
                    market_id=market.market_id,
                    token=token,
                    token_id=token_id,
                    history=history,
                ))
                await asyncio.sleep(0.1)

        return records
    finally:
        await client.disconnect()


async def fetch_kalshi(
    *,
    market_tickers: list[str],
    max_markets: int,
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int,
    kalshi_archived: bool,
) -> list[dict]:
    records: list[dict] = []

    async def resolve_series_ticker(client: KalshiClient, market) -> str:
        if not market:
            return ""
        if market.series_ticker:
            return market.series_ticker
        if not market.event_ticker:
            return ""
        event = await client.get_event(market.event_ticker)
        return event.series_ticker if event else ""

    async with KalshiClient(dry_run=True) as client:
        if market_tickers:
            markets = []
            for ticker in market_tickers:
                market = await client.get_market(ticker)
                if market:
                    markets.append(market)
                else:
                    markets.append(None)
        elif kalshi_archived:
            markets, _ = await client.list_historical_markets(max_markets=max_markets)
        else:
            markets = await client.list_all_markets(status="open", max_markets=max_markets)

        for market_or_none, ticker in zip(markets, market_tickers or [m.ticker for m in markets]):
            source = "candlesticks"
            if market_or_none and not kalshi_archived:
                series_ticker = await resolve_series_ticker(client, market_or_none)
                if not series_ticker:
                    logger.warning("Skipping %s; Kalshi did not provide a series ticker", market_or_none.ticker)
                    continue
                candlesticks = await client.get_market_candlesticks(
                    series_ticker=series_ticker,
                    ticker=market_or_none.ticker,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    period_interval=fidelity_minutes,
                    include_latest_before_start=False,
                )
                if not candlesticks:
                    candlesticks = await client.get_historical_market_candlesticks(
                        ticker=market_or_none.ticker,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        period_interval=fidelity_minutes,
                    )
                    source = "historical-candlesticks"
                market_id = market_or_none.ticker
            else:
                candlesticks = await client.get_historical_market_candlesticks(
                    ticker=ticker,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    period_interval=fidelity_minutes,
                )
                source = "historical-candlesticks"
                market_id = ticker

            records.extend(normalize_kalshi_candlesticks(
                market_id=market_id,
                candlesticks=candlesticks,
                source=source,
            ))
            await asyncio.sleep(0.1)

    return records


async def run(args: argparse.Namespace) -> int:
    start_ts = to_unix_seconds(args.start)
    end_ts = to_unix_seconds(args.end)
    if end_ts <= start_ts:
        raise SystemExit("--end must be after --start")

    market_ids = _market_args(args.markets)
    all_records: list[dict] = []

    if args.platform in ("polymarket", "both"):
        poly_records = await fetch_polymarket(
            config_path=args.config,
            market_ids=market_ids if args.platform == "polymarket" else [],
            max_markets=args.max_markets,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity_minutes=args.fidelity_minutes,
        )
        logger.info("Fetched %s Polymarket records", len(poly_records))
        all_records.extend(poly_records)

    if args.platform in ("kalshi", "both"):
        kalshi_records = await fetch_kalshi(
            market_tickers=market_ids if args.platform == "kalshi" else [],
            max_markets=args.max_markets,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity_minutes=args.fidelity_minutes,
            kalshi_archived=args.kalshi_archived,
        )
        logger.info("Fetched %s Kalshi records", len(kalshi_records))
        all_records.extend(kalshi_records)

    output = Path(args.output) if args.output else _default_output(args.platform, start_ts, end_ts)
    count = write_jsonl(output, sorted(all_records, key=lambda r: (r["timestamp"], r["platform"], r["market_id"])))
    print(f"Wrote {count} records to {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch historical Polymarket/Kalshi data into normalized JSONL."
    )
    parser.add_argument(
        "--platform",
        choices=("polymarket", "kalshi", "both"),
        required=True,
        help="Which platform to fetch.",
    )
    parser.add_argument("--start", required=True, help="Start timestamp, ISO-8601 or Unix seconds.")
    parser.add_argument("--end", required=True, help="End timestamp, ISO-8601 or Unix seconds.")
    parser.add_argument(
        "--fidelity-minutes",
        type=int,
        default=60,
        help="Data resolution in minutes (default: 60).",
    )
    parser.add_argument(
        "--output",
        help="Output JSONL path. Defaults to data/historical/<platform>_<range>.jsonl.",
    )
    parser.add_argument(
        "--markets",
        help="Comma-separated Polymarket market IDs or Kalshi tickers for single-platform fetches.",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=25,
        help="Maximum markets to auto-discover when --markets is omitted.",
    )
    parser.add_argument(
        "--kalshi-archived",
        action="store_true",
        help="Use Kalshi's archived historical candlestick endpoint.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Config file for Polymarket endpoint settings.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
