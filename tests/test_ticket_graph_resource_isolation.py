from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PURE_TICKET_TESTS = {
    "tests/test_issue_graph_proposals.py",
    "tests/test_ticket_graph.py",
    "tests/test_ticket_readiness.py",
}
EXCLUSIVE_TICKET_TESTS = {
    "tests/test_github_ticket_graph_adapter.py",
    "tests/test_github_ticket_project_adapter.py",
    "tests/test_issue_graph_publication.py",
    "tests/test_issue_graph_publication_adapter.py",
    "tests/test_issue_graph_publication_executor.py",
    "tests/test_issue_graph_workflow.py",
    "tests/test_repo_issue_graph_tools.py",
    "tests/test_ticket_project_sync.py",
    "tests/test_ticket_sync_cli.py",
}


def _load_script(name: str) -> Any:
    script = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"repoforge_{script.stem}", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_splits_pure_ticket_domain_from_exclusive_effects() -> None:
    selector = _load_script("select_affected_tests.py")
    manifest = selector.load_manifest(ROOT / "tests/test-groups.toml")
    ownership = {test_file: group for group in manifest.groups for test_file in group.test_files}

    assert {ownership[test_file].name for test_file in PURE_TICKET_TESTS} == {"ticket_graph_domain"}
    assert all(ownership[test_file].parallel for test_file in PURE_TICKET_TESTS)
    assert {ownership[test_file].name for test_file in EXCLUSIVE_TICKET_TESTS} == {
        "ticket_graph_release_e2e"
    }
    assert all(not ownership[test_file].parallel for test_file in EXCLUSIVE_TICKET_TESTS)


def test_catalog_preserves_pure_and_exclusive_ticket_lanes() -> None:
    catalog_module = _load_script("test_catalog.py")
    catalog = catalog_module.load_catalog(ROOT / "tests/catalog.toml")
    ownership = {
        test_file: capability
        for capability in catalog.capabilities
        for test_file in capability.test_files
    }

    assert {ownership[test_file].isolation for test_file in PURE_TICKET_TESTS} == {"pure"}
    assert {ownership[test_file].isolation for test_file in EXCLUSIVE_TICKET_TESTS} == {
        "exclusive:ticket_graph_release_e2e"
    }
    assert {tuple(ownership[test_file].resources) for test_file in EXCLUSIVE_TICKET_TESTS} == {
        ("ticket_graph_release_e2e",)
    }
