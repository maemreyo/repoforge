from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from repoforge.domain.errors import SecurityError
from repoforge.domain.policy import assert_path_allowed
from repoforge.domain.repository_proposal import REMOVABLE_DEFAULT_DENIES, SAFE_DENIED_PATHS


def test_repository_uses_documented_executable_integrity_policy_not_manual_manifest() -> None:
    root = Path(__file__).parents[1]
    assert not (root / "SOURCE_MANIFEST.sha256").exists()

    policy = root / "docs/development/INTEGRITY_POLICY.md"
    text = policy.read_text(encoding="utf-8")
    for required in (
        "scripts/verify-production.sh",
        "release contract",
        "uv.lock",
        "wheel",
        "symlink",
        "line ending",
        "generated artifact",
        "failure",
    ):
        assert required.lower() in text.lower()

    stale_references: list[str] = []
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or ".claude" in path.parts
            or ".venv" in path.parts
            or path == Path(__file__)
            or path.is_relative_to(root / "docs/superpowers/plans")
        ):
            continue
        if path.suffix not in {".md", ".py", ".sh", ".toml"}:
            continue
        if "SOURCE_MANIFEST.sha256" in path.read_text(encoding="utf-8", errors="ignore"):
            stale_references.append(str(path.relative_to(root)))
    assert stale_references == []


def test_tracked_paths_do_not_collide_with_non_removable_policy_denies() -> None:
    root = Path(__file__).parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    repo = SimpleNamespace(
        allowed_paths=(),
        denied_paths=tuple(
            pattern for pattern in SAFE_DENIED_PATHS if pattern not in REMOVABLE_DEFAULT_DENIES
        ),
    )

    violations: list[str] = []
    for path in tracked:
        if not path.endswith(".py"):
            continue
        try:
            assert_path_allowed(path, repo)
        except SecurityError:
            violations.append(path)

    assert violations == []
