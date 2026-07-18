"""Coverage for nested AGENTS.md / AGENTS.override.md advisory resolution (#205)."""

from __future__ import annotations

from pathlib import Path

from repoforge.application.skills.agents_md import (
    discover_advisory_documents,
    resolve_advisory_for_path,
)


def test_root_agents_md_applies_repo_wide(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root guidance", encoding="utf-8")
    documents = discover_advisory_documents(tmp_path)
    resolved = resolve_advisory_for_path(documents, "src/anything.py")
    assert resolved is not None
    assert resolved.content == "root guidance"


def test_nested_agents_md_is_scoped_to_its_own_subtree(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root guidance", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "AGENTS.md").write_text("pkg guidance", encoding="utf-8")

    documents = discover_advisory_documents(tmp_path)
    assert resolve_advisory_for_path(documents, "pkg/module.py").content == "pkg guidance"
    assert resolve_advisory_for_path(documents, "other/module.py").content == "root guidance"


def test_agents_override_in_pkg_sub_wins_only_for_pkg_sub(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root guidance", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "AGENTS.md").write_text("pkg guidance", encoding="utf-8")
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "AGENTS.override.md").write_text("sub override guidance", encoding="utf-8")

    documents = discover_advisory_documents(tmp_path)

    assert (
        resolve_advisory_for_path(documents, "pkg/sub/module.py").content == "sub override guidance"
    )
    # pkg/** outside of pkg/sub/** still uses the parent's plain AGENTS.md.
    assert resolve_advisory_for_path(documents, "pkg/other.py").content == "pkg guidance"
    assert resolve_advisory_for_path(documents, "elsewhere/x.py").content == "root guidance"


def test_override_in_the_same_directory_as_a_plain_agents_md_wins(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "AGENTS.md").write_text("plain", encoding="utf-8")
    (pkg / "AGENTS.override.md").write_text("override", encoding="utf-8")

    documents = discover_advisory_documents(tmp_path)
    matching = [d for d in documents if d.directory == "pkg"]
    assert len(matching) == 1
    assert matching[0].content == "override"
    assert matching[0].is_override is True


def test_no_matching_document_returns_none_when_no_root_file_exists(tmp_path: Path) -> None:
    documents = discover_advisory_documents(tmp_path)
    assert resolve_advisory_for_path(documents, "anything.py") is None


def test_claude_md_and_contributing_md_are_also_ingested_as_root_advisory(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("claude guidance", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("contributing guidance", encoding="utf-8")
    documents = discover_advisory_documents(tmp_path)
    paths = {d.path for d in documents}
    assert paths == {"CLAUDE.md", "CONTRIBUTING.md"}
