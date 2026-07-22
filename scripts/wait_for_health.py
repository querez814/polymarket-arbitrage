#!/usr/bin/env python3
"""Wait for an HTTP health endpoint without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import time
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def wait_for_health(
    url: str, *, timeout_seconds: float, interval: float = 0.25
) -> bool:
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("health probe URL must use loopback HTTP")
    if timeout_seconds <= 0 or interval <= 0:
        raise ValueError("health probe timing must be positive")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(
                Request(url, method="GET"), timeout=min(2.0, interval * 4)
            ) as response:
                payload = json.loads(response.read())
                if response.status == 200 and payload.get("status") in {
                    "alive",
                    "ready",
                }:
                    return True
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(interval)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    return 0 if wait_for_health(args.url, timeout_seconds=args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
