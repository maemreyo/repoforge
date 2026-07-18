#!/usr/bin/env python3
"""Fail closed when public release contracts drift without an updated golden review."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repoforge.contracts.registry import render_v2_schema_bundle  # noqa: E402
from repoforge.interfaces.cli.contract import build_cli_release_contract  # noqa: E402
from repoforge.interfaces.mcp.contract import build_release_contract  # noqa: E402

CONTRACT_PATH = ROOT / "docs/contracts/release-contract-v2.json"
TOOL_SCHEMA_PATH = ROOT / "docs/contracts/tool-schemas-v2.json"
PLAN_PATH = ROOT / "docs/plans/repoforge-production-architecture-tunnel-plan.md"


def _encoded(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _compact_encoded(payload: object) -> str:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _emit_github_error(message: str) -> None:
    """Expose a bounded contract diff to check annotations used by RepoForge evidence."""

    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    bounded = message[:12_000]
    escaped = bounded.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(
        "::error file=docs/contracts/release-contract-v2.json,"
        f"title=Public release contract drift::{escaped}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Replace the golden contract after an explicit compatibility review.",
    )
    args = parser.parse_args()

    actual = asyncio.run(build_release_contract())
    actual["cli"] = build_cli_release_contract()
    rendered = _encoded(actual)
    if args.write:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_PATH.write_text(rendered, encoding="utf-8")
        print(f"updated {CONTRACT_PATH.relative_to(ROOT)}")
        return 0

    if not CONTRACT_PATH.is_file():
        print(f"missing release contract: {CONTRACT_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 1
    expected_text = CONTRACT_PATH.read_text(encoding="utf-8")
    try:
        expected = json.loads(expected_text)
    except json.JSONDecodeError as exc:
        print(f"invalid release contract JSON: {exc}", file=sys.stderr)
        return 1
    if actual != expected:
        diff = difflib.unified_diff(
            expected_text.splitlines(),
            rendered.splitlines(),
            fromfile=str(CONTRACT_PATH.relative_to(ROOT)),
            tofile="generated release contract",
            lineterm="",
        )
        diff_text = "\n".join(diff)
        print("public release contract drift detected:", file=sys.stderr)
        print(diff_text, file=sys.stderr)
        _emit_github_error(diff_text)
        print(
            "Review compatibility and run `uv run python scripts/check_release_contracts.py --write` "
            "only when the contract change is intentional.",
            file=sys.stderr,
        )
        return 1

    tool_schema_rendered = _compact_encoded(render_v2_schema_bundle())
    if not TOOL_SCHEMA_PATH.is_file():
        print(f"missing tool schema golden: {TOOL_SCHEMA_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 1
    tool_schema_expected = TOOL_SCHEMA_PATH.read_text(encoding="utf-8")
    if tool_schema_expected != tool_schema_rendered:
        diff = difflib.unified_diff(
            tool_schema_expected.splitlines(),
            tool_schema_rendered.splitlines(),
            fromfile=str(TOOL_SCHEMA_PATH.relative_to(ROOT)),
            tofile="generated Forge v2 tool schemas",
            lineterm="",
        )
        print("Forge v2 tool schema drift detected:", file=sys.stderr)
        print("\n".join(diff), file=sys.stderr)
        print(
            "Review compatibility and run `uv run python scripts/generate_tool_schemas.py --write` "
            "only when the contract change is intentional.",
            file=sys.stderr,
        )
        return 1

    plan = PLAN_PATH.read_text(encoding="utf-8")
    required_plan_markers = (
        "Status: Implemented — Phases 0" + "\N{EN DASH}" + "8 complete",
        "Phase 8 — Program completion and release gates",
        "scripts/verify-production.sh",
        "release-contract-v2.json",
    )
    missing = [marker for marker in required_plan_markers if marker not in plan]
    if missing:
        print(f"plan/release contract disagreement; missing markers: {missing}", file=sys.stderr)
        return 1

    print(
        "release contracts match: "
        f"{len(actual['mcp']['tool_names'])} MCP tools, "
        f"surface={actual['mcp']['tool_surface_hash']}, "
        f"runtime-protocol={actual['runtime']['control_protocol_version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
