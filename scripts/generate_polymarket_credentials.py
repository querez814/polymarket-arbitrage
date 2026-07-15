#!/usr/bin/env python3
"""
Generate Polymarket CLOB API credentials from a wallet private key.

This helper uses the official `py_clob_client_v2` SDK to create or derive
the L2 API credentials required for authenticated Polymarket trading.

It intentionally does not write the wallet private key to disk unless the
caller explicitly asks for it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _read_private_key(explicit_key: str | None) -> str:
    """Resolve the wallet private key from CLI flags or environment."""
    for candidate in (
        explicit_key,
        os.environ.get("POLYMARKET_PRIVATE_KEY"),
        os.environ.get("PRIVATE_KEY"),
        os.environ.get("PK"),
    ):
        if candidate:
            key = candidate.strip()
            if key:
                return key
    raise SystemExit(
        "Missing private key. Pass --private-key or set POLYMARKET_PRIVATE_KEY."
    )


def _get_attr(obj: object, *names: str) -> str:
    """Read the first matching attribute from a credential object."""
    for name in names:
        value = getattr(obj, name, None)
        if value:
            return str(value)
    raise SystemExit(
        "Could not read generated credentials from the SDK response."
    )


def _format_yaml(api_key: str, api_secret: str, passphrase: str, private_key: str | None) -> str:
    """Render a YAML fragment that can be merged into config.yaml."""
    generated_at = datetime.now(ZoneInfo("America/New_York")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )
    lines = [
        f"# Generated: {generated_at}",
        "api:",
        f'  api_key: "{api_key}"',
        f'  api_secret: "{api_secret}"',
        f'  passphrase: "{passphrase}"',
    ]
    if private_key is not None:
        lines.append(f'  private_key: "{private_key}"')
    else:
        lines.append("  # private_key is intentionally omitted; use POLYMARKET_PRIVATE_KEY")
    return "\n".join(lines) + "\n"


def generate_credentials(host: str, chain_id: int, private_key: str) -> tuple[str, str, str]:
    """Generate or derive the Polymarket API credentials."""
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: py_clob_client_v2.\n"
            "Run this with uv, for example:\n"
            "  uv run --with py-clob-client-v2 python scripts/generate_polymarket_credentials.py "
            "--private-key <YOUR_PRIVATE_KEY>"
        ) from exc

    client = ClobClient(host=host, chain_id=chain_id, key=private_key)
    creds = client.create_or_derive_api_key()

    api_key = _get_attr(creds, "api_key", "key")
    api_secret = _get_attr(creds, "api_secret", "secret")
    passphrase = _get_attr(creds, "passphrase", "api_passphrase", "pass_phrase")
    return api_key, api_secret, passphrase


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Polymarket API credentials and a YAML config fragment."
    )
    parser.add_argument(
        "--private-key",
        help="Wallet private key used to derive the API credentials.",
    )
    parser.add_argument(
        "--host",
        default="https://clob.polymarket.com",
        help="Polymarket CLOB host (default: https://clob.polymarket.com).",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=137,
        help="Polygon chain ID (default: 137).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write the generated YAML fragment.",
    )
    parser.add_argument(
        "--include-private-key",
        action="store_true",
        help="Include the private key in the output fragment (not recommended).",
    )

    args = parser.parse_args()
    private_key = _read_private_key(args.private_key)
    api_key, api_secret, passphrase = generate_credentials(
        host=args.host,
        chain_id=args.chain_id,
        private_key=private_key,
    )

    fragment = _format_yaml(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        private_key=private_key if args.include_private_key else None,
    )

    if args.output:
        args.output.write_text(fragment, encoding="utf-8")
        print(f"Wrote Polymarket credential config to {args.output}")
    else:
        sys.stdout.write(fragment)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
