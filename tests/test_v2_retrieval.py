from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application import retrieval as retrieval_module
from repoforge.application.retrieval import SearchMode, StructuredSearchMatch, paginate
from repoforge.contracts.registry import V2_TOOL_SPECS
from repoforge.domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError


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
    (root / "space name.py").write_text("prefix value_789 suffix\n", encoding="utf-8")

    regex = service.workspace_search_v2(
        workspace_id,
        r"value_[0-9]+",
        mode=SearchMode.REGEX,
    )
    assert [item["match"] for item in regex["matches"]] == [
        "value_123",
        "value_456",
        "value_789",
    ]
    assert all(item["provider"] == "git_grep_regex" for item in regex["matches"])
    spaced = next(item for item in regex["matches"] if item["path"] == "space name.py")
    assert spaced["line"] == 1
    assert spaced["column"] == 8

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

    nested = root / "pkg" / "nested" / "child.py"
    nested.parent.mkdir()
    nested.write_text("VALUE = 99\n", encoding="utf-8")
    complete = service.workspace_tree_v2(
        workspace_id,
        subtree="pkg",
        max_entries=20,
        byte_budget=20_000,
    )
    directory = next(item for item in complete["entries"] if item["path"] == "pkg/nested")
    assert directory == {"path": "pkg/nested", "kind": "directory", "size_bytes": None}


def test_workspace_diff_defaults_to_summaries_and_supports_opt_in_hunks(
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

    summary = service.workspace_diff_v2(workspace_id, staged=False)

    assert {item["path"] for item in summary["files"]} == {"hello.txt", "new.txt"}
    assert all(item["hunks"] == [] for item in summary["files"])
    hello_summary = next(item for item in summary["files"] if item["path"] == "hello.txt")
    assert hello_summary["status"] == "modified"
    assert (hello_summary["additions"], hello_summary["deletions"]) == (2, 1)
    new_summary = next(item for item in summary["files"] if item["path"] == "new.txt")
    assert new_summary["status"] == "added"
    assert summary["change_metrics"]["changed_files"] == 2
    assert set(summary["change_metrics"]) == {
        "changed_files",
        "added_lines",
        "deleted_lines",
        "diff_lines",
        "total_current_bytes",
        "within_limits",
    }
    assert summary["head_sha"] == service.workspace_status(workspace_id)["head_sha"]

    detailed = service.workspace_diff_v2(
        workspace_id,
        staged=False,
        path_glob="hello.txt",
        max_files=1,
        include_hunks=True,
    )
    hello_diff = detailed["files"][0]
    assert hello_diff["hunks"][0]["header"].startswith("@@")
    assert {line["kind"] for line in hello_diff["hunks"][0]["lines"]} >= {
        "add",
        "delete",
    }


def test_staged_diff_preserves_multiple_hunk_coordinates(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 staged diff hunks")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    original = "".join(f"line {index}\n" for index in range(1, 31))
    target = root / "multi.txt"
    target.write_text(original, encoding="utf-8")
    subprocess.run(["git", "add", "multi.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "add multi"], cwd=root, check=True)
    lines = original.splitlines()
    lines[1] = "changed near start"
    lines[27] = "changed near end"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "multi.txt"], cwd=root, check=True)

    result = service.workspace_diff_v2(workspace_id, staged=True, include_hunks=True)

    diff = next(item for item in result["files"] if item["path"] == "multi.txt")
    assert diff["status"] == "modified"
    assert diff["additions"] == 2
    assert diff["deletions"] == 2
    assert len(diff["hunks"]) == 2
    assert diff["hunks"][0]["lines"][0]["old_line"] == 1
    assert diff["hunks"][1]["lines"][0]["old_line"] >= 24


def test_summary_mode_never_calls_full_diff(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "summary routing")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=root, check=True)
    monkeypatch.setattr(
        service._workspace_retrieval.ctx.git,
        "diff",
        lambda *args, **kwargs: pytest.fail("full diff called in summary mode"),
    )

    result = service.workspace_diff_v2(workspace_id, staged=True)

    assert result["files"][0]["hunks"] == []


def test_hunk_mode_never_calls_summary_adapter(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "hunk routing")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=root, check=True)
    monkeypatch.setattr(
        service._workspace_retrieval.ctx.git,
        "diff_summary",
        lambda *args, **kwargs: pytest.fail("summary adapter called in hunk mode"),
    )

    result = service.workspace_diff_v2(workspace_id, staged=True, include_hunks=True)

    assert result["files"][0]["hunks"]


