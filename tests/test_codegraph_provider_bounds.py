from __future__ import annotations

from pathlib import Path

from codegraph_provider_support import (
    FakeProjection,
    FakeRunner,
    SequenceClock,
    baseline,
    manifest,
    request,
    status,
    symbol,
)

from repoforge.adapters.codegraph.provider import ManagedCodeGraphProvider
from repoforge.domain.code_intelligence import CodeIntelligenceStatus


def test_status_file_count_mismatch_is_unavailable_and_invalidated(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    runner.status_file_count = 2
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request))

    assert graph.status is CodeIntelligenceStatus.UNAVAILABLE
    assert projection.completed == []
    assert projection.invalidated == 1
    assert graph.relationships == ()
    assert graph.affected_paths == ()


def test_malformed_status_is_unavailable_without_raw_provider_text(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    runner.status_payload = '{"private":"provider-secret"} trailing'
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request))

    assert graph.status is CodeIntelligenceStatus.UNAVAILABLE
    assert projection.invalidated == 1
    assert "provider-secret" not in repr(graph)
    assert "private" not in repr(graph)


def test_aggregate_output_bound_after_status_yields_partial(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    bytes_through_status = len(b"initialized") + len(status(projection.source).encode())
    provider = ManagedCodeGraphProvider(
        manifest(),
        projection,
        runner,
        max_total_output_bytes=bytes_through_status,
    )

    graph = provider.analyze(code_request, baseline(code_request, symbol("run")))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert graph.truncated is True
    assert projection.completed
    assert any("aggregate" in limitation.lower() for limitation in graph.limitations)


def test_truncated_command_after_status_yields_partial(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    runner.truncated_command = "affected"
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request, symbol("run")))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert graph.truncated is True
    assert any("byte bound" in limitation.lower() for limitation in graph.limitations)


def test_wall_time_bound_after_status_yields_partial(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    clock = SequenceClock((0, 0, 0, 0, 100))
    provider = ManagedCodeGraphProvider(
        manifest(),
        projection,
        runner,
        max_wall_seconds=1,
        monotonic=clock,
    )

    graph = provider.analyze(code_request, baseline(code_request, symbol("run")))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert graph.truncated is True
    assert projection.completed
    assert any("wall-time" in limitation.lower() for limitation in graph.limitations)


def test_changed_paths_outside_projection_are_excluded(tmp_path: Path) -> None:
    code_request = request(tmp_path, changed=("README.md",))
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert not any(name == "affected" for name, _ in runner.calls)
    assert any(
        "changed" in limitation.lower() and "projection" in limitation.lower()
        for limitation in graph.limitations
    )


def test_projection_coverage_uses_manifest_entries(tmp_path: Path) -> None:
    code_request = request(tmp_path, changed=())
    code_request.workspace_root.mkdir()
    projection = FakeProjection(
        tmp_path,
        code_request,
        projected_paths=("src/helper.py", "src/service.py"),
        limitations=("One unsupported file was omitted from the projection.",),
    )
    runner = FakeRunner(projection.source)
    runner.status_file_count = 2
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request))

    assert graph.status is CodeIntelligenceStatus.PARTIAL
    assert graph.coverage.value == 67
    assert any("unsupported" in limitation.lower() for limitation in graph.limitations)


def test_dangling_incomplete_symlink_forces_invalidation(tmp_path: Path) -> None:
    code_request = request(tmp_path, changed=())
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    (projection.source / ".index").mkdir()
    (projection.root / "INCOMPLETE").symlink_to(projection.root / "missing")
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    provider.analyze(code_request, baseline(code_request))

    assert projection.invalidated == 1
    assert runner.calls[0][0] == "init"


def test_dangling_index_symlink_is_invalidated_before_successful_init(tmp_path: Path) -> None:
    code_request = request(tmp_path, changed=())
    code_request.workspace_root.mkdir()
    projection = FakeProjection(tmp_path, code_request)
    (projection.source / ".index").symlink_to(projection.root / "missing-index")
    runner = FakeRunner(projection.source)
    provider = ManagedCodeGraphProvider(manifest(), projection, runner)

    graph = provider.analyze(code_request, baseline(code_request))

    assert graph.status is CodeIntelligenceStatus.CURRENT
    assert projection.invalidated == 1
    assert runner.calls[0][0] == "init"
