from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_builder() -> Any:
    script = Path(__file__).parents[1] / "scripts/build_coverage_map.py"
    spec = importlib.util.spec_from_file_location("repoforge_build_coverage_map", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_existing_coverage_mode_never_records_pytest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    builder = _load_builder()
    coverage_file = tmp_path / "canonical.coverage"
    coverage_file.write_bytes(b"placeholder")
    map_path = tmp_path / "coverage-map.json"
    map_path.write_text("{}\n", encoding="utf-8")

    def unexpected_record(*_args, **_kwargs):
        raise AssertionError("existing coverage mode must not spawn pytest")

    monkeypatch.setattr(builder, "_record_coverage", unexpected_record)
    monkeypatch.setattr(
        builder,
        "build_map",
        lambda _root, _coverage_file: {"src/repoforge/demo.py": ["tests/test_demo.py"]},
    )
    monkeypatch.setattr(builder, "check_map", lambda _root, _mapping, _map_path: 0)

    result = builder.main(
        [
            "--root",
            str(tmp_path),
            "--coverage-file",
            str(coverage_file),
            "--map-path",
            str(map_path),
            "--from-existing-coverage",
            "--check",
        ]
    )

    assert result == 0


def test_make_target_requires_canonical_coverage_file() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")

    target = makefile.split("\ntest-map-check-existing:", 1)[1].split("\n\n", 1)[0]
    assert "COVERAGE_FILE is required" in target
    assert "--from-existing-coverage" in target
    assert "--coverage-file" in target
