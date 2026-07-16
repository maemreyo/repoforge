"""Coverage for `repo_task_context` (#146): a bounded warm-start bundle assembled from the
pure application logic of `repo_context`, `repo_issue_spec`, `workspace_status`, and
`repo_recent_commits`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.repository.task_context import (
    _BUNDLE_HARD_CAP_BYTES,
    _TICKET_SECTION_MAX_BYTES,
    _TRUNCATION_ORDER,
    _bound_ticket_section,
    _encoded_size,
    _enforce_hard_cap,
)
from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import CommandError, WorkspaceError
from repoforge.interfaces.mcp.server import create_server


def _audit_events(root: Path, action: str) -> list[dict[str, Any]]:
    audit_path = root / "state" / "audit.jsonl"
    if not audit_path.is_file():
        return []
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


# ---------------------------------------------------------------------------
# Parity: every section matches the standalone tool's content field-for-field.
# ---------------------------------------------------------------------------


def test_bundle_matches_standalone_tools_field_for_field(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "resume-task")
    workspace_id = created["workspace_id"]

    context_result = service.repo_context("demo")
    service.repo_issue_spec("demo", 55)  # prime the GitHub read cache
    spec_result = service.repo_issue_spec("demo", 55)  # now a cache hit
    status_result = service.workspace_status(workspace_id)
    commits_result = service.repo_recent_commits("demo", limit=5)

    bundle = service.repo_task_context("demo", issue_number=55, workspace_id=workspace_id)

    assert bundle["repo_id"] == "demo"
    assert bundle["issue_number"] == 55
    assert bundle["workspace_id"] == workspace_id
    assert bundle["truncated"] is False
    assert bundle["repository"] == {**context_result, "truncated": False}
    assert spec_result["observed_at"] is None
    assert bundle["ticket"] == {**spec_result, "truncated": False}
    assert bundle["workspace"] == {**status_result, "truncated": False}
    assert bundle["recent_commits"] == {**commits_result, "truncated": False}


# ---------------------------------------------------------------------------
# Explicit-null absence: an omitted issue_number/workspace_id yields null, not an error.
# ---------------------------------------------------------------------------


def test_bundle_omits_absent_sections_as_explicit_null(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service

    bundle = service.repo_task_context("demo")

    assert bundle["issue_number"] is None
    assert bundle["workspace_id"] is None
    assert bundle["ticket"] is None
    assert bundle["workspace"] is None
    assert bundle["repository"]["repo_id"] == "demo"
    assert bundle["recent_commits"]["commits"]


def test_bundle_includes_ticket_without_a_workspace(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service

    bundle = service.repo_task_context("demo", issue_number=12)

    assert bundle["ticket"] is not None
    assert bundle["ticket"]["issue_number"] == 12
    assert bundle["workspace"] is None


def test_bundle_uses_workspace_branch_for_recent_commits(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "workspace commit context")["workspace_id"]
    workspace_path = Path(service.workspace_status(workspace_id)["path"])
    (workspace_path / "workspace-only.txt").write_text("workspace only\n", encoding="utf-8")
    git("add", "workspace-only.txt", cwd=workspace_path)
    git("commit", "-m", "workspace-only context commit", cwd=workspace_path)

    bundle = service.repo_task_context("demo", workspace_id=workspace_id)

    assert bundle["recent_commits"]["commits"][0]["subject"] == "workspace-only context commit"


# ---------------------------------------------------------------------------
# Exactly one audit event per call, carrying per-section durations, and no nested
# audit events from the reused use cases.
# ---------------------------------------------------------------------------


def test_bundle_produces_exactly_one_audit_event_with_section_durations(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "resume-task-2")
    workspace_id = created["workspace_id"]

    service.repo_task_context("demo", issue_number=7, workspace_id=workspace_id)

    events = _audit_events(forge_env.root, "repo_task_context")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["repo_id"] == "demo"
    assert details["issue_number"] == 7
    assert details["workspace_id"] == workspace_id
    assert details["truncated"] is False
    for key in (
        "repository_duration_ms",
        "ticket_duration_ms",
        "workspace_duration_ms",
        "recent_commits_duration_ms",
    ):
        assert isinstance(details[key], (int, float))

    # The reused use cases must not have produced their own audit events.
    assert _audit_events(forge_env.root, "repo_context") == []
    assert _audit_events(forge_env.root, "repo_issue_spec") == []
    assert _audit_events(forge_env.root, "workspace_status") == []
    assert _audit_events(forge_env.root, "repo_recent_commits") == []
    # workspace_create above legitimately audits itself once; nothing else should appear.
    assert len(_audit_events(forge_env.root, "workspace_create")) == 1


def test_bundle_audit_details_omit_duration_for_absent_sections(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service

    service.repo_task_context("demo", issue_number=9)

    events = _audit_events(forge_env.root, "repo_task_context")
    assert len(events) == 1
    details = events[0]["details"]
    assert "ticket_duration_ms" in details
    assert "workspace_duration_ms" not in details
    assert "repository_duration_ms" in details
    assert "recent_commits_duration_ms" in details


# ---------------------------------------------------------------------------
# Workspace/repository mismatch fails closed.
# ---------------------------------------------------------------------------


def _two_repo_service(tmp_path: Path) -> CodingService:
    def _init_repo(name: str) -> Path:
        remote = tmp_path / f"{name}-remote.git"
        git("init", "--bare", str(remote), cwd=tmp_path)
        source = tmp_path / name
        git("clone", str(remote), str(source), cwd=tmp_path)
        git("config", "user.name", "Test User", cwd=source)
        git("config", "user.email", "test@example.com", cwd=source)
        (source / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        git("add", ".", cwd=source)
        git("commit", "-m", "initial", cwd=source)
        git("branch", "-M", "main", cwd=source)
        git("push", "-u", "origin", "main", cwd=source)
        return source

    demo_source = _init_repo("demo")
    other_source = _init_repo("other")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{demo_source}"
default_base = "main"
allowed_base_branches = ["main"]

[repositories.other]
path = "{other_source}"
default_base = "main"
allowed_base_branches = ["main"]
""",
        encoding="utf-8",
    )
    return CodingService(load_config(config_path))


