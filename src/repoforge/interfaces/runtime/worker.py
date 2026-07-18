"""Long-lived supervisor process launched by `rf runtime start`."""

from __future__ import annotations

import argparse
from pathlib import Path

from ...bootstrap import run_runtime_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repoforge-runtime-worker")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--connector-identity",
        choices=("forge_v2",),
        default="forge_v2",
    )
    args = parser.parse_args(argv)
    return run_runtime_worker(
        Path(args.config),
        connector_identity=args.connector_identity,
    )


if __name__ == "__main__":
    raise SystemExit(main())
