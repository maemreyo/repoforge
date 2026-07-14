from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git

from repoforge.adapters.git.cli import GitCliRepository
from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError


def _commit_change(env: ForgeEnvironment) -> tuple[str, str]:
    base = git("rev-parse", "HEAD", cwd=env.source)
    git("mv", "hello.txt", "greeting.txt", cwd=env.source)
    (env.source / "README.md").write_text(
        "# Demo\n\nRepository instructions changed.\n", encoding="utf-8"
    )
    (env.source / "binary.dat").write_bytes(b"\x00\x01\x02")
    git("add", "-A", cwd=env.source)
    git("commit", "-m", "change repository evidence", cwd=env.source)
    head = git("rev-parse", "HEAD", cwd=env.source)
    git("tag", "v1.0.0", head, cwd=env.source)
    return base, head


def test_commit_read_resolves_branch_tag_and_sha_with_typed_files(
    forge_env: ForgeEnvironment,
) -> None:
    base, head = _commit_change(forge_env)
    service = forge_env.service

    branch = service.repo_commit_read("demo", "main", max_files=20, include_patch=True)
    tag = service.repo_commit_read("demo", "v1.0.0", max_files=20, include_patch=True)
    sha = service.repo_commit_read("demo", head, max_files=20, include_patch=True)

    assert branch["requested_ref"] == "main"
    assert branch["resolved_ref"] == "refs/heads/main"
    assert tag["resolved_ref"] == "refs/tags/v1.0.0"
    assert sha["resolved_ref"] == head
    assert {branch["commit_sha"], tag["commit_sha"], sha["commit_sha"]} == {head}
    assert branch["parent_shas"] == [base]
    assert branch["comparison_parent_sha"] == base
    assert branch["subject"] == "change repository evidence"
    assert branch["author"]["email"] == "test@example.com"
    assert branch["committer"]["name"] == "Test User"
    assert len(branch["tree_sha"]) in {40, 64}

    files = {item["path"]: item for item in branch["files"]}
    assert list(files) == sorted(files)
    assert files["greeting.txt"]["status"] == "renamed"
    assert files["greeting.txt"]["previous_path"] == "hello.txt"
    assert files["greeting.txt"]["binary"] is False
    assert files["README.md"]["status"] == "modified"
    assert files["binary.dat"]["status"] == "added"
    assert files["binary.dat"]["binary"] is True
    assert files["binary.dat"]["additions"] is None
    assert branch["binary_files"] == 1
    assert branch["binary_patch_omitted"] is True
    assert "greeting.txt" in branch["patch"]
    assert "README.md" in branch["patch"]
    assert "binary.dat" not in branch["patch"]
    assert branch["patch_truncated"] is False
    assert branch["files_truncated"] is False


