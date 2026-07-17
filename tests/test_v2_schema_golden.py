from __future__ import annotations

import json
from pathlib import Path

import tomllib

from repoforge.contracts.registry import render_v2_schema_bundle

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "docs" / "contracts" / "tool-schemas-v2.json"


def test_schema_bundle_is_deterministic_and_complete() -> None:
    first = render_v2_schema_bundle()
    second = render_v2_schema_bundle()

    assert first == second
    assert first["contract_version"] == 2
    assert first["tool_count"] == 28
    assert first["evolution"] == {
        "outputs_closed": True,
        "tolerant_reader_required": True,
        "additive_output_fields_require_contract_bump": False,
    }
    assert tuple(first["tools"]) == tuple(sorted(first["tools"]))


def test_committed_golden_matches_generated_schema_bundle() -> None:
    assert GOLDEN.is_file()
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert expected == render_v2_schema_bundle()


def test_committed_golden_is_byte_stable() -> None:
    actual = GOLDEN.read_text(encoding="utf-8")
    expected = (
        json.dumps(
            render_v2_schema_bundle(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    if actual != expected:
        mismatch = next(
            (
                index
                for index, pair in enumerate(zip(actual, expected, strict=False))
                if pair[0] != pair[1]
            ),
            min(len(actual), len(expected)),
        )
        window_start = max(0, mismatch - 80)
        window_end = mismatch + 160
        raise AssertionError(
            {
                "mismatch": mismatch,
                "actual_length": len(actual),
                "expected_length": len(expected),
                "actual_ends_newline": actual.endswith("\n"),
                "expected_ends_newline": expected.endswith("\n"),
                "actual_window": actual[window_start:window_end],
                "expected_window": expected[window_start:window_end],
            }
        )


def test_pydantic_is_an_explicit_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"]

    assert any(item.startswith("pydantic>=2.13") for item in dependencies)
