#!/usr/bin/env python3
"""Call a loopback operator endpoint without placing its bearer token in argv."""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence
from urllib import request
from urllib.parse import urlsplit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "resume", "panic"))
    parser.add_argument("--reason", default="")
    parser.add_argument("--base-url", default="http://127.0.0.1:8888")
    args = parser.parse_args(argv)
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        print("operator endpoint must be loopback HTTP")
        return 2
    token = os.environ.get("NIGHTWATCH_OPERATOR_TOKEN", "")
    if not token:
        print("operator token is unavailable")
        return 2
    body = None
    method = "GET"
    headers = {"Authorization": f"Bearer {token}"}
    if args.action != "status":
        if not args.reason.strip():
            print("--reason is required for mutations")
            return 2
        method = "POST"
        body = json.dumps({"reason": args.reason}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    target = f"{args.base_url.rstrip('/')}/api/operator/{args.action}"
    response = request.urlopen(
        request.Request(target, data=body, headers=headers, method=method), timeout=10
    )
    print(response.read().decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
