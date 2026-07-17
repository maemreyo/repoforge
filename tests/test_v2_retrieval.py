from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.retrieval import SearchMode
from repoforge.domain.errors import SecurityError


def test_workspace_search_returns_structured_literal_and_context_matches(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 structured search")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    (root / "search.txt").write_text("before\nneedle alpha needle\nafter\n", encoding="utf-8")

    result = service.workspace_search_v2(
        workspace_id,
        "needle",
        mode=SearchMode.LITERAL,
        context_lines=1,
    )

    assert result["mode"] == "literal"
    assert len(result["matches"]) == 2
    first = result["matches"][0]
    assert first == {
        "path": "search.txt",
        "line": 2,
        "column": 1,
        "match": "needle",
        "context_before": ["before"],
        "context_after": ["after"],
        "score": 1.0,
        "provider": "builtin_literal",
    }
    assert result["matches"][1]["column"] == 14


def test_regex_and_file_name_modes_are_guarded_and_typed(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 regex search")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    (root / "alpha_test.py").write_text("value_123\nvalue_456\n", encoding="utf-8")

    regex = service.workspace_search_v2(
        workspace_id,
        r"value_[0-9]+",
        mode=SearchMode.REGEX,
    )
    assert [item["match"] for item in regex["matches"]] == ["value_123", "value_456"]
    assert all(item["provider"] == "builtin_regex" for item in regex["matches"])

    names = service.workspace_search_v2(
        workspace_id,
        "alpha",
        mode=SearchMode.FILE_NAME,
    )
    assert names["matches"] == [
        {
            "path": "alpha_test.py",
            "line": None,
            "column": None,
            "match": "alpha_test.py",
            "context_before": [],
            "context_after": [],
            "score": 1.0,
            "provider": "builtin_file_name",
        }
    ]

    with pytest.raises(SecurityError, match="unsafe regex"):
        service.workspace_search_v2(
            workspace_id,
            r"(a+)+$",
            mode=SearchMode.REGEX,
        )


def test_repo_search_is_snapshot_bound_and_structured(forge_env: ForgeEnvironment) -> None:
    result = forge_env.service.repo_search_v2(
        "demo",
        "Repository",
        mode=SearchMode.LITERAL,
        context_lines=1,
    )

    assert len(result["commit_sha"]) == 40
    assert result["matches"] == [
        {
            "path": "README.md",
            "line": 3,
            "column": 1,
            "match": "Repository",
            "context_before": [""],
            "context_after": [],
            "score": 1.0,
            "provider": "builtin_literal",
        }
    ]


def test_tree_supports_subtree_cursor_and_explicit_omitted_count(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 tree cursor")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    for index in range(5):
        path = root / "pkg" / f"file_{index}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")

    first = service.workspace_tree_v2(
        workspace_id,
        subtree="pkg",
        max_entries=2,
        byte_budget=10_000,
    )
    assert len(first["entries"]) == 2
    assert first["omitted_count"] >= 3
    assert first["next_cursor"] is not None
    second = service.workspace_tree_v2(
        workspace_id,
        subtree="pkg",
        max_entries=10,
        byte_budget=10_000,
        cursor=first["next_cursor"],
    )
    all_paths = [item["path"] for item in first["entries"] + second["entries"]]
    assert all_paths == sorted(set(all_paths))
    assert all(path.startswith("pkg/") for path in all_paths)


def test_workspace_diff_returns_structured_files_hunks_and_lines(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 structured diff")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed\nsecond\n",
        hello["sha256"],
    )
    service.workspace_write_file(workspace_id, "new.txt", "new\n", "<new>")

    result = service.workspace_diff_v2(workspace_id, staged=False)

    assert {item["path"] for item in result["files"]} == {"hello.txt", "new.txt"}
    hello_diff = next(item for item in result["files"] if item["path"] == "hello.txt")
    assert hello_diff["status"] == "modified"
    assert hello_diff["additions"] == 2
    assert hello_diff["deletions"] == 1
    assert hello_diff["hunks"][0]["header"].startswith("@@")
    assert {line["kind"] for line in hello_diff["hunks"][0]["lines"]} >= {
        "add",
        "delete",
    }
    new_diff = next(item for item in result["files"] if item["path"] == "new.txt")
    assert new_diff["status"] == "added"
    assert result["change_metrics"]["changed_files"] == 2
    assert result["head_sha"] == service.workspace_status(workspace_id)["head_sha"]


def test_retrieval_cursor_is_bound_to_query_and_scope(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 retrieval cursor")["workspace_id"]
    first = service.workspace_search_v2(
        workspace_id,
        "e",
        max_results=1,
        byte_budget=200,
    )
    assert first["next_cursor"] is not None

    with pytest.raises(ValueError, match=r"cursor.*request"):
        service.workspace_search_v2(
            workspace_id,
            "different",
            cursor=first["next_cursor"],
        )
