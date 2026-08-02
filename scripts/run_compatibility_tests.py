#!/usr/bin/env python3
"""Run the reviewed no-coverage compatibility subset for non-canonical CI cells."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

COMPATIBILITY_TESTS = (
    "tests/test_config.py",
    "tests/test_contract_identity_probe.py",
    "tests/test_onboarding_real_git.py",
    "tests/test_repository_discovery.py",
    "tests/test_runtime.py",
    "tests/test_runtime_adapters_and_serve.py",
    "tests/test_ssh_config_alias.py",
)


def build_command(root: Path, *, full: bool) -> list[str]:
    """Return a fixed reviewed command; compatibility cells never add coverage."""

    if full:
        return [
            sys.executable,
            str(root / "scripts/select_affected_tests.py"),
            "--full",
            "--run",
        ]
    missing = [test_file for test_file in COMPATIBILITY_TESTS if not (root / test_file).is_file()]
    if missing:
        raise ValueError("compatibility test files are missing: " + ", ".join(missing))
    return [sys.executable, "-m", "pytest", "-q", *COMPATIBILITY_TESTS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--full",
        action="store_true",
        help="Emergency rollback: run the full behavioral suite without coverage.",
    )
    args = parser.parse_args(argv)
    full = args.full or os.environ.get("REPOFORGE_CI_FULL_MATRIX") == "1"
    try:
        command = build_command(args.root.resolve(), full=full)
    except ValueError as exc:
        print(f"compatibility selection failed: {exc}", file=sys.stderr)
        return 2
    return subprocess.run(command, cwd=args.root.resolve(), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
