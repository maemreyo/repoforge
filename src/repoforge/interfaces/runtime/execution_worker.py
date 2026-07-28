"""Dedicated process entry point for durable repository execution."""

from __future__ import annotations

import argparse
from pathlib import Path

from ...bootstrap import run_execution_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repoforge-execution-worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--generation", required=True, type=int)
    args = parser.parse_args(argv)
    if args.generation <= 0:
        parser.error("--generation must be positive")
    return run_execution_worker(
        Path(args.config),
        generation=args.generation,
    )


if __name__ == "__main__":
    raise SystemExit(main())
