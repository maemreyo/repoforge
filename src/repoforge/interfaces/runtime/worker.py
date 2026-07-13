"""Long-lived supervisor process launched by `rf runtime start`."""

from __future__ import annotations

import argparse
from pathlib import Path

from ...bootstrap import run_runtime_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repoforge-runtime-worker")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    return run_runtime_worker(Path(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