def test_workspace_belonging_to_a_different_repository_fails_closed(tmp_path: Path) -> None:
    service = _two_repo_service(tmp_path)
    created = service.workspace_create("other", "other-task")
    other_workspace_id = created["workspace_id"]

    with pytest.raises(WorkspaceError, match="belongs to repository"):
        service.repo_task_context("demo", workspace_id=other_workspace_id)

    # The correctly paired repo_id/workspace_id combination keeps working.
    result = service.repo_task_context("other", workspace_id=other_workspace_id)
    assert result["workspace"]["workspace_id"] == other_workspace_id


def test_unknown_workspace_fails_closed(forge_env: ForgeEnvironment) -> None:
    with pytest.raises(WorkspaceError, match="Unknown workspace"):
        forge_env.service.repo_task_context("demo", workspace_id="does-not-exist")


# ---------------------------------------------------------------------------
# Stale/unknown issue fails closed with the existing envelope, and the failure is
# still recorded as exactly one audit event.
# ---------------------------------------------------------------------------


class _RaisingGithubGateway:
    """Fake GitHub gateway that fails an unknown issue number like a real `gh` miss."""

    def issue_read(self, cwd: Path, issue_number: int) -> dict[str, Any]:
        del cwd
        if issue_number == 999:
            raise CommandError(f"gh: issue #{issue_number} not found")
        return {
            "number": issue_number,
            "title": "Issue",
            "body": "Body",
            "state": "OPEN",
            "comments": [],
        }

    def pr_read(self, cwd: Path, pr_number: int) -> dict[str, Any]:  # pragma: no cover
        del cwd
        raise NotImplementedError


def test_unknown_issue_fails_closed(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path)
    config = load_config(env.config_path)
    application = build_application(
        config, overrides=AdapterOverrides(github=_RaisingGithubGateway())
    )
    service = CodingService(config, application=application)

    with pytest.raises(CommandError, match="not found"):
        service.repo_task_context("demo", issue_number=999)

    events = _audit_events(tmp_path, "repo_task_context")
    assert len(events) == 1
    assert events[0]["success"] is False


# ---------------------------------------------------------------------------
# Overflow truncation, end to end: an oversized fixture never exceeds the hard cap and
# every section that had to be dropped carries an explicit `truncated: true` flag.
# ---------------------------------------------------------------------------


