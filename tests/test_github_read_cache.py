from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.audit import JsonlAuditSink
from repoforge.adapters.persistence import JsonGitHubReadCache
from repoforge.application.context import ApplicationContext
from repoforge.application.repository.issue_read import IssueReadCommand, IssueReader
from repoforge.application.repository.issue_spec import (
    RepositoryIssueSpecCommand,
    RepositoryIssueSpecReader,
)
from repoforge.application.repository.pr_read import PullRequestReadCommand, PullRequestReader
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig
from repoforge.domain.errors import ConfigError
from repoforge.testing import (
    FixedClock,
    InMemoryLockManager,
    InMemoryOperationGate,
    InMemoryWorkspaceStore,
    SequenceIdGenerator,
)


class CountingGithubGateway:
    """Fake GitHub gateway that records every live `issue_read`/`pr_read` call."""

    def __init__(self) -> None:
        self.issue_calls: list[int] = []
        self.pr_calls: list[int] = []

    def issue_read(self, cwd: Path, issue_number: int) -> dict[str, Any]:
        del cwd
        self.issue_calls.append(issue_number)
        return {
            "number": issue_number,
            "title": f"Issue {issue_number}",
            "body": "Issue body",
            "state": "OPEN",
            "comments": [],
        }

    def pr_read(self, cwd: Path, pr_number: int) -> dict[str, Any]:
        del cwd
        self.pr_calls.append(pr_number)
        return {
            "number": pr_number,
            "title": f"PR {pr_number}",
            "body": "PR body",
            "state": "OPEN",
        }


def _build_ctx(
    tmp_path: Path,
    *,
    github: CountingGithubGateway,
    ttl_seconds: int = 120,
    clock: FixedClock | None = None,
    max_entries: int = 128,
) -> tuple[ApplicationContext, JsonGitHubReadCache]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    (repo_path / ".git").mkdir(exist_ok=True)
    state_root = tmp_path / "state"
    server = ServerConfig(
        tmp_path / "workspaces",
        state_root,
        github_read_cache_ttl_seconds=ttl_seconds,
    )
    config = AppConfig(
        tmp_path / "config.toml",
        server,
        {"demo": RepositoryConfig("demo", repo_path)},
    )
    locks = InMemoryLockManager()
    active_clock = clock or FixedClock("2026-01-01T00:00:00+00:00")
    audit = JsonlAuditSink(state_root, active_clock)
    cache = JsonGitHubReadCache(state_root, locks, max_entries=max_entries)
    ctx = ApplicationContext(
        config=config,
        commands=object(),
        git=object(),
        github=github,
        filesystem=object(),
        store=InMemoryWorkspaceStore(),
        locks=locks,
        gate=InMemoryOperationGate(),
        audit=audit,
        clock=active_clock,
        ids=SequenceIdGenerator(),
        executables=object(),
        github_read_cache=cache,
    )
    return ctx, cache


# --------------------------------------------------------------------------
# Application-layer integration: repo_issue_read / repo_issue_spec / repo_pr_read
# --------------------------------------------------------------------------


def test_issue_read_cache_hit_avoids_live_call(tmp_path: Path) -> None:
    github = CountingGithubGateway()
    ctx, _cache = _build_ctx(tmp_path, github=github)
    reader = IssueReader(ctx)

    first = reader.execute(IssueReadCommand("demo", 42))
    assert github.issue_calls == [42]
    assert "cache_hit" not in first.payload

    second = reader.execute(IssueReadCommand("demo", 42))
    assert github.issue_calls == [42], "second read within TTL must not hit gh again"
    assert second.payload["cache_hit"] is True
    assert second.payload["title"] == "Issue 42"


def test_pr_read_cache_hit_avoids_live_call(tmp_path: Path) -> None:
    github = CountingGithubGateway()
    ctx, _cache = _build_ctx(tmp_path, github=github)
    reader = PullRequestReader(ctx)

    first = reader.execute(PullRequestReadCommand("demo", 7))
    assert github.pr_calls == [7]
    assert "cache_hit" not in first.payload

    second = reader.execute(PullRequestReadCommand("demo", 7))
    assert github.pr_calls == [7]
    assert second.payload["cache_hit"] is True


def test_fresh_forces_live_read_and_refreshes_entry(tmp_path: Path) -> None:
    github = CountingGithubGateway()
    ctx, _cache = _build_ctx(tmp_path, github=github)
    reader = IssueReader(ctx)

    reader.execute(IssueReadCommand("demo", 5))
    assert github.issue_calls == [5]

    forced = reader.execute(IssueReadCommand("demo", 5, fresh=True))
    assert github.issue_calls == [5, 5], "fresh=True must always perform a live read"
    assert "cache_hit" not in forced.payload

    # The forced read refreshed the cache entry, so the next plain read is a hit again.
    refreshed = reader.execute(IssueReadCommand("demo", 5))
    assert github.issue_calls == [5, 5]
    assert refreshed.payload["cache_hit"] is True