def test_diff_cursor_binds_mode_but_not_page_size_controls(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "diff cursor modes")["workspace_id"]
    for name in ("a.txt", "b.txt", "c.txt"):
        service.workspace_write_file(workspace_id, name, f"{name}\n", "<new>")

    summary = service.workspace_diff_v2(workspace_id, max_files=2)
    assert summary["next_cursor"] is not None
    resumed_summary = service.workspace_diff_v2(
        workspace_id,
        max_files=1,
        byte_budget=60_000,
        cursor=summary["next_cursor"],
    )
    assert len(resumed_summary["files"]) == 1
    with pytest.raises(ValueError, match=r"cursor.*request"):
        service.workspace_diff_v2(
            workspace_id,
            include_hunks=True,
            cursor=summary["next_cursor"],
        )

    hunks = service.workspace_diff_v2(workspace_id, max_files=2, include_hunks=True)
    assert hunks["next_cursor"] is not None
    resumed_hunks = service.workspace_diff_v2(
        workspace_id,
        max_files=1,
        byte_budget=60_000,
        cursor=hunks["next_cursor"],
        include_hunks=True,
    )
    assert len(resumed_hunks["files"]) == 1
    with pytest.raises(ValueError, match=r"cursor.*request"):
        service.workspace_diff_v2(workspace_id, cursor=hunks["next_cursor"])


@pytest.mark.parametrize("include_hunks", [False, True])
def test_diff_filter_and_pagination_work_in_both_modes(
    forge_env: ForgeEnvironment,
    include_hunks: bool,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", f"filtered diff {include_hunks}")[
        "workspace_id"
    ]
    for name in ("a.txt", "b.txt", "ignored.py"):
        service.workspace_write_file(workspace_id, name, f"{name}\n", "<new>")

    result = service.workspace_diff_v2(
        workspace_id,
        path_glob="*.txt",
        max_files=1,
        include_hunks=include_hunks,
    )

    assert len(result["files"]) == 1
    assert result["files"][0]["path"].endswith(".txt")
    assert bool(result["files"][0]["hunks"]) is include_hunks
    assert result["truncated"] is True
    assert result["next_cursor"] is not None


def test_unchanged_workspace_diff_is_empty(forge_env: ForgeEnvironment) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "empty diff")["workspace_id"]

    assert forge_env.service.workspace_diff_v2(workspace_id)["files"] == []


