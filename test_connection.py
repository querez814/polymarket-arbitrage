#!/usr/bin/env python3
"""
Test Polymarket API Connection
===============================

Run this to verify read-only market data connectivity and local credentials.

Usage:
    python3 test_connection.py
    python3 test_connection.py --config config.yaml
"""

import asyncio
import argparse
import sys

from utils.config_loader import load_config
from utils.logging_utils import setup_logging
from polymarket_client import PolymarketClient


async def test_connection(config_path: str = "config.yaml"):
    """Test the API connection and credentials."""
    print("=" * 60)
    print("Polymarket API Connection Test")
    print("=" * 60)
    
    # Load config
    try:
        config = load_config(config_path)
        print(f"[OK] Config loaded from {config_path}")
    except Exception as e:
        print(f"[ERROR] Failed to load config: {e}")
        return False
    
    print(f"   Mode: {config.mode.trading_mode.upper()}")
    print(f"   API Key: {config.api.api_key[:8]}..." if config.api.api_key else "   API Key: NOT SET")
    
    # Check credentials
    if config.is_live:
        if not config.api.api_key:
            print("[ERROR] API key not configured!")
            print("   Add POLYMARKET_API_KEY to .env.local or your secret manager")
            return False
        
        if not config.api.private_key:
            print("[ERROR] Private key not configured!")
            print("   Add POLYMARKET_PRIVATE_KEY to .env.local or your secret manager")
            return False
    
    print()
    print("Testing API connection...")
    
    # Create client
    client = PolymarketClient(
        rest_url=config.api.polymarket_rest_url,
        ws_url=config.api.polymarket_ws_url,
        gamma_url=config.api.gamma_api_url,
        api_key=config.api.api_key,
        api_secret=config.api.api_secret,
        private_key=config.api.private_key,
        timeout=config.api.timeout_seconds,
        dry_run=config.is_dry_run,
    )
    
    try:
        await client.connect()
        print("[OK] HTTP client connected")
    except Exception as e:
        print(f"[ERROR] Connection failed: {e}")
        return False
    
    # Test Gamma API (market data)
    print()
    print("Testing Gamma API (market data)...")
    try:
        markets = await client.list_markets({"limit": 5, "closed": "false"})
        print(f"[OK] Gamma API working - found {len(markets)} markets")
        
        if markets:
            print("   Sample markets:")
            for m in markets[:3]:
                print(f"   - {m.question[:50]}...")
                print(f"     Volume 24h: ${m.volume_24h:,.0f} | Liquidity: ${m.liquidity:,.0f}")
    except Exception as e:
        print(f"[ERROR] Gamma API error: {e}")
        await client.disconnect()
        return False
    
    # Test positions (requires auth)
    if config.is_live:
        print()
        print("Testing authenticated endpoints...")
        try:
            positions = await client.get_positions()
            print(f"[OK] Auth working - {len(positions)} positions")
        except Exception as e:
            print(f"[WARN] Could not fetch positions: {e}")
            print("   This may be normal if you have no positions yet")
    
    await client.disconnect()
    
    print()
    print("=" * 60)
    print("[OK] Connection test PASSED!")
    print("=" * 60)
    print()
    print("Next steps:")
    print("1. Run scanner: uv run python run_with_dashboard.py")
    print("2. Run paper mode intentionally: uv run python run_with_dashboard.py --paper")
    print("3. Keep live mode disabled until the Phase 6 execution layer exists")
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