def test_oversized_fixture_is_truncated_and_never_exceeds_the_hard_cap(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    source = forge_env.source

    # Oversized instruction files: each near repo_context's own preview cap.
    for relative in (
        "AGENTS.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        ".github/copilot-instructions.md",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 20_000, encoding="utf-8")

    # Many root-level files: repo_context's root_files listing has no independent bound, so a
    # large tree alone can push the repository section past the 96 KB bundle hard cap.
    for index in range(6000):
        (source / f"generated-{index:05d}.txt").write_text("x", encoding="utf-8")

    git("add", ".", cwd=source)
    git("commit", "-m", "oversized fixture", cwd=source)

    created = service.workspace_create("demo", "oversized-task")
    workspace_id = created["workspace_id"]

    bundle = service.repo_task_context("demo", issue_number=1, workspace_id=workspace_id)

    assert _encoded_size(bundle) <= _BUNDLE_HARD_CAP_BYTES
    assert bundle["truncated"] is True
    assert bundle["recent_commits"]["truncated"] is True
    assert bundle["ticket"]["truncated"] is True
    assert bundle["workspace"]["truncated"] is True
    assert bundle["repository"]["truncated"] is True
    # The repository identity survives even though its bulky fields were dropped.
    assert bundle["repository"]["repo_id"] == "demo"
    assert bundle["repository"]["root_files"] == []
    assert bundle["repository"]["instruction_files"] == []


# ---------------------------------------------------------------------------
# Pure truncation-order unit tests: prove the documented order and that a section is
# left untouched once the bundle already fits.
# ---------------------------------------------------------------------------


def _fixture_bundle(*, repository_bytes: int, ticket_bytes: int) -> dict[str, Any]:
    return {
        "repo_id": "demo",
        "issue_number": 1,
        "workspace_id": "ws",
        "repository": {
            "repo_id": "demo",
            "root_files": ["x" * repository_bytes],
            "engines": {},
            "scripts": {},
            "instruction_files": [],
            "truncated": False,
        },
        "ticket": {
            "repo_id": "demo",
            "issue_number": 1,
            "comments": ["x" * ticket_bytes],
            "live": {"number": 1, "title": "t", "state": "OPEN", "url": "u"},
            "drift": [],
            "node": None,
            "truncated": False,
        },
        "workspace": {
            "workspace_id": "ws",
            "changed_paths": [],
            "change_metrics": {},
            "issue_ids": [],
            "last_verification": None,
            "truncated": False,
        },
        "recent_commits": {
            "repo_id": "demo",
            "commits": [{"sha": "a", "subject": "s"}],
            "truncated": False,
        },
    }


def test_truncation_order_constant_matches_documented_sequence() -> None:
    assert _TRUNCATION_ORDER == ("recent_commits", "ticket", "workspace", "repository")


def test_enforce_hard_cap_is_a_no_op_when_already_within_bound() -> None:
    bundle = _fixture_bundle(repository_bytes=100, ticket_bytes=100)
    before = json.loads(json.dumps(bundle))

    truncated = _enforce_hard_cap(bundle)

    assert truncated is False
    assert bundle == before


def test_enforce_hard_cap_drops_recent_commits_first_and_stops_once_it_fits() -> None:
    # A small overflow that a single dropped section can absorb.
    bundle = _fixture_bundle(repository_bytes=100, ticket_bytes=100)
    bundle["recent_commits"]["commits"] = [{"sha": "a", "subject": "s" * 200_000}]
    assert _encoded_size(bundle) > _BUNDLE_HARD_CAP_BYTES

    truncated = _enforce_hard_cap(bundle)

    assert truncated is True
    assert bundle["recent_commits"]["truncated"] is True
    assert bundle["ticket"]["truncated"] is False
    assert bundle["workspace"]["truncated"] is False
    assert bundle["repository"]["truncated"] is False
    assert _encoded_size(bundle) <= _BUNDLE_HARD_CAP_BYTES


def test_enforce_hard_cap_cascades_through_every_section_when_necessary() -> None:
    bundle = _fixture_bundle(repository_bytes=200_000, ticket_bytes=200_000)
    bundle["workspace"]["changed_paths"] = ["path"] * 50_000
    assert _encoded_size(bundle) > _BUNDLE_HARD_CAP_BYTES

    truncated = _enforce_hard_cap(bundle)

    assert truncated is True
    for name in _TRUNCATION_ORDER:
        assert bundle[name]["truncated"] is True
    assert _encoded_size(bundle) <= _BUNDLE_HARD_CAP_BYTES


def test_bound_ticket_section_leaves_small_payloads_untouched() -> None:
    payload = {
        "repo_id": "demo",
        "issue_number": 1,
        "comments": [{"body": "small"}],
        "live": {"number": 1, "title": "t", "body": "b", "state": "OPEN"},
        "drift": [],
        "node": None,
    }
    before = json.loads(json.dumps(payload))

    truncated = _bound_ticket_section(payload)

    assert truncated is False
    assert payload == before


def test_bound_ticket_section_shrinks_oversized_payloads() -> None:
    payload = {
        "repo_id": "demo",
        "issue_number": 1,
        "comments": [{"body": "x" * 5_000} for _ in range(10)],
        "live": {"number": 1, "title": "t", "body": "x" * 5_000, "state": "OPEN", "url": "u"},
        "drift": [{"code": "X", "message": "m"}],
        "node": {"number": 1},
    }

    truncated = _bound_ticket_section(payload)

    assert truncated is True
    assert _encoded_size(payload) <= _TICKET_SECTION_MAX_BYTES
    assert payload["comments"] == []
    assert payload["drift"] == []
    assert payload["node"] is None
    assert "body" not in payload["live"]


# ---------------------------------------------------------------------------
# MCP protocol-level coverage through an actual client session.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mcp_repo_task_context_tool(forge_env: ForgeEnvironment) -> None:
    server = create_server(forge_env.config_path)
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        tool = tools["repo_task_context"]
        assert tool.description.startswith("Use this")
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False

        result = await session.call_tool("repo_task_context", {"repo_id": "demo"})
        assert result.isError is False
        structured = result.structuredContent
        assert structured is not None
        assert structured["ticket"] is None
        assert structured["workspace"] is None
        assert structured["repository"]["repo_id"] == "demo"

        error_result = await session.call_tool("repo_task_context", {"repo_id": "missing-repo"})
        assert error_result.isError is True
