from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.read_batch import FileReadRequest
from repoforge.domain.errors import WorkspaceError


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    path = root / "state" / "audit.jsonl"
    return [
        event
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
        for event in [json.loads(line)]
        if event["action"] == action
    ]


def test_workspace_read_supports_independent_ranges_and_one_audit_event(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 read ranges")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    (root / "a.txt").write_text("a1\na2\na3\na4\n", encoding="utf-8")
    (root / "b.txt").write_text("b1\nb2\nb3\nb4\n", encoding="utf-8")

    result = service.workspace_read(
        workspace_id,
        [
            FileReadRequest("a.txt", 2, 3),
            FileReadRequest("b.txt", 1, 2),
        ],
        byte_budget=60_000,
    )

    assert [item["content"] for item in result["files"]] == [
        "2: a2\n3: a3",
        "1: b1\n2: b2",
    ]
    assert [item["start_line"] for item in result["files"]] == [2, 1]
    assert [item["end_line"] for item in result["files"]] == [3, 2]
    assert result["truncated"] is False
    assert result["next_cursor"] is None
    assert len(_audit_events(forge_env.root, "workspace_read")) == 1
    assert _audit_events(forge_env.root, "workspace_read_file") == []


def test_workspace_read_global_budget_resumes_without_loss_or_duplication(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 read resume")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    long_line = "x" * 1800
    (root / "large.txt").write_text(f"first\n{long_line}\nlast\n", encoding="utf-8")
    request = [FileReadRequest("large.txt", 1, 3)]

    chunks: list[str] = []
    cursor = None
    seen: set[str] = set()
    while True:
        result = service.workspace_read(
            workspace_id,
            request,
            byte_budget=256,
            cursor=cursor,
        )
        chunks.extend(item["content"] for item in result["files"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen
        seen.add(cursor)
        truncated = result["files"][-1]
        assert truncated["truncated"] is True
        assert truncated["omitted_line_range"] is not None
        assert truncated["next_cursor"] == cursor

    assert "".join(chunks) == f"1: first\n2: {long_line}\n3: last"
    assert len(seen) >= 3


def test_workspace_read_cursor_is_bound_to_exact_request(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 read cursor binding")["workspace_id"]
    first = service.workspace_read(
        workspace_id,
        [FileReadRequest("README.md", 1, 3)],
        byte_budget=8,
    )
    assert first["next_cursor"] is not None

    with pytest.raises(WorkspaceError, match=r"cursor.*request"):
        service.workspace_read(
            workspace_id,
            [FileReadRequest("hello.txt", 1, 1)],
            byte_budget=8,
            cursor=first["next_cursor"],
        )


def test_workspace_read_partial_error_is_typed_and_does_not_leak_denied_content(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 read partial error")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    (root / ".env").write_text("SECRET=value\n", encoding="utf-8")

    result = service.workspace_read(
        workspace_id,
        [FileReadRequest("hello.txt"), FileReadRequest(".env")],
    )

    assert result["succeeded"] == 1
    assert result["errors"][0]["path"] == ".env"
    assert "SECRET" not in json.dumps(result)
    assert all(
        not str(value).startswith("/") for value in result.values() if isinstance(value, str)
    )


def test_repo_read_is_snapshot_bound_and_never_returns_host_paths(
    forge_env: ForgeEnvironment,
) -> None:
    result = forge_env.service.repo_read(
        "demo",
        [FileReadRequest("hello.txt", 1, 1), FileReadRequest("README.md", 2, 3)],
    )

    assert result["repo_id"] == "demo"
    assert len(result["commit_sha"]) == 40
    assert result["files"][0]["content"] == "1: hello"
    assert result["files"][1]["content"] == "2: \n3: Repository instructions."
    rendered = json.dumps(result)
    assert str(forge_env.source) not in rendered
    assert str(forge_env.root) not in rendered
    assert len(_audit_events(forge_env.root, "repo_read")) == 1


def test_read_rejects_duplicate_paths_and_invalid_ranges(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 read validation")["workspace_id"]

    with pytest.raises(WorkspaceError, match="duplicate path"):
        service.workspace_read(
            workspace_id,
            [FileReadRequest("hello.txt"), FileReadRequest("hello.txt", 2, 3)],
        )
    with pytest.raises(WorkspaceError, match="end_line"):
        service.workspace_read(
            workspace_id,
            [FileReadRequest("hello.txt", 3, 2)],
        )
