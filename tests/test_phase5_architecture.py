from __future__ import annotations
import ast
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "repoforge"


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update((alias.name for alias in node.names))
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_domain_is_pure() -> None:
    forbidden = {
        "subprocess",
        "fcntl",
        "repoforge.adapters",
        "adapters",
        "runner",
        "state",
        "audit",
        "service",
    }
    for path in (PACKAGE / "domain").glob("*.py"):
        assert not any(
            (item in name for name in imports(path) for item in forbidden)
        ), path


def test_application_depends_on_ports_not_adapters() -> None:
    for path in (PACKAGE / "application").rglob("*.py"):
        names = imports(path)
        assert not any(("adapters" in name for name in names)), path
        assert "subprocess" not in names and "fcntl" not in names, path


def test_service_is_a_delegating_facade() -> None:
    text = (PACKAGE / "service.py").read_text(encoding="utf-8")
    for forbidden in (
        "subprocess",
        "fcntl",
        "git apply",
        "git push",
        "gh pr",
        "os.replace",
    ):
        assert forbidden not in text
    expected = {
        "create",
        "list",
        "status",
        "tree",
        "file_read",
        "files_read",
        "search",
        "file_write",
        "replace_text",
        "apply_patch",
        "restore_paths",
        "diff",
        "run_profile",
        "verify",
        "commit",
        "push",
        "create_draft_pr",
        "update_draft_pr",
        "pr_status",
        "pr_checks",
        "remove",
    }
    assert expected.issubset(
        {path.stem for path in (PACKAGE / "application" / "workspace").glob("*.py")}
    )


def test_bootstrap_is_composition_root() -> None:
    assert "adapters" in (PACKAGE / "bootstrap.py").read_text(encoding="utf-8")
    for path in [
        PACKAGE / "service.py",
        *(PACKAGE / "application").rglob("*.py"),
        *(PACKAGE / "domain").rglob("*.py"),
    ]:
        assert "from .adapters" not in path.read_text(encoding="utf-8")
