#!/usr/bin/env python3
"""
Test Polymarket API Connection
===============================

Run this BEFORE going live to verify your credentials work.

Usage:
    python3 test_connection.py
    python3 test_connection.py --config config.live.yaml   # before going live
"""

import asyncio
import argparse
import sys

import pytest

from utils.config_loader import load_config
from utils.logging_utils import setup_logging
from polymarket_client import create_polymarket_client

pytestmark = pytest.mark.asyncio


async def test_connection(config_path: str = "config.yaml"):
    """Test the API connection and credentials."""
    print("=" * 60)
    print("🔌 Polymarket API Connection Test")
    print("=" * 60)

    try:
        config = load_config(config_path)
        print(f"✅ Config loaded from {config_path}")
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return False

    platform = "Polymarket US" if config.is_polymarket_us else "Polymarket Global"
    print(f"   Platform: {platform}")
    print(f"   Mode: {config.mode.trading_mode.upper()}")

    if config.is_live:
        if config.is_polymarket_us:
            if not config.api.polymarket_us_key_id:
                print("❌ POLYMARKET_US_KEY_ID not configured")
                return False
            if not config.api.polymarket_us_secret_key:
                print("❌ POLYMARKET_US_SECRET_KEY not configured")
                return False
            print(f"   US Key ID: {config.api.polymarket_us_key_id[:8]}...")
        else:
            if not config.api.api_key:
                print("❌ Global API key not configured")
                return False
            if not config.api.private_key:
                print("❌ Global private key not configured")
                return False
            print(f"   API Key: {config.api.api_key[:8]}...")

    print()
    print("📡 Testing API connection...")

    client = create_polymarket_client(config)

    try:
        await client.connect()
        print("✅ Client connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

    print()
    print(f"📊 Testing {platform} market data...")
    try:
        markets = await client.list_markets({"limit": 5, "max_markets": 5})
        print(f"✅ Market data working - found {len(markets)} markets")
        for market in markets[:3]:
            print(f"   - {market.question[:60]}")
    except Exception as e:
        print(f"❌ Market data error: {e}")
        await client.disconnect()
        return False

    if config.is_live:
        print()
        print("💼 Testing authenticated endpoints...")
        try:
            balance = await client.get_usdc_balance()
            if balance is not None:
                print(f"✅ Auth working - buying power: ${balance:.2f}")
            else:
                print("⚠️  Could not fetch account balance")
        except Exception as e:
            print(f"❌ Auth failed: {e}")
            await client.disconnect()
            return False

        try:
            positions = await client.get_positions()
            print(f"✅ Positions endpoint reachable - {len(positions)} markets tracked")
        except Exception as e:
            print(f"⚠️  Could not fetch positions: {e}")

    await client.disconnect()

    print()
    print("=" * 60)
    print("✅ Connection test PASSED!")
    print("=" * 60)
    print()
    print("Next steps:")
    print(f"1. Review {config_path} settings")
    if config.is_live:
        print("2. Start with: python run_with_dashboard.py --live -c", config_path)
        print("3. Monitor closely on the dashboard")
    else:
        print("2. Dry run: python run_with_dashboard.py -c", config_path)
        print("3. When ready for live: cp config.live.yaml.example config.live.yaml")
    print()
    return True


def main():
    parser = argparse.ArgumentParser(description="Test Polymarket API connection")
    parser.add_argument("-c", "--config", default="config.yaml", help="Config file")
    args = parser.parse_args()

    setup_logging(console_level="WARNING")

    success = asyncio.run(test_connection(args.config))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