def test_commit_read_omits_denied_paths_and_ignores_dirty_clone(
    forge_env: ForgeEnvironment,
) -> None:
    (forge_env.source / ".env").write_text("SECRET=do-not-return\n", encoding="utf-8")
    (forge_env.source / "visible.txt").write_text("committed\n", encoding="utf-8")
    git("add", ".env", "visible.txt", cwd=forge_env.source)
    git("commit", "-m", "add visible and denied paths", cwd=forge_env.source)
    head = git("rev-parse", "HEAD", cwd=forge_env.source)

    (forge_env.source / "visible.txt").write_text("dirty working tree\n", encoding="utf-8")
    (forge_env.source / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    result = forge_env.service.repo_commit_read("demo", head, include_patch=True)

    assert [item["path"] for item in result["files"]] == ["visible.txt"]
    assert result["omitted_paths"] == 1
    assert ".env" not in result["patch"]
    assert "do-not-return" not in result["patch"]
    assert "dirty working tree" not in result["patch"]
    assert "untracked.txt" not in result["patch"]


def test_compare_returns_merge_base_ahead_behind_glob_and_bounds(
    forge_env: ForgeEnvironment,
) -> None:
    base, head = _commit_change(forge_env)

    result = forge_env.service.repo_compare(
        "demo",
        base,
        head,
        path_glob="*.txt",
        max_files=1,
        include_patch=True,
    )

    assert result["base_sha"] == base
    assert result["head_sha"] == head
    assert result["merge_base_sha"] == base
    assert result["ahead"] == 1
    assert result["behind"] == 0
    assert result["path_glob"] == "*.txt"
    assert result["total_files"] == 1
    assert result["returned_files"] == 1
    assert result["files_truncated"] is False
    assert result["files"][0]["path"] == "greeting.txt"
    assert result["files"][0]["previous_path"] == "hello.txt"
    assert "greeting.txt" in result["patch"]
    assert "README.md" not in result["patch"]
    assert "binary.dat" not in result["patch"]

    all_files = forge_env.service.repo_compare("demo", base, head, max_files=2)
    assert all_files["returned_files"] == 2
    assert all_files["total_files"] == 3
    assert all_files["files_truncated"] is True
    assert all_files["patch"] is None


def test_commit_read_handles_root_empty_and_merge_commits(
    forge_env: ForgeEnvironment,
) -> None:
    initial = git("rev-list", "--max-parents=0", "HEAD", cwd=forge_env.source)
    root = forge_env.service.repo_commit_read("demo", initial)
    assert root["parent_shas"] == []
    assert root["comparison_parent_sha"] is None
    assert root["total_files"] >= 1

    git("commit", "--allow-empty", "-m", "empty change", cwd=forge_env.source)
    empty_sha = git("rev-parse", "HEAD", cwd=forge_env.source)
    empty = forge_env.service.repo_commit_read("demo", empty_sha)
    assert empty["files"] == []
    assert empty["total_files"] == 0

    first_parent = git("rev-parse", "HEAD", cwd=forge_env.source)
    git("checkout", "-b", "feature", cwd=forge_env.source)
    (forge_env.source / "feature.txt").write_text("feature\n", encoding="utf-8")
    git("add", "feature.txt", cwd=forge_env.source)
    git("commit", "-m", "feature commit", cwd=forge_env.source)
    git("checkout", "main", cwd=forge_env.source)
    (forge_env.source / "main.txt").write_text("main\n", encoding="utf-8")
    git("add", "main.txt", cwd=forge_env.source)
    git("commit", "-m", "main commit", cwd=forge_env.source)
    merge_first_parent = git("rev-parse", "HEAD", cwd=forge_env.source)
    git("merge", "--no-ff", "feature", "-m", "merge feature", cwd=forge_env.source)
    merge_sha = git("rev-parse", "HEAD", cwd=forge_env.source)

    merge = forge_env.service.repo_commit_read("demo", merge_sha)
    assert len(merge["parent_shas"]) == 2
    assert merge["comparison_parent_sha"] == merge_first_parent
    assert [item["path"] for item in merge["files"]] == ["feature.txt"]
    assert first_parent in git("rev-list", merge_sha, cwd=forge_env.source)


def test_compare_rejects_unrelated_history_and_invalid_inputs(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path)
    git("checkout", "--orphan", "other", cwd=env.source)
    git("rm", "-rf", ".", cwd=env.source)
    (env.source / "other.txt").write_text("other history\n", encoding="utf-8")
    git("add", "other.txt", cwd=env.source)
    git("commit", "-m", "unrelated root", cwd=env.source)
    git("checkout", "main", cwd=env.source)

    text = env.config_path.read_text(encoding="utf-8").replace(
        'allowed_base_branches = ["main"]',
        'allowed_base_branches = ["main", "other"]',
    )
    env.config_path.write_text(text, encoding="utf-8")
    service = CodingService(load_config(env.config_path))

    with pytest.raises(RepoForgeError) as unrelated:
        service.repo_compare("demo", "main", "other")
    assert unrelated.value.code is ErrorCode.REPOSITORY_HISTORIES_UNRELATED

    for limit in (0, 501):
        with pytest.raises(RepoForgeError) as invalid_limit:
            service.repo_commit_read("demo", "main", max_files=limit)
        assert invalid_limit.value.code is ErrorCode.REPOSITORY_EVIDENCE_LIMIT_INVALID

    with pytest.raises(RepoForgeError) as unsafe_glob:
        service.repo_compare("demo", "main", "main", path_glob="../*.py")
    assert unsafe_glob.value.code is ErrorCode.SECURITY_POLICY_VIOLATION


def test_commit_read_redacts_sensitive_commit_metadata(forge_env: ForgeEnvironment) -> None:
    git("config", "user.name", "token=author-secret", cwd=forge_env.source)
    git("config", "user.email", "author@example.com", cwd=forge_env.source)
    git(
        "commit",
        "--allow-empty",
        "-m",
        "token=super-secret-token",
        "-m",
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
        cwd=forge_env.source,
    )
    result = forge_env.service.repo_commit_read("demo", "main")

    assert result["subject"] == "token=<redacted>"
    assert result["body"] == "<redacted:private-key>"
    assert result["author"]["name"] == "token=<redacted>"
    assert result["committer"]["name"] == "token=<redacted>"
    assert result["message_redacted"] is True
    assert result["identity_redacted"] is True


def test_commit_read_omits_denied_rename_symlink_and_gitlink(
    forge_env: ForgeEnvironment,
) -> None:
    (forge_env.source / ".env").write_text("SECRET=withheld\n", encoding="utf-8")
    (forge_env.source / "delete-me.txt").write_text("delete me\n", encoding="utf-8")
    git("add", ".env", "delete-me.txt", cwd=forge_env.source)
    git("commit", "-m", "prepare policy evidence", cwd=forge_env.source)
    parent = git("rev-parse", "HEAD", cwd=forge_env.source)

    git("mv", ".env", "visible-secret.txt", cwd=forge_env.source)
    git("rm", "delete-me.txt", cwd=forge_env.source)
    (forge_env.source / "link.txt").symlink_to("hello.txt")
    git("add", "link.txt", cwd=forge_env.source)
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{parent},vendor/module",
        cwd=forge_env.source,
    )
    git("commit", "-m", "exercise policy filtering", cwd=forge_env.source)

    result = forge_env.service.repo_commit_read("demo", "main", include_patch=True)

    assert result["files"] == [
        {
            "status": "deleted",
            "path": "delete-me.txt",
            "previous_path": None,
            "additions": 0,
            "deletions": 1,
            "binary": False,
        }
    ]
    assert result["omitted_paths"] == 3
    assert "visible-secret.txt" not in result["patch"]
    assert ".env" not in result["patch"]
    assert "withheld" not in result["patch"]
    assert "link.txt" not in result["patch"]
    assert "vendor/module" not in result["patch"]


