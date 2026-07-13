from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "repoforge"
_ALLOWED_ROOT_MODULES = {"__init__.py", "__main__.py", "bootstrap.py", "config.py"}
_REMOVED_MODULES = {
    "repoforge.audit",
    "repoforge.cli",
    "repoforge.config_delta",
    "repoforge.discovery",
    "repoforge.errors",
    "repoforge.onboarding",
    "repoforge.proposal",
    "repoforge.runner",
    "repoforge.runtime",
    "repoforge.runtime_worker",
    "repoforge.security",
    "repoforge.server",
    "repoforge.service",
    "repoforge.state",
    "repoforge.user_config",
    "repoforge.workspace_apply_patch",
    "repoforge.workspace_create",
    "repoforge.workspace_file_read",
    "repoforge.workspace_file_write",
    "repoforge.workspace_replace_text",
}


def test_root_package_contains_only_active_composition_and_config_modules() -> None:
    actual = {path.name for path in PACKAGE.glob("*.py")}
    assert actual == _ALLOWED_ROOT_MODULES


def test_source_and_tests_do_not_import_removed_root_modules() -> None:
    violations: list[str] = []
    for root in (PACKAGE, Path(__file__).parent):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    if name in _REMOVED_MODULES:
                        violations.append(f"{path}:{node.lineno}: {name}")
    assert violations == []
