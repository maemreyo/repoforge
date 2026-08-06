from __future__ import annotations

from pathlib import Path

from codegraph_provider_support import (
    FakeProjection,
    FakeRunner,
    baseline,
    manifest,
    request,
    symbol,
)

from repoforge.adapters.codegraph.provider import ManagedCodeGraphProvider
from repoforge.domain.code_intelligence import CodeIntelligenceStatus


def test_provider_builds_current_graph_in_reviewed_order(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request, symbol("run")))

    assert graph.status is CodeIntelligenceStatus.CURRENT
    assert [fact.source_path for fact in graph.relationships] == [
        "src/helper.py",
        "src/service.py",
    ]
    assert [item.path for item in graph.affected_paths] == [
        "tests/test_service.py",
        "tests/test_service.py",
    ]
    assert {item.reason for item in graph.affected_paths} == {
        "CodeGraph affected-file traversal.",
        "CodeGraph symbol-impact traversal.",
    }
    assert [name for name, _ in runner.calls] == [
        "init",
        "status",
        "affected",
        "query",
        "callers",
        "callees",
        "impact",
    ]
    assert projection.completed == [projection.result.manifest.manifest_digest]


def test_existing_complete_index_uses_sync(tmp_path: Path) -> None:
    code_request = request(tmp_path, changed=())
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    (projection.source / ".index").mkdir()
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request))

    assert graph.status is CodeIntelligenceStatus.CURRENT
    assert runner.calls[0][0] == "sync"


def test_incomplete_state_is_invalidated_before_fresh_init(tmp_path: Path) -> None:
    code_request = request(tmp_path, changed=())
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    (projection.source / ".index").mkdir()
    (projection.root / "INCOMPLETE").write_text("failed\n", encoding="utf-8")
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    provider.analyze(code_request, baseline(code_request))

    assert projection.invalidated == 1
    assert runner.calls[0][0] == "init"


def test_malformed_late_output_returns_partial_without_raw_text(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    runner.fail_query = True
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request, symbol("run")))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert [item.path for item in graph.affected_paths] == ["tests/test_service.py"]
    rendered = repr(graph)
    assert "provider-secret" not in rendered
    assert "private" not in rendered


def test_duplicate_baseline_symbol_names_are_skipped_not_misjoined(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(
        code_request,
        baseline(
            code_request,
            symbol("run"),
            symbol("run", "src/helper.py"),
        ),
    )

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert graph.relationships == ()
    assert not any(name in {"query", "callers", "callees", "impact"} for name, _ in runner.calls)
    assert any("ambiguous" in limitation.lower() for limitation in graph.limitations)


def test_cross_path_query_ambiguity_stops_before_relationship_commands(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    runner.ambiguous_query = True
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request, symbol("run")))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert graph.relationships == ()
    assert not any(name in {"callers", "callees", "impact"} for name, _ in runner.calls)
    assert any("ambiguous" in limitation.lower() for limitation in graph.limitations)


def test_symbol_fanout_bound_yields_partial_evidence(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(
        manifest(),
        projection,
        runner,
        max_seed_symbols=1,
    )
    symbols = (symbol("first"), symbol("second"))

    graph = provider.analyze(code_request, baseline(code_request, *symbols))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert sum(name == "query" for name, _ in runner.calls) == 1
    assert any(
        "symbol" in limitation.lower() and "bound" in limitation.lower()
        for limitation in graph.limitations
    )
