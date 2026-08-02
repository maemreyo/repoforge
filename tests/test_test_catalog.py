from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_module() -> Any:
    script = Path(__file__).parents[1] / "scripts/test_catalog.py"
    spec = importlib.util.spec_from_file_location("repoforge_test_catalog", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _catalog(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "catalog.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_catalog_digest_and_plan_are_deterministic(tmp_path: Path) -> None:
    catalog_module = _load_module()
    first = _catalog(
        tmp_path,
        """
version = 1
[safety_bundle]
test_files = ["tests/test_smoke.py"]
[capabilities.beta]
source_globs = ["src/beta/**"]
test_files = ["tests/test_beta.py"]
isolation = "exclusive:beta-state"
resources = ["beta-state"]
platforms = ["linux"]
python_versions = ["3.13"]
cost = "large"
[capabilities.alpha]
source_globs = ["src/alpha/**"]
test_files = ["tests/test_alpha.py"]
isolation = "pure"
resources = []
platforms = ["linux", "macos"]
python_versions = ["3.10", "3.13"]
cost = "small"
""",
    )
    catalog = catalog_module.load_catalog(first)

    one = catalog_module.compile_plan(catalog, ["src/alpha/example.py"], intent="affected")
    two = catalog_module.compile_plan(catalog, ["src/alpha/example.py"], intent="affected")

    assert one == two
    assert one.catalog_digest == catalog.digest
    assert one.selected_files == ("tests/test_alpha.py", "tests/test_smoke.py")
    assert one.lanes["parallel"] == ("tests/test_alpha.py", "tests/test_smoke.py")


def test_catalog_validation_rejects_duplicate_test_ownership(tmp_path: Path) -> None:
    catalog_module = _load_module()
    path = _catalog(
        tmp_path,
        """
version = 1
[capabilities.alpha]
source_globs = ["src/alpha/**"]
test_files = ["tests/test_shared.py"]
isolation = "pure"
resources = []
platforms = ["linux"]
python_versions = ["3.13"]
cost = "small"
[capabilities.beta]
source_globs = ["src/beta/**"]
test_files = ["tests/test_shared.py"]
isolation = "pure"
resources = []
platforms = ["linux"]
python_versions = ["3.13"]
cost = "small"
""",
    )

    with pytest.raises(catalog_module.CatalogError, match="more than one"):
        catalog_module.load_catalog(path)


def test_unknown_path_widens_with_a_stable_reason(tmp_path: Path) -> None:
    catalog_module = _load_module()
    path = _catalog(
        tmp_path,
        """
version = 1
[capabilities.alpha]
source_globs = ["src/alpha/**"]
test_files = ["tests/test_alpha.py"]
isolation = "pure"
resources = []
platforms = ["linux"]
python_versions = ["3.13"]
cost = "small"
""",
    )
    plan = catalog_module.compile_plan(
        catalog_module.load_catalog(path),
        ["src/unknown/example.py"],
        intent="affected",
    )

    assert plan.escalated is True
    assert plan.selected_files == ("tests/test_alpha.py",)
    assert plan.escalation_reason == "unowned changed paths: src/unknown/example.py"


def test_shadow_comparison_refuses_false_negatives() -> None:
    catalog_module = _load_module()

    comparison = catalog_module.compare_shadow(
        authoritative_files=("tests/test_a.py", "tests/test_b.py"),
        shadow_files=("tests/test_a.py",),
    )

    assert comparison.false_negatives == ("tests/test_b.py",)
    assert comparison.safe_to_cut_over is False