def test_ttl_expiry_falls_back_to_live_read(tmp_path: Path) -> None:
    github = CountingGithubGateway()
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    ctx, _cache = _build_ctx(tmp_path, github=github, ttl_seconds=60, clock=clock)
    reader = IssueReader(ctx)

    reader.execute(IssueReadCommand("demo", 9))
    assert github.issue_calls == [9]

    # Still within TTL: served from cache.
    clock.value = "2026-01-01T00:00:30+00:00"
    reader.execute(IssueReadCommand("demo", 9))
    assert github.issue_calls == [9]

    # Past the 60s TTL: falls back to a live read.
    clock.value = "2026-01-01T00:02:00+00:00"
    stale = reader.execute(IssueReadCommand("demo", 9))
    assert github.issue_calls == [9, 9]
    assert "cache_hit" not in stale.payload


def test_issue_spec_shares_cache_with_issue_read(tmp_path: Path) -> None:
    github = CountingGithubGateway()
    ctx, _cache = _build_ctx(tmp_path, github=github)

    IssueReader(ctx).execute(IssueReadCommand("demo", 11))
    assert github.issue_calls == [11]

    spec = RepositoryIssueSpecReader(ctx).execute(RepositoryIssueSpecCommand("demo", 11))
    assert github.issue_calls == [11], "repo_issue_spec must reuse the repo_issue_read cache entry"
    assert spec.cache_hit is True
    assert spec.live["title"] == "Issue 11"


def test_issue_spec_fresh_bypasses_cache(tmp_path: Path) -> None:
    github = CountingGithubGateway()
    ctx, _cache = _build_ctx(tmp_path, github=github)

    IssueReader(ctx).execute(IssueReadCommand("demo", 12))
    assert github.issue_calls == [12]

    spec = RepositoryIssueSpecReader(ctx).execute(
        RepositoryIssueSpecCommand("demo", 12, fresh=True)
    )
    assert github.issue_calls == [12, 12]
    assert spec.cache_hit is False


def test_missing_cache_adapter_still_serves_live_reads(tmp_path: Path) -> None:
    """Without a wired cache adapter, reads must keep working exactly as before."""
    github = CountingGithubGateway()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    state_root = tmp_path / "state"
    config = AppConfig(
        tmp_path / "config.toml",
        ServerConfig(tmp_path / "workspaces", state_root),
        {"demo": RepositoryConfig("demo", repo_path)},
    )
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    ctx = ApplicationContext(
        config=config,
        commands=object(),
        git=object(),
        github=github,
        filesystem=object(),
        store=InMemoryWorkspaceStore(),
        locks=InMemoryLockManager(),
        gate=InMemoryOperationGate(),
        audit=JsonlAuditSink(state_root, clock),
        clock=clock,
        ids=SequenceIdGenerator(),
        executables=object(),
        github_read_cache=None,
    )
    reader = IssueReader(ctx)
    reader.execute(IssueReadCommand("demo", 1))
    reader.execute(IssueReadCommand("demo", 1))
    assert github.issue_calls == [1, 1]


def test_audit_records_success_for_both_hit_and_miss(tmp_path: Path) -> None:
    github = CountingGithubGateway()
    ctx, _cache = _build_ctx(tmp_path, github=github)
    reader = IssueReader(ctx)

    reader.execute(IssueReadCommand("demo", 3))
    reader.execute(IssueReadCommand("demo", 3))

    audit_path = tmp_path / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    reads = [event for event in events if event["action"] == "repo_issue_read"]
    assert len(reads) == 2
    assert all(event["success"] is True for event in reads)
    assert all(event["details"]["issue_number"] == 3 for event in reads)


# --------------------------------------------------------------------------
# Adapter-level unit tests: JsonGitHubReadCache
# --------------------------------------------------------------------------


def test_cache_file_and_directory_are_private(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager())
    cache.put("demo", "issue", 1, {"number": 1}, now_epoch=1_000.0)
    path = tmp_path / "github-read-cache.json"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_cache_get_put_roundtrip(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager())
    cache.put("demo", "issue", 1, {"number": 1, "title": "hello"}, now_epoch=1_000.0)
    result = cache.get("demo", "issue", 1, ttl_seconds=120, now_epoch=1_010.0)
    assert result == {"number": 1, "title": "hello"}


def test_cache_miss_for_unknown_key(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager())
    assert cache.get("demo", "issue", 99, ttl_seconds=120, now_epoch=1_000.0) is None


def test_cache_ttl_expiry_is_a_miss(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager())
    cache.put("demo", "pr", 2, {"number": 2}, now_epoch=1_000.0)
    assert cache.get("demo", "pr", 2, ttl_seconds=60, now_epoch=1_050.0) is not None
    assert cache.get("demo", "pr", 2, ttl_seconds=60, now_epoch=1_061.0) is None


