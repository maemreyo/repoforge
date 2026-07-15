from __future__ import annotations

import pytest
from conftest import ForgeEnvironment

from repoforge.domain.errors import ConfigError, WorkspaceError


def test_workspace_create_links_issue_ids(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "issue linkage", None, None, ("42", "#43"))
    assert created["issue_ids"] == ["42", "#43"]
    workspace_id = created["workspace_id"]

    listed = service.workspace_list()["workspaces"]
    entry = next(item for item in listed if item["workspace_id"] == workspace_id)
    assert entry["issue_ids"] == ["42", "#43"]
    assert entry["dirty"] is False

    status = service.workspace_status(workspace_id)
    assert status["issue_ids"] == ["42", "#43"]


def test_workspace_create_without_issue_ids_defaults_empty(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "no issue linkage")
    assert created["issue_ids"] == []
    workspace_id = created["workspace_id"]

    listed = service.workspace_list()["workspaces"]
    entry = next(item for item in listed if item["workspace_id"] == workspace_id)
    assert entry["issue_ids"] == []


def test_workspace_list_reports_dirty_state(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "dirty state")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed\n", hello["sha256"])

    listed = service.workspace_list()["workspaces"]
    entry = next(item for item in listed if item["workspace_id"] == workspace_id)
    assert entry["dirty"] is True


def test_workspace_create_rejects_too_many_issue_ids(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    too_many = tuple(str(i) for i in range(17))
    with pytest.raises(WorkspaceError, match="at most 16 entries"):
        service.workspace_create("demo", "too many issues", None, None, too_many)


def test_workspace_create_rejects_empty_issue_id(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    with pytest.raises(WorkspaceError, match="non-empty"):
        service.workspace_create("demo", "empty issue id", None, None, ("42", "  "))


def test_workspace_create_rejects_overlong_issue_id(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    with pytest.raises(WorkspaceError, match="at most 64 characters"):
        service.workspace_create("demo", "overlong issue id", None, None, ("x" * 65,))


def test_workspace_create_idempotency_conflict_on_different_issue_ids(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    key = "shared-key-issue-ids"
    first = service.workspace_create("demo", "stacked issue", None, key, ("42",))
    assert first["issue_ids"] == ["42"]

    with pytest.raises(ConfigError, match="IDEMPOTENCY_CONFLICT"):
        service.workspace_create("demo", "stacked issue", None, key, ("43",))

    replay = service.workspace_create("demo", "stacked issue", None, key, ("42",))
    assert replay["workspace_id"] == first["workspace_id"]
    assert replay["issue_ids"] == ["42"]
