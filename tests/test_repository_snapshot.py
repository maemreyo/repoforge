from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git

from repoforge.adapters.git.cli import GitCliRepository
from repoforge.application.read_batch import FileReadRequest
from repoforge.application.retrieval import SearchMode
from repoforge.config import RepositoryConfig, ServerConfig
from repoforge.domain.errors import ErrorCode, RepoForgeError, SecurityError
from repoforge.ports.command import CommandResult


def test_repository_snapshot_reads_default_and_explicit_commit(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    commit_sha = git("rev-parse", "main", cwd=forge_env.source)

    tree = service.repo_tree("demo")
    assert tree["resolved_ref"] == "refs/heads/main"
    assert tree["commit_sha"] == commit_sha
    assert tree["entries"] == sorted(tree["entries"])
    assert "hello.txt" in tree["entries"]
    assert tree["truncated"] is False

    default_file = service.repo_read_file("demo", "hello.txt")
    assert default_file["resolved_ref"] == "refs/heads/main"
    assert default_file["commit_sha"] == commit_sha
    assert default_file["path"] == "hello.txt"
    assert default_file["content"] == "1: hello"
    assert len(default_file["sha256"]) == 64
    assert default_file["truncated"] is False

    explicit_file = service.repo_read_file("demo", "hello.txt", ref=commit_sha)
    assert explicit_file["resolved_ref"] == commit_sha
    assert explicit_file["commit_sha"] == commit_sha
    assert explicit_file["content"] == "1: hello"

    batch = service.repo_read_files("demo", ["hello.txt", "README.md", "hello.txt"], ref=commit_sha)
    assert batch["resolved_ref"] == commit_sha
    assert batch["commit_sha"] == commit_sha
    assert batch["requested"] == 2
    assert batch["succeeded"] == 2
    assert [item["path"] for item in batch["files"]] == ["hello.txt", "README.md"]
    assert batch["errors"] == []

    search = service.repo_search("demo", "Repository", ref=commit_sha)
    assert search["resolved_ref"] == commit_sha
    assert search["commit_sha"] == commit_sha
    assert search["matches"] == ["README.md:3:Repository instructions."]
    assert search["truncated"] is False


def test_repository_snapshot_ignores_dirty_source_clone(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    commit_sha = git("rev-parse", "main", cwd=forge_env.source)
    (forge_env.source / "hello.txt").write_text("dirty working tree\n", encoding="utf-8")
    (forge_env.source / "untracked.txt").write_text("uncommitted\n", encoding="utf-8")

    tree = service.repo_tree("demo")
    read = service.repo_read_file("demo", "hello.txt")
    search = service.repo_search("demo", "dirty working tree")

    assert tree["commit_sha"] == commit_sha
    assert "untracked.txt" not in tree["entries"]
    assert read["content"] == "1: hello"
    assert search["matches"] == []


def test_repository_snapshot_ordering_and_truncation(forge_env: ForgeEnvironment) -> None:
    for name in ("z-last.txt", "a-first.txt", "m-middle.txt"):
        (forge_env.source / name).write_text(f"needle in {name}\n", encoding="utf-8")
    git("add", ".", cwd=forge_env.source)
    git("commit", "-m", "add ordered snapshot files", cwd=forge_env.source)

    tree = forge_env.service.repo_tree("demo", max_entries=2)
    assert tree["entries"] == sorted(tree["entries"])
    assert len(tree["entries"]) == 2
    assert tree["truncated"] is True

    search = forge_env.service.repo_search("demo", "needle", max_results=2)
    assert search["matches"] == sorted(search["matches"])
    assert len(search["matches"]) == 2
    assert search["truncated"] is True

    long_file = forge_env.source / "long.txt"
    long_file.write_text(
        "\n".join(f"line {index} " + ("x" * 80) for index in range(2_001)) + "\n",
        encoding="utf-8",
    )
    git("add", "long.txt", cwd=forge_env.source)
    git("commit", "-m", "add long file", cwd=forge_env.source)
    bounded = forge_env.service.repo_read_file("demo", "long.txt", end_line=5_000)
    assert bounded["truncated"] is True
    assert "characters omitted" in bounded["content"]


def test_repository_snapshot_rejects_unsafe_objects_and_paths(
    forge_env: ForgeEnvironment,
) -> None:
    (forge_env.source / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (forge_env.source / "binary.dat").write_bytes(b"text\x00binary")
    (forge_env.source / "link.txt").symlink_to("hello.txt")
    git("add", ".env", "binary.dat", "link.txt", cwd=forge_env.source)
    current_head = git("rev-parse", "HEAD", cwd=forge_env.source)
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{current_head},vendor/module",
        cwd=forge_env.source,
    )
    git("commit", "-m", "add unsafe snapshot objects", cwd=forge_env.source)

    with pytest.raises(SecurityError, match="denied"):
        forge_env.service.repo_read_file("demo", ".env")
    with pytest.raises(SecurityError, match="normalized repository-relative"):
        forge_env.service.repo_read_file("demo", "../hello.txt")
    with pytest.raises(SecurityError, match="Binary files"):
        forge_env.service.repo_read_file("demo", "binary.dat")
    with pytest.raises(SecurityError, match="symlink"):
        forge_env.service.repo_read_file("demo", "link.txt")
    with pytest.raises(SecurityError, match="gitlink"):
        forge_env.service.repo_read_file("demo", "vendor/module")

    batch = forge_env.service.repo_read_files("demo", ["hello.txt", ".env", "missing.txt"])
    assert batch["succeeded"] == 1
    assert [error["error_code"] for error in batch["errors"]] == [
        ErrorCode.SECURITY_POLICY_VIOLATION.value,
        ErrorCode.NOT_FOUND.value,
    ]
    assert len({item["commit_sha"] for item in batch["files"]}) == 1


def test_repository_snapshot_rejects_oversized_file_and_batch(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path, max_batch_files=2)
    oversized = env.source / "oversized.txt"
    oversized.write_bytes(b"x" * 2_000_001)
    git("add", "oversized.txt", cwd=env.source)
    git("commit", "-m", "add oversized file", cwd=env.source)

    with pytest.raises(SecurityError, match="max_file_bytes"):
        env.service.repo_read_file("demo", "oversized.txt")
    with pytest.raises(RepoForgeError, match="max_batch_files"):
        env.service.repo_read_files("demo", ["hello.txt", "README.md", "AGENTS.md"])


def test_repository_snapshot_rejects_invalid_refs(forge_env: ForgeEnvironment) -> None:
    commit_sha = git("rev-parse", "main", cwd=forge_env.source)
    tree_sha = git("rev-parse", "main^{tree}", cwd=forge_env.source)
    external_commit = git("commit-tree", tree_sha, "-m", "detached", cwd=forge_env.source)

    cases = [
        ("0" * len(commit_sha), ErrorCode.REPOSITORY_REF_NOT_FOUND),
        (external_commit, ErrorCode.REPOSITORY_REF_EXTERNAL),
        ("origin/main", ErrorCode.REPOSITORY_REF_EXTERNAL),
        ("refs/remotes/origin/main", ErrorCode.REPOSITORY_REF_EXTERNAL),
        ("main~1", ErrorCode.REPOSITORY_REF_DISALLOWED),
    ]
    for ref, expected_code in cases:
        with pytest.raises(RepoForgeError) as exc_info:
            forge_env.service.repo_tree("demo", ref=ref)
        assert exc_info.value.code is expected_code


class _FakeAmbiguousExecutor:
    """Simulates a repository where a short hex prefix genuinely collides between
    two distinct commits. Real SHA-1 collisions cannot be constructed deterministically
    in a fast test, so the collision is faked at the command boundary instead; every
    other resolution path (branch/tag/full-object-id lookups) is made to fail so
    `resolve_snapshot_ref` falls through to the abbreviated-ref path being tested."""

    def __init__(self, prefix: str, colliding_shas: tuple[str, str]) -> None:
        self._prefix = prefix
        self._colliding_shas = colliding_shas

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

    def run(self, argv: list[str], **kwargs: object) -> CommandResult:
        cwd = str(kwargs.get("cwd", ""))
        if argv[:3] == ["git", "rev-parse", f"--disambiguate={self._prefix}"]:
            return CommandResult(tuple(argv), cwd, 0, "\n".join(self._colliding_shas), "")
        if len(argv) >= 3 and argv[-1].removesuffix("^{commit}") in self._colliding_shas:
            sha = argv[-1].removesuffix("^{commit}")
            return CommandResult(tuple(argv), cwd, 0, sha, "")
        return CommandResult(tuple(argv), cwd, 1, "", "not found")


def test_repository_snapshot_still_rejects_a_truly_ambiguous_abbreviated_sha() -> None:
    """A short hex prefix that resolves to more than one distinct commit must still
    fail closed (#445): the fix is accepting an *unambiguous* abbreviation, not
    accepting every abbreviation."""
    prefix = "abcd"
    colliding_shas = ("a" * 40, "b" * 40)
    executor = _FakeAmbiguousExecutor(prefix, colliding_shas)
    server = ServerConfig(workspace_root=Path("/tmp/ws"), state_root=Path("/tmp/state"))
    repo = RepositoryConfig(repo_id="demo", path=Path("/tmp/repo"))
    git_repo = GitCliRepository(executor, server)  # type: ignore[arg-type]

    with pytest.raises(RepoForgeError) as exc_info:
        git_repo.resolve_snapshot_ref(repo.path, repo, prefix)
    assert exc_info.value.code is ErrorCode.REPOSITORY_REF_AMBIGUOUS


def test_unmerged_local_branch_is_readable_by_name_with_operator_local_provenance(
    forge_env: ForgeEnvironment,
) -> None:
    """#445 AC: a local feature branch not in allowed_base_branches is readable by
    name via repo_read/repo_tree/repo_search, diffable via repo_history compare,
    every payload carries its provenance, and no workspace/worktree is created."""
    service = forge_env.service
    before_workspaces = len(service.workspace_list()["workspaces"])

    git("checkout", "-b", "feature/local-work", cwd=forge_env.source)
    (forge_env.source / "feature.txt").write_text("feature content\n", encoding="utf-8")
    git("add", "feature.txt", cwd=forge_env.source)
    git("commit", "-m", "unmerged local work", cwd=forge_env.source)
    feature_sha = git("rev-parse", "feature/local-work", cwd=forge_env.source)
    main_sha = git("rev-parse", "main", cwd=forge_env.source)
    git("checkout", "main", cwd=forge_env.source)

    tree = service.repo_tree_v2("demo", ref="feature/local-work")
    assert tree["resolved_ref"] == "refs/heads/feature/local-work"
    assert tree["commit_sha"] == feature_sha
    assert tree["provenance"] == "operator_local"
    assert "feature.txt" in [entry["path"] for entry in tree["entries"]]

    # Same commit, addressed by full SHA instead of branch name -- exercises the
    # ancestry-based _commit_is_operator_local classification path, not the
    # unconditional branch-name-resolution path exercised above.
    tree_by_sha = service.repo_tree_v2("demo", ref=feature_sha)
    assert tree_by_sha["resolved_ref"] == feature_sha
    assert tree_by_sha["commit_sha"] == feature_sha
    assert tree_by_sha["provenance"] == "operator_local"

    read = service.repo_read("demo", [FileReadRequest("feature.txt")], ref="feature/local-work")
    assert read["commit_sha"] == feature_sha
    assert read["provenance"] == "operator_local"
    assert read["files"][0]["content"] == "1: feature content"

    search = service.repo_search_v2(
        "demo", "feature content", mode=SearchMode.LITERAL, ref="feature/local-work"
    )
    assert search["commit_sha"] == feature_sha
    assert search["provenance"] == "operator_local"

    history = service.repo_history_v2(
        "demo", mode="compare", base_ref="main", head_ref="feature/local-work"
    )
    comparison = history["comparison"]
    assert comparison["base_sha"] == main_sha
    assert comparison["base_provenance"] == "reviewed_base"
    assert comparison["head_sha"] == feature_sha
    assert comparison["head_provenance"] == "operator_local"
    assert any(item["path"] == "feature.txt" for item in comparison["files"])

    commit = service.repo_history_v2("demo", mode="commit", ref="feature/local-work")["commit"]
    assert commit["sha"] == feature_sha
    assert commit["provenance"] == "operator_local"

    assert len(service.workspace_list()["workspaces"]) == before_workspaces


def test_repository_history_log_mode_reports_reviewed_base_provenance(
    forge_env: ForgeEnvironment,
) -> None:
    history = forge_env.service.repo_history_v2("demo", mode="log", limit=5)
    assert history["commits"]
    assert all(commit["provenance"] == "reviewed_base" for commit in history["commits"])


def test_repository_read_tree_search_report_reviewed_base_provenance_on_main(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    assert service.repo_tree_v2("demo")["provenance"] == "reviewed_base"
    assert (
        service.repo_read("demo", [FileReadRequest("hello.txt")])["provenance"] == "reviewed_base"
    )
    assert (
        service.repo_search_v2("demo", "Repository", mode=SearchMode.LITERAL)["provenance"]
        == "reviewed_base"
    )


def test_repository_snapshot_accepts_an_unambiguous_abbreviated_sha(
    forge_env: ForgeEnvironment,
) -> None:
    """#445 AC: an abbreviated SHA that resolves to exactly one object is accepted,
    not blanket-rejected."""
    commit_sha = git("rev-parse", "main", cwd=forge_env.source)
    tree = forge_env.service.repo_tree_v2("demo", ref=commit_sha[:12])
    assert tree["commit_sha"] == commit_sha
    assert tree["resolved_ref"] == commit_sha
    assert tree["provenance"] == "reviewed_base"


def test_repository_snapshot_v2_still_rejects_remote_refs(
    forge_env: ForgeEnvironment,
) -> None:
    """#445 AC: refs/remotes/* and <remote>/* remain rejected through the v2 surface too."""
    for ref in ("origin/main", "refs/remotes/origin/main"):
        with pytest.raises(RepoForgeError) as exc_info:
            forge_env.service.repo_tree_v2("demo", ref=ref)
        assert exc_info.value.code is ErrorCode.REPOSITORY_REF_EXTERNAL


def test_repository_search_context_lines_returns_surrounding_lines(
    forge_env: ForgeEnvironment,
) -> None:
    (forge_env.source / "ctx.txt").write_text(
        "alpha\nbravo\nNEEDLE charlie\ndelta\necho\n", encoding="utf-8"
    )
    git("add", "ctx.txt", cwd=forge_env.source)
    git("commit", "-m", "add context fixture", cwd=forge_env.source)

    result = forge_env.service.repo_search("demo", "NEEDLE", context_lines=2)
    assert result["matches"] == [
        "ctx.txt-1-alpha",
        "ctx.txt-2-bravo",
        "ctx.txt:3:NEEDLE charlie",
        "ctx.txt-4-delta",
        "ctx.txt-5-echo",
    ]
    assert result["truncated"] is False


def test_repository_search_context_lines_bounds_and_truncation(
    forge_env: ForgeEnvironment,
) -> None:
    (forge_env.source / "ctx2.txt").write_text("line1\nNEEDLE x\nline3\n", encoding="utf-8")
    git("add", "ctx2.txt", cwd=forge_env.source)
    git("commit", "-m", "add second context fixture", cwd=forge_env.source)

    with pytest.raises(ValueError, match="context_lines"):
        forge_env.service.repo_search("demo", "NEEDLE", context_lines=6)
    with pytest.raises(ValueError, match="context_lines"):
        forge_env.service.repo_search("demo", "NEEDLE", context_lines=-1)

    truncated = forge_env.service.repo_search(
        "demo", "NEEDLE", context_lines=1, max_results=2, path_glob="ctx2.txt"
    )
    assert truncated["matches"] == ["ctx2.txt-1-line1", "ctx2.txt:2:NEEDLE x"]
    assert truncated["truncated"] is True


def test_repository_search_context_lines_never_leaks_denied_path(
    forge_env: ForgeEnvironment,
) -> None:
    (forge_env.source / ".env").write_text(
        "before secret\nNEEDLE_BOUNDARY=denied\nafter secret\n", encoding="utf-8"
    )
    (forge_env.source / "allowed_neighbor.txt").write_text(
        "line one\nNEEDLE_BOUNDARY here\nline three\n", encoding="utf-8"
    )
    git("add", "-A", cwd=forge_env.source)
    git("commit", "-m", "add denied/allowed boundary fixture", cwd=forge_env.source)

    result = forge_env.service.repo_search("demo", "NEEDLE_BOUNDARY", context_lines=1)
    assert all(".env" not in match for match in result["matches"])
    assert all("secret" not in match for match in result["matches"])
    assert result["matches"] == [
        "allowed_neighbor.txt-1-line one",
        "allowed_neighbor.txt:2:NEEDLE_BOUNDARY here",
        "allowed_neighbor.txt-3-line three",
    ]


def test_repository_search_default_context_lines_is_contract_stable(
    forge_env: ForgeEnvironment,
) -> None:
    commit_sha = git("rev-parse", "main", cwd=forge_env.source)
    default_call = forge_env.service.repo_search("demo", "Repository", ref=commit_sha)
    explicit_zero = forge_env.service.repo_search(
        "demo", "Repository", ref=commit_sha, context_lines=0
    )
    assert explicit_zero == default_call
    assert default_call["matches"] == ["README.md:3:Repository instructions."]
