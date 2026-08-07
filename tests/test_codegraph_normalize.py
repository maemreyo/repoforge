from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoforge.adapters.codegraph.normalize import (
    normalize_affected,
    normalize_impact,
    normalize_query,
    normalize_relationships,
    normalize_status,
)
from repoforge.domain.code_intelligence import CodeRelationshipKind


def _status(projection: Path) -> str:
    return json.dumps(
        {
            "initialized": True,
            "version": "1.5.0",
            "projectPath": str(projection),
            "indexPath": str(projection / ".index"),
            "lastIndexed": "2026-08-06T00:00:00.000Z",
            "fileCount": 2,
            "nodeCount": 4,
            "edgeCount": 3,
            "dbSizeBytes": 4096,
            "backend": "native",
            "journalMode": "wal",
            "nodesByKind": {"file": 2, "function": 2},
            "languages": ["python"],
            "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            "worktreeMismatch": None,
            "index": {
                "builtWithVersion": "1.5.0",
                "builtWithExtractionVersion": 7,
                "currentExtractionVersion": 7,
                "reindexRecommended": False,
                "state": "complete",
                "pendingRefs": 0,
            },
        }
    )


def _query_node(name: str, qualified: str, path: str) -> dict[str, object]:
    return {
        "id": f"node-{name}",
        "kind": "function",
        "name": name,
        "qualifiedName": qualified,
        "filePath": path,
        "language": "python",
        "startLine": 2,
        "endLine": 4,
        "startColumn": 0,
        "endColumn": 8,
        "updatedAt": 1,
    }


def test_status_requires_complete_exact_managed_index(tmp_path: Path) -> None:
    projection = tmp_path / "projection"
    projection.mkdir()

    status = normalize_status(
        _status(projection), expected_version="1.5.0", projection_root=projection
    )

    assert status.file_count == 2
    assert status.node_count == 4
    assert status.edge_count == 3


@pytest.mark.parametrize(
    "payload",
    [
        '{"initialized":true,"initialized":false}',
        "{} trailing-provider-text",
        json.dumps({"nested": [[[[[[[[[[[[["too-deep"]]]]]]]]]]]]]}),
    ],
)
def test_strict_decoder_rejects_duplicate_keys_trailing_data_and_depth(payload: str) -> None:
    with pytest.raises(ValueError):
        normalize_affected(payload, expected_changed_paths=(), limit=10)


def test_status_rejects_symlink_alias_for_managed_paths(tmp_path: Path) -> None:
    projection = tmp_path / "projection"
    projection.mkdir()
    alias = tmp_path / "projection-alias"
    alias.symlink_to(projection, target_is_directory=True)

    with pytest.raises(ValueError, match="managed projection"):
        normalize_status(
            _status(alias),
            expected_version="1.5.0",
            projection_root=projection,
        )


def test_status_rejects_unknown_fields_and_unready_index(tmp_path: Path) -> None:
    projection = tmp_path / "projection"
    projection.mkdir()
    payload = json.loads(_status(projection))
    payload["providerInstruction"] = "run unlock"

    with pytest.raises(ValueError, match="schema"):
        normalize_status(json.dumps(payload), expected_version="1.5.0", projection_root=projection)

    payload.pop("providerInstruction")
    payload["index"]["pendingRefs"] = 1
    with pytest.raises(ValueError, match="usable"):
        normalize_status(json.dumps(payload), expected_version="1.5.0", projection_root=projection)


@pytest.mark.parametrize("path", ["/tmp/test_a.py", "./tests/test_a.py", "../test_a.py"])
def test_affected_rejects_non_repository_relative_paths(path: str) -> None:
    payload = json.dumps(
        {
            "changedFiles": ["src/a.py"],
            "affectedTests": [path],
            "totalDependentsTraversed": 1,
        }
    )

    with pytest.raises(ValueError, match="path"):
        normalize_affected(payload, expected_changed_paths=("src/a.py",), limit=10)