def test_commit_patch_output_is_bounded(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path)
    config_text = env.config_path.read_text(encoding="utf-8").replace(
        "max_batch_files = 20",
        "max_batch_files = 20\nmax_tool_output_chars = 240",
    )
    env.config_path.write_text(config_text, encoding="utf-8")
    service = CodingService(load_config(env.config_path))
    (env.source / "large.txt").write_text(
        "\n".join(f"line {index}: " + ("x" * 80) for index in range(200)) + "\n",
        encoding="utf-8",
    )
    git("add", "large.txt", cwd=env.source)
    git("commit", "-m", "add large patch", cwd=env.source)

    result = service.repo_commit_read("demo", "main", include_patch=True)

    assert result["patch_truncated"] is True
    assert "characters omitted" in result["patch"]
    assert len(result["patch"]) < 400


def test_commit_parser_rejects_malformed_records_and_unknown_status() -> None:
    for operation in (
        lambda: GitCliRepository._parse_raw_diff(b"malformed\x00path\x00"),
        lambda: GitCliRepository._parse_numstat(b"not-a-numstat\x00"),
        lambda: GitCliRepository._status_name("X"),
    ):
        with pytest.raises(RepoForgeError) as rejected:
            operation()
        assert rejected.value.code is ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED


def test_explicit_missing_tag_and_oversized_ref_are_rejected(
    forge_env: ForgeEnvironment,
) -> None:
    with pytest.raises(RepoForgeError) as missing_tag:
        forge_env.service.repo_commit_read("demo", "refs/tags/missing")
    assert missing_tag.value.code is ErrorCode.REPOSITORY_REF_NOT_FOUND

    with pytest.raises(RepoForgeError) as oversized:
        forge_env.service.repo_commit_read("demo", "x" * 257)
    assert oversized.value.code is ErrorCode.REPOSITORY_REF_DISALLOWED


def test_tag_outside_reviewed_history_is_rejected(forge_env: ForgeEnvironment) -> None:
    main = git("rev-parse", "main", cwd=forge_env.source)
    tree = git("rev-parse", "main^{tree}", cwd=forge_env.source)
    external = git("commit-tree", tree, "-m", "external tagged commit", cwd=forge_env.source)
    git("tag", "external-tag", external, cwd=forge_env.source)

    with pytest.raises(RepoForgeError) as rejected:
        forge_env.service.repo_commit_read("demo", "external-tag")
    assert rejected.value.code is ErrorCode.REPOSITORY_REF_EXTERNAL
    assert git("rev-parse", "main", cwd=forge_env.source) == main