def test_diff_audit_records_summary_and_hunk_modes(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "diff audit modes")["workspace_id"]
    service.workspace_write_file(workspace_id, "new.txt", "new\n", "<new>")

    service.workspace_diff_v2(workspace_id)
    service.workspace_diff_v2(workspace_id, include_hunks=True)

    events = [
        json.loads(line)
        for line in (forge_env.root / "state" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    details = [
        event["details"]
        for event in events
        if event["action"] == "workspace_diff_v2" and event["success"]
    ]
    assert [item["include_hunks"] for item in details[-2:]] == [False, True]


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


def test_workspace_search_deadline_returns_partial_resumable_non_security_evidence(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 deadline resume")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    target = root / "deadline"
    target.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (target / name).write_text(f"needle in {name}\n", encoding="utf-8")

    first_ticks = iter((0.0, 0.1, 1.0))
    monkeypatch.setattr(
        retrieval_module,
        "_monotonic",
        lambda: next(first_ticks, 1.0),
        raising=False,
    )
    first = service.workspace_search_v2(
        workspace_id,
        "needle",
        path_glob="deadline/*.txt",
        max_results=10,
        byte_budget=10_000,
    )

    V2_TOOL_SPECS["workspace_search"].validate_success_output(first)
    assert [item["path"] for item in first["matches"]] == ["deadline/a.txt"]
    assert first["truncated"] is True
    assert first["source_truncated"] is True
    assert first["truncation_reason"] == "search_deadline_exceeded"
    assert first["next_cursor"] is not None
    assert first["scanned_path_count"] == 1
    assert first["candidate_path_count"] == 3
    assert first["remaining_path_count"] == 2
    assert first["completed_providers"] == ["builtin_literal"]
    assert "path_glob" in first["recommended_scope"]
    metrics = service.application.context.metrics
    assert metrics is not None
    partial = metrics.snapshot()["operations"]["workspace_search_v2.partial"]
    assert partial["count"] == 1
    assert partial["successes"] == 1

    second_ticks = iter((2.0, 2.1, 2.2))
    monkeypatch.setattr(
        retrieval_module,
        "_monotonic",
        lambda: next(second_ticks, 2.2),
        raising=False,
    )
    second = service.workspace_search_v2(
        workspace_id,
        "needle",
        path_glob="deadline/*.txt",
        max_results=10,
        byte_budget=10_000,
        cursor=first["next_cursor"],
    )

    V2_TOOL_SPECS["workspace_search"].validate_success_output(second)
    assert [item["path"] for item in second["matches"]] == [
        "deadline/b.txt",
        "deadline/c.txt",
    ]
    assert second["next_cursor"] is None
    assert second["truncation_reason"] is None
    assert second["scanned_path_count"] == 3
    assert second["candidate_path_count"] == 3
    assert second["remaining_path_count"] == 0


def test_workspace_search_cursor_rejects_changed_workspace_identity(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 stale search cursor")["workspace_id"]
    first = service.workspace_search_v2(
        workspace_id,
        "e",
        max_results=1,
        byte_budget=200,
    )
    assert first["next_cursor"] is not None
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed after cursor review\n",
        current["sha256"],
    )

    with pytest.raises(ValueError, match=r"cursor.*request"):
        service.workspace_search_v2(
            workspace_id,
            "e",
            max_results=1,
            byte_budget=200,
            cursor=first["next_cursor"],
        )


def test_workspace_regex_timeout_falls_back_to_bounded_resumable_provider(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 regex timeout fallback")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    (root / "regex.txt").write_text("value_123\nvalue_456\n", encoding="utf-8")
    context = service.application.context

    def timed_out(*args: object, **kwargs: object):
        del args, kwargs
        raise CommandError(
            "COMMAND_TIMEOUT: git grep exceeded the reviewed deadline",
            code=ErrorCode.COMMAND_TIMEOUT,
            retryable=True,
        )

    monkeypatch.setattr(context.git, "search_regex_locations", timed_out)
    result = service.workspace_search_v2(
        workspace_id,
        r"value_[0-9]+",
        mode=SearchMode.REGEX,
        path_glob="regex.txt",
    )

    assert [item["match"] for item in result["matches"]] == ["value_123", "value_456"]
    assert all(item["provider"] == "builtin_regex" for item in result["matches"])
    assert result["completed_providers"] == ["builtin_regex"]
    assert result["truncation_reason"] is None
    metrics = context.metrics
    assert metrics is not None
    fallback = metrics.snapshot()["operations"]["workspace_search_v2.provider_fallback"]
    assert fallback["count"] == 1
    assert fallback["successes"] == 1


def test_repo_regex_timeout_falls_back_and_validates_public_output(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    context = service.application.context

    def timed_out(*args: object, **kwargs: object):
        del args, kwargs
        raise CommandError(
            "COMMAND_TIMEOUT: git grep exceeded the reviewed deadline",
            code=ErrorCode.COMMAND_TIMEOUT,
            retryable=True,
        )

    monkeypatch.setattr(context.git, "search_regex_locations", timed_out)
    result = service.repo_search_v2(
        "demo",
        r"hello",
        mode=SearchMode.REGEX,
        path_glob="hello.txt",
    )

    V2_TOOL_SPECS["repo_search"].validate_success_output(result)
    assert result["summary"] == "Searched the exact repository snapshot"
    assert [item["match"] for item in result["matches"]] == ["hello"]
    assert result["completed_providers"] == ["builtin_regex"]
    metrics = context.metrics
    assert metrics is not None
    fallback = metrics.snapshot()["operations"]["repo_search_v2.provider_fallback"]
    assert fallback["count"] == 1
    assert fallback["successes"] == 1


def test_paginate_refuses_an_item_larger_than_the_advertised_transport_budget() -> None:
    oversized = StructuredSearchMatch(
        path="large.txt",
        line=1,
        column=1,
        match="x" * 1_000,
        context_before=(),
        context_after=(),
        score=1.0,
        provider="builtin_literal",
    )

    with pytest.raises(RepoForgeError) as blocked:
        paginate(
            (oversized,),
            kind="transport-budget-test",
            scope="fixture",
            request={"query": "x"},
            max_items=10,
            byte_budget=32,
            cursor=None,
        )

    assert blocked.value.code is ErrorCode.RESULT_TRANSPORT_BUDGET_EXCEEDED
    assert blocked.value.retryable is False
    assert blocked.value.details["byte_budget"] == 32
    assert blocked.value.details["required_bytes"] > 32
    assert blocked.value.unchanged_state == ("No oversized result page was emitted.",)