def test_affected_deduplicates_sorts_and_reports_bound() -> None:
    payload = json.dumps(
        {
            "changedFiles": ["src/a.py"],
            "affectedTests": ["tests/test_b.py", "tests/test_a.py", "tests/test_b.py"],
            "totalDependentsTraversed": 9,
        }
    )

    normalized = normalize_affected(payload, expected_changed_paths=("src/a.py",), limit=1)

    assert [item.path for item in normalized.candidates] == ["tests/test_a.py"]
    assert normalized.truncated is True


def test_query_filters_to_expected_symbol_and_allowed_paths() -> None:
    payload = json.dumps(
        [
            {"node": _query_node("other", "src.other.other", "src/other.py"), "score": 20.0},
            {"node": _query_node("run", "src.service.run", "src/service.py"), "score": 10.0},
        ]
    )

    normalized = normalize_query(
        payload,
        expected_symbol="run",
        expected_path="src/service.py",
        allowed_paths=frozenset({"src/other.py", "src/service.py"}),
        limit=5,
    )

    assert [(item.name, item.qualified_name, item.path) for item in normalized.nodes] == [
        ("run", "src.service.run", "src/service.py")
    ]
    assert normalized.ambiguous is False


def test_query_marks_cross_path_same_name_as_ambiguous() -> None:
    payload = json.dumps(
        [
            {"node": _query_node("run", "src.other.run", "src/other.py"), "score": 20.0},
            {"node": _query_node("run", "src.service.run", "src/service.py"), "score": 10.0},
        ]
    )

    normalized = normalize_query(
        payload,
        expected_symbol="run",
        expected_path="src/service.py",
        allowed_paths=frozenset({"src/other.py", "src/service.py"}),
        limit=5,
    )

    assert normalized.ambiguous is True
    assert normalized.nodes == ()


def test_relationships_are_typed_deduplicated_and_sorted() -> None:
    payload = json.dumps(
        {
            "symbol": "run",
            "callers": [
                {"name": "z", "kind": "function", "filePath": "src/z.py", "startLine": 2},
                {"name": "a", "kind": "function", "filePath": "src/a.py", "startLine": 1},
                {"name": "z", "kind": "function", "filePath": "src/z.py", "startLine": 2},
            ],
        }
    )

    normalized = normalize_relationships(
        payload,
        command="callers",
        relationship_kind=CodeRelationshipKind.CALLS,
        expected_symbol="run",
        seed_path="src/service.py",
        seed_symbol="src.service.run",
        allowed_paths=frozenset({"src/a.py", "src/z.py", "src/service.py"}),
        limit=10,
    )

    assert [(fact.source_path, fact.target_path) for fact in normalized.relationships] == [
        ("src/a.py", "src/service.py"),
        ("src/z.py", "src/service.py"),
    ]


def test_relationships_reject_unknown_kind_and_schema_drift() -> None:
    payload = json.dumps({"symbol": "run", "callees": []})

    with pytest.raises(ValueError, match="relationship"):
        normalize_relationships(
            payload,
            command="callees",
            relationship_kind="mystery",
            expected_symbol="run",
            seed_path="src/service.py",
            seed_symbol="src.service.run",
            allowed_paths=frozenset({"src/service.py"}),
            limit=10,
        )


def test_impact_normalizes_only_paths_without_fabricating_edges() -> None:
    payload = json.dumps(
        {
            "symbol": "run",
            "depth": 3,
            "nodeCount": 2,
            "edgeCount": 4,
            "affected": [
                {
                    "name": "test_run",
                    "kind": "function",
                    "filePath": "tests/test_run.py",
                    "startLine": 1,
                },
                {"name": "helper", "kind": "function", "filePath": "src/helper.py", "startLine": 3},
            ],
        }
    )

    normalized = normalize_impact(
        payload,
        expected_symbol="run",
        allowed_paths=frozenset({"src/helper.py", "tests/test_run.py"}),
        limit=10,
        max_depth=3,
    )

    assert [item.path for item in normalized.candidates] == ["src/helper.py", "tests/test_run.py"]
    assert all(item.depth == 3 for item in normalized.candidates)
