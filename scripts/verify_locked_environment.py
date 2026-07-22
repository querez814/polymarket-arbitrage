#!/usr/bin/env python3
"""Verify that every hash-locked distribution is installed at the exact version."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re
from typing import Sequence

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")


def locked_versions(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = PIN.match(raw_line.strip())
        if match:
            pins[canonical_name(match.group(1))] = match.group(2)
    if not pins:
        raise ValueError("lock contains no exact pins")
    return pins


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def verify(
    path: Path, installed_versions: dict[str, str] | None = None
) -> tuple[str, ...]:
    mismatches: list[str] = []
    expected_versions = locked_versions(path)
    if installed_versions is None:
        installed_versions = {
            canonical_name(distribution.metadata["Name"]): distribution.version
            for distribution in metadata.distributions()
            if distribution.metadata["Name"]
        }
    else:
        installed_versions = {
            canonical_name(name): version
            for name, version in installed_versions.items()
        }
    for name, expected in expected_versions.items():
        actual = installed_versions.get(name)
        if actual is None:
            mismatches.append(f"{name}:missing")
            continue
        if actual != expected:
            mismatches.append(f"{name}:version-mismatch")
    for name in sorted(installed_versions.keys() - expected_versions.keys()):
        mismatches.append(f"{name}:unexpected")
    return tuple(mismatches)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True)
    args = parser.parse_args(argv)
    mismatches = verify(Path(args.lock))
    if mismatches:
        print("locked environment verification failed: " + ", ".join(mismatches))
        return 1
    print("locked environment verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