def test_cache_lru_eviction_bounds_entry_count(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager(), max_entries=2)
    cache.put("demo", "issue", 1, {"number": 1}, now_epoch=1_000.0)
    cache.put("demo", "issue", 2, {"number": 2}, now_epoch=1_001.0)
    cache.put("demo", "issue", 3, {"number": 3}, now_epoch=1_002.0)

    # The least-recently-stored entry (1) is evicted to keep the bound of 2.
    assert cache.get("demo", "issue", 1, ttl_seconds=1_000_000, now_epoch=1_002.0) is None
    assert cache.get("demo", "issue", 2, ttl_seconds=1_000_000, now_epoch=1_002.0) is not None
    assert cache.get("demo", "issue", 3, ttl_seconds=1_000_000, now_epoch=1_002.0) is not None


def test_cache_corrupt_file_falls_back_to_miss_without_raising(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager())
    path = tmp_path / "github-read-cache.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert cache.get("demo", "issue", 1, ttl_seconds=120, now_epoch=1_000.0) is None

    # A corrupt file must not prevent future writes from succeeding.
    cache.put("demo", "issue", 1, {"number": 1}, now_epoch=1_000.0)
    assert cache.get("demo", "issue", 1, ttl_seconds=120, now_epoch=1_000.0) == {"number": 1}


def test_cache_malformed_entry_shape_falls_back_to_miss(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager())
    path = tmp_path / "github-read-cache.json"
    path.write_text(
        json.dumps({"version": 1, "entries": {"demo:issue:1": {"payload": "not-a-dict"}}}),
        encoding="utf-8",
    )
    assert cache.get("demo", "issue", 1, ttl_seconds=120, now_epoch=1_000.0) is None

    path.write_text(
        json.dumps({"version": 1, "entries": "not-a-dict"}),
        encoding="utf-8",
    )
    assert cache.get("demo", "issue", 1, ttl_seconds=120, now_epoch=1_000.0) is None


def test_cache_put_skips_oversized_entry_without_raising(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager(), max_entry_bytes=100)
    cache.put("demo", "issue", 1, {"number": 1, "body": "x" * 1_000}, now_epoch=1_000.0)
    assert cache.get("demo", "issue", 1, ttl_seconds=120, now_epoch=1_000.0) is None


def test_cache_put_skips_non_serializable_payload_without_raising(tmp_path: Path) -> None:
    cache = JsonGitHubReadCache(tmp_path, InMemoryLockManager())
    cache.put("demo", "issue", 1, {"bad": object()}, now_epoch=1_000.0)
    assert cache.get("demo", "issue", 1, ttl_seconds=120, now_epoch=1_000.0) is None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_config_accepts_custom_github_read_cache_ttl(tmp_path: Path) -> None:
    from repoforge.config import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
github_read_cache_ttl_seconds = 200

[repositories.demo]
path = "{repo}"
""",
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    assert loaded.server.github_read_cache_ttl_seconds == 200


def test_config_rejects_out_of_range_github_read_cache_ttl(tmp_path: Path) -> None:
    from repoforge.config import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
github_read_cache_ttl_seconds = 30

[repositories.demo]
path = "{repo}"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="github_read_cache_ttl_seconds"):
        load_config(config_path)


def test_server_config_default_github_read_cache_ttl() -> None:
    server = ServerConfig(Path("/tmp/workspaces"), Path("/tmp/state"))
    assert server.github_read_cache_ttl_seconds == 120


# --------------------------------------------------------------------------
# End-to-end through the full wired CodingService (bootstrap + fake gh CLI)
# --------------------------------------------------------------------------


def test_service_repo_issue_read_uses_wired_cache(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service

    first = service.repo_issue_read("demo", 101)
    assert "cache_hit" not in first

    second = service.repo_issue_read("demo", 101)
    assert second.get("cache_hit") is True
    assert second["title"] == first["title"]

    fresh = service.repo_issue_read("demo", 101, fresh=True)
    assert "cache_hit" not in fresh

    cache_path = Path(forge_env.service.config.server.state_root) / "github-read-cache.json"
    assert cache_path.is_file()
    assert os.stat(cache_path).st_mode & 0o777 == 0o600


def test_service_repo_pr_read_uses_wired_cache(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service

    first = service.repo_pr_read("demo", 202)
    assert "cache_hit" not in first

    second = service.repo_pr_read("demo", 202)
    assert second.get("cache_hit") is True
    assert second["title"] == first["title"]


def test_service_repo_issue_spec_uses_wired_cache(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service

    first = service.repo_issue_spec("demo", 303)
    assert first["cache_hit"] is False

    second = service.repo_issue_spec("demo", 303)
    assert second["cache_hit"] is True
