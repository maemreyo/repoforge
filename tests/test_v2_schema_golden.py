from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

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


def test_pydantic_is_an_explicit_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"]

    assert any(item.startswith("pydantic>=2.13") for item in dependencies)
