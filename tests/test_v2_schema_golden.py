from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from repoforge.contracts.artifacts import write_generated_artifacts
from repoforge.contracts.registry import render_v2_schema_bundle

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "docs" / "contracts" / "tool-schemas-v2.json"


def _load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"repoforge_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_artifact_writer_reports_digests_without_bodies(tmp_path: Path) -> None:
    artifacts = {
        "docs/contracts/release-contract-v2.json": '{"version":2}\n',
        "docs/contracts/tool-schemas-v2.json": '{"tool_count":28}\n',
    }

    first = write_generated_artifacts(tmp_path, artifacts)
    second = write_generated_artifacts(tmp_path, artifacts)

    assert first["changed_paths"] == sorted(artifacts)
    assert second["changed_paths"] == []
    first_identities = [(item["path"], item["sha256"]) for item in first["artifacts"]]
    second_identities = [(item["path"], item["sha256"]) for item in second["artifacts"]]
    assert first_identities == second_identities
    assert all(item["changed"] is True for item in first["artifacts"])
    assert all(item["changed"] is False for item in second["artifacts"])
    assert all(len(item["sha256"]) == 64 for item in first["artifacts"])
    encoded = json.dumps(first, sort_keys=True)
    assert "tool_count" not in encoded
    assert "version" not in encoded


def test_schema_generator_write_emits_bounded_digest_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    generator = _load_script("generate_tool_schemas")
    golden = tmp_path / "docs/contracts/tool-schemas-v2.json"
    identity = tmp_path / "src/repoforge/contracts/generated_contract_identity.py"
    identity.parent.mkdir(parents=True)
    identity.write_text(generator.render_identity_module(), encoding="utf-8")
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    monkeypatch.setattr(generator, "GOLDEN_PATH", golden)
    monkeypatch.setattr(generator, "IDENTITY_PATH", identity)
    monkeypatch.setattr(sys, "argv", ["generate_tool_schemas.py", "--write"])

    assert generator.main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["generator"] == "tool-schemas-v2"
    assert report["changed_paths"] == ["docs/contracts/tool-schemas-v2.json"]
    assert {item["path"] for item in report["artifacts"]} == {
        "docs/contracts/tool-schemas-v2.json",
        "src/repoforge/contracts/generated_contract_identity.py",
    }
    assert "properties" not in json.dumps(report, sort_keys=True)


def test_generated_identity_module_has_canonical_trailing_comma() -> None:
    generator = _load_script("generate_tool_schemas")

    rendered = generator.render_identity_module()

    assert rendered.endswith(",\n}\n")
    compile(rendered, "generated_contract_identity.py", "exec")


def test_release_generator_write_emits_bounded_digest_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    generator = _load_script("check_release_contracts")
    contract = tmp_path / "docs/contracts/release-contract-v2.json"

    async def release_contract() -> dict[str, object]:
        return {"contract_version": 2, "mcp": {"tool_count": 28}}

    monkeypatch.setattr(generator, "ROOT", tmp_path)
    monkeypatch.setattr(generator, "CONTRACT_PATH", contract)
    monkeypatch.setattr(generator, "build_release_contract", release_contract)
    monkeypatch.setattr(generator, "build_cli_release_contract", lambda: {"commands": []})
    monkeypatch.setattr(sys, "argv", ["check_release_contracts.py", "--write"])

    assert generator.main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["generator"] == "release-contract-v2"
    assert report["changed_paths"] == ["docs/contracts/release-contract-v2.json"]
    assert report["artifacts"][0]["path"] == "docs/contracts/release-contract-v2.json"
    assert "commands" not in json.dumps(report, sort_keys=True)


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
