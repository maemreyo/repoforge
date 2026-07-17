#!/usr/bin/env python3
"""Generate or verify the Forge v2 Pydantic JSON Schema golden bundle."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repoforge.contracts.registry import render_v2_schema_bundle  # noqa: E402

GOLDEN_PATH = ROOT / "docs" / "contracts" / "tool-schemas-v2.json"


def render() -> str:
    return (
        json.dumps(
            render_v2_schema_bundle(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Replace the reviewed golden bundle with the generated Pydantic schemas.",
    )
    args = parser.parse_args()
    actual = render()

    if args.write:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(actual, encoding="utf-8")
        print(f"updated {GOLDEN_PATH.relative_to(ROOT)}")
        return 0

    if not GOLDEN_PATH.is_file():
        print(f"missing schema golden: {GOLDEN_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 1
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    if expected == actual:
        print("Forge v2 tool schemas match the reviewed golden bundle (28 tools)")
        return 0

    diff = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=str(GOLDEN_PATH.relative_to(ROOT)),
        tofile="generated tool schemas",
        lineterm="",
    )
    print("Forge v2 tool schema drift detected:", file=sys.stderr)
    print("\n".join(diff), file=sys.stderr)
    print(
        "Review compatibility and run `uv run python scripts/generate_tool_schemas.py --write` "
        "only when the public contract change is intentional.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
