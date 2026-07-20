#!/usr/bin/env python3
"""Generate or verify the Forge v2 Pydantic JSON Schema golden bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from repoforge.contracts.artifacts import write_generated_artifacts  # noqa: E402
from repoforge.contracts.registry import (  # noqa: E402
    render_contract_identity_artifact,
    render_v2_schema_bundle,
)

GOLDEN_PATH = ROOT / "docs" / "contracts" / "tool-schemas-v2.json"
IDENTITY_PATH = ROOT / "src" / "repoforge" / "contracts" / "generated_contract_identity.py"


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


def render_identity_module() -> str:
    payload = json.dumps(render_contract_identity_artifact(), indent=4, sort_keys=True)
    payload = f"{payload[:-2]},\n}}"
    return (
        '"""Generated Forge V2 contract identity. Do not edit by hand."""\n\n'
        "from __future__ import annotations\n\n"
        f"CONTRACT_IDENTITY: dict[str, object] = {payload}\n"
    )


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _drift_record(path: Path, expected: str, actual: str) -> dict[str, str]:
    return {
        "path": _relative(path),
        "expected_sha256": _digest(expected),
        "actual_sha256": _digest(actual),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Replace the reviewed golden bundle with the generated Pydantic schemas.",
    )
    args = parser.parse_args()
    actual = render()
    identity_actual = render_identity_module()

    if args.write:
        report = write_generated_artifacts(
            ROOT,
            {
                _relative(GOLDEN_PATH): actual,
                _relative(IDENTITY_PATH): identity_actual,
            },
        )
        print(
            json.dumps(
                {
                    "generator": "tool-schemas-v2",
                    "status": "written",
                    "tool_count": 28,
                    **report,
                },
                sort_keys=True,
            )
        )
        return 0

    if not GOLDEN_PATH.is_file():
        print(
            json.dumps(
                {
                    "generator": "tool-schemas-v2",
                    "status": "missing",
                    "path": _relative(GOLDEN_PATH),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    identity_expected = IDENTITY_PATH.read_text(encoding="utf-8") if IDENTITY_PATH.is_file() else ""
    if expected == actual and identity_expected == identity_actual:
        print(
            json.dumps(
                {
                    "generator": "tool-schemas-v2",
                    "status": "match",
                    "tool_count": 28,
                    "artifacts": [
                        {"path": _relative(GOLDEN_PATH), "sha256": _digest(actual)},
                        {"path": _relative(IDENTITY_PATH), "sha256": _digest(identity_actual)},
                    ],
                },
                sort_keys=True,
            )
        )
        return 0

    drift = []
    if expected != actual:
        drift.append(_drift_record(GOLDEN_PATH, expected, actual))
    if identity_expected != identity_actual:
        drift.append(_drift_record(IDENTITY_PATH, identity_expected, identity_actual))
    print(
        json.dumps(
            {
                "generator": "tool-schemas-v2",
                "status": "drift",
                "tool_count": 28,
                "artifacts": drift,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    print(
        "Review compatibility and run `uv run python scripts/generate_tool_schemas.py --write` "
        "only when the public contract change is intentional.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
