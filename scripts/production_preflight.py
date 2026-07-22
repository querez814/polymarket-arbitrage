#!/usr/bin/env python3
"""Fail-closed, non-network production deployment/canary preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.production_preflight import (
    PreflightPhase,
    evaluate_production_preflight,
    owner_private_regular_file,
    protected_regular_file,
)
from utils.config_loader import (
    ConfigError,
    load_config,
    resolve_runtime_secrets,
    validate_config,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--config-owner-uid", type=int)
    parser.add_argument(
        "--phase",
        choices=[phase.value for phase in PreflightPhase],
        default=PreflightPhase.DEPLOYMENT.value,
    )
    parser.add_argument("--working-directory", default=".")
    parser.add_argument("--state-directory")
    parser.add_argument("--secret-file", action="append", default=[])
    parser.add_argument("--secret-file-owner-uid", type=int)
    parser.add_argument("--canary-max-contracts", type=float)
    parser.add_argument("--canary-max-order-notional", type=float)
    args = parser.parse_args(argv)

    try:
        if args.config_owner_uid is not None and not protected_regular_file(
            Path(args.config), expected_uid=args.config_owner_uid
        ):
            raise ConfigError("config file contract failed")
        config = load_config(args.config)
        resolve_runtime_secrets(config)
        validate_config(config)
        report = evaluate_production_preflight(
            config,
            phase=PreflightPhase(args.phase),
            working_directory=Path(args.working_directory),
            state_directory=(
                Path(args.state_directory) if args.state_directory else None
            ),
            canary_max_contracts=args.canary_max_contracts,
            canary_max_order_notional=args.canary_max_order_notional,
        )
        if args.secret_file and not all(
            owner_private_regular_file(
                Path(path),
                expected_uid=args.secret_file_owner_uid,
                require_read=False,
            )
            for path in args.secret_file
        ):
            raise ConfigError("secret file contract failed")
    except (ConfigError, OSError, ValueError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "phase": args.phase,
                    "failed": ["configuration"],
                    "error": "configuration_invalid",
                },
                sort_keys=True,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "ok": report.ok,
                "phase": report.phase.value,
                "failed": list(report.failed),
                "checks": [
                    {
                        "name": check.name,
                        "passed": check.passed,
                        "detail": check.detail,
                    }
                    for check in report.checks
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
