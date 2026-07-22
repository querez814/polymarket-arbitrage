#!/usr/bin/env python3
"""Validate Kalshi and Polymarket credentials using read-only API calls only."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_client import KalshiClient
from polymarket_client import create_polymarket_client
from polymarket_client.api import PolymarketClient
from utils.config_loader import load_config, resolve_runtime_secrets


def _safe_error(prefix: str, exc: BaseException) -> None:
    """Report useful failure metadata without response bodies or credentials."""
    if isinstance(exc, httpx.HTTPStatusError):
        print(f"{prefix}: HTTP {exc.response.status_code}")
    else:
        print(f"{prefix}: {type(exc).__name__}")


async def validate_kalshi(config_path: Path) -> bool:
    config = load_config(str(config_path))
    api = config.api
    if not api.kalshi_api_key_id or not api.kalshi_private_key_path:
        print("KALSHI: FAIL (credential fields incomplete)")
        return False

    try:
        client = KalshiClient(
            base_url=api.kalshi_api_url,
            api_key_id=api.kalshi_api_key_id,
            private_key_path=api.kalshi_private_key_path,
            timeout=api.timeout_seconds,
            max_retries=api.max_retries,
            dry_run=True,
        )
        async with client:
            balance = await client.get_balance()
            positions = await client.get_positions(limit=1)
        if not isinstance(balance, dict):
            print("KALSHI: FAIL (authenticated balance response missing)")
            return False
        print(
            "KALSHI: PASS "
            f"(authenticated balance and positions reads; positions_sample={len(positions)})"
        )
        return True
    except BaseException as exc:
        _safe_error("KALSHI: FAIL", exc)
        return False


async def validate_polymarket(config_path: Path, keychain_label: str) -> bool:
    config = load_config(str(config_path))
    config.mode.trading_mode = "live"
    config.api.polymarket_private_key_keychain_label = keychain_label

    try:
        resolve_runtime_secrets(config)
        required = (
            config.api.api_key,
            config.api.api_secret,
            config.api.passphrase,
            config.api.private_key,
        )
        if not all(value and value.strip() for value in required):
            print("POLYMARKET: FAIL (credential fields incomplete)")
            return False

        client = create_polymarket_client(config)
        if not isinstance(client, PolymarketClient):
            print("POLYMARKET: FAIL (expected Global CLOB client)")
            return False
        await client.connect()
        try:
            balance = await client.get_usdc_balance()
            open_orders = await client.get_open_orders()
        finally:
            await client.disconnect()

        if balance is None:
            print("POLYMARKET: FAIL (authenticated balance response missing)")
            return False
        print(
            "POLYMARKET: PASS "
            f"(authenticated balance and open-orders reads; open_orders={len(open_orders)})"
        )
        return True
    except BaseException as exc:
        _safe_error("POLYMARKET: FAIL", exc)
        return False


async def run(args: argparse.Namespace) -> int:
    kalshi_ok = await validate_kalshi(args.kalshi_config)
    polymarket_ok = await validate_polymarket(
        args.polymarket_config, args.polymarket_keychain_label
    )
    return 0 if kalshi_ok and polymarket_ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only credential validation; never submits or cancels orders"
    )
    parser.add_argument("--kalshi-config", type=Path, required=True)
    parser.add_argument("--polymarket-config", type=Path, required=True)
    parser.add_argument("--polymarket-keychain-label", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
