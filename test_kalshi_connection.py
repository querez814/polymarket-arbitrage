#!/usr/bin/env python3
"""
Test Kalshi API Connection
==========================

Run before enabling Kalshi live trading.

Usage:
    uv run python test_kalshi_connection.py
    uv run python test_kalshi_connection.py -c config.yaml

Environment (recommended — never commit keys):
    export KALSHI_API_KEY_ID="your-uuid-from-kalshi"
    export KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi/kalshi-key.pem"
    export KALSHI_API_URL="https://api.elections.kalshi.com/trade-api/v2"
    # Demo sandbox:
    # export KALSHI_API_URL="https://demo-api.kalshi.co/trade-api/v2"

Docs:
    https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
    https://docs.kalshi.com/api-reference/exchange/get-exchange-status
"""

import argparse
import asyncio
import sys

from kalshi_client import KalshiClient
from utils.config_loader import load_config
from utils.logging_utils import setup_logging

# This is an explicitly invoked network diagnostic, not an automated pytest test.
# Keep its public function name for callers while preventing accidental collection.
__test__ = False


async def test_kalshi_connection(config_path: str = "config.yaml") -> bool:
    print("=" * 60)
    print("🔌 Kalshi API Connection Test")
    print("=" * 60)

    try:
        config = load_config(config_path)
        print(f"✅ Config loaded from {config_path}")
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return False

    api = config.api
    print(f"   API URL: {api.kalshi_api_url}")
    print(
        f"   Auth configured: {bool(api.kalshi_api_key_id and api.kalshi_private_key_path)}"
    )

    client = KalshiClient(
        base_url=api.kalshi_api_url,
        api_key_id=api.kalshi_api_key_id or None,
        private_key_path=api.kalshi_private_key_path or None,
        timeout=api.timeout_seconds,
        max_retries=api.max_retries,
        dry_run=not config.is_live,
    )

    async with client:
        print()
        print("📡 Exchange status (public)...")
        try:
            status = await client.get_exchange_status()
            if status:
                active = status.get("exchange_active")
                trading = status.get("trading_active")
                print(f"✅ Exchange active: {active}  |  Trading active: {trading}")
            else:
                print("⚠️  Empty exchange status response")
        except Exception as e:
            print(f"❌ Exchange status failed: {e}")
            return False

        print()
        print("📊 Market data sample...")
        try:
            markets, _ = await client.list_markets(limit=3)
            print(f"✅ Market data working — {len(markets)} markets returned")
            for m in markets[:3]:
                print(f"   - {m.title[:60]}")
        except Exception as e:
            print(f"❌ Market data failed: {e}")
            return False

        if not client.is_authenticated:
            print()
            print("⚠️  Kalshi API credentials not configured (public endpoints only).")
            print()
            print("To enable live Kalshi trading:")
            print("1. Kalshi → Account & security → API Keys → Create Key")
            print(
                "2. Save the API Key ID and download the .key / .pem file immediately"
            )
            print("3. export KALSHI_API_KEY_ID='...'")
            print("4. export KALSHI_PRIVATE_KEY_PATH='/path/to/kalshi-key.pem'")
            print("5. Re-run this script")
            print()
            print("=" * 60)
            print("✅ Public Kalshi connection OK (auth not tested)")
            print("=" * 60)
            return True

        print()
        print("🔐 Authenticated portfolio check...")
        try:
            balance = await client.get_balance_dollars()
            if balance is not None:
                print(f"✅ Auth working — available balance: ${balance:.2f}")
            else:
                print("⚠️  Balance endpoint returned no data")
        except Exception as e:
            print(f"❌ Auth failed: {e}")
            print(
                "   Check API Key ID, private key path, and that URL matches your key (prod vs demo)"
            )
            return False

        try:
            positions = await client.get_positions(limit=10)
            print(f"✅ Positions endpoint — {len(positions)} open market position(s)")
        except Exception as e:
            print(f"⚠️  Positions check failed: {e}")

    print()
    print("=" * 60)
    print("✅ Kalshi connection test PASSED!")
    print("=" * 60)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Kalshi API connection")
    parser.add_argument("-c", "--config", default="config.yaml", help="Config file")
    args = parser.parse_args()

    setup_logging(console_level="WARNING")
    success = asyncio.run(test_kalshi_connection(args.config))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
