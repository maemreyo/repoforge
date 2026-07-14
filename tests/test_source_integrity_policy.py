from __future__ import annotations

from pathlib import Path


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
            or path == Path(__file__)
            or path.is_relative_to(root / "docs/superpowers/plans")
        ):
            continue
        if path.suffix not in {".md", ".py", ".sh", ".toml"}:
            continue
        if "SOURCE_MANIFEST.sha256" in path.read_text(encoding="utf-8", errors="ignore"):
            stale_references.append(str(path.relative_to(root)))
    assert stale_references == []
