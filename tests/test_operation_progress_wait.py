"""Tests for #257: progress-heartbeat wait + capability-gated MCP reporter."""

from __future__ import annotations

from conftest import ForgeEnvironment

from repoforge.application.operations.composite import OperationCommand
from repoforge.domain.client_capabilities import (
    ClientCapabilities,
    ClientFeature,
    FeatureSupport,
)
from repoforge.interfaces.mcp.progress import build_progress_reporter
from repoforge.ports.progress_reporter import NullProgressReporter


class _FakeReporter:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self.reports: list[tuple[int, int | None, str | None]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def report(self, *, current: int, total: int | None, message: str | None) -> None:
        self.reports.append((current, total, message))


def _caps(*, progress: bool) -> ClientCapabilities:
    features = tuple(
        (
            feature,
            FeatureSupport(
                supported=(feature is ClientFeature.PROGRESS_NOTIFICATIONS and progress)
            ),
        )
        for feature in ClientFeature
    )
    return ClientCapabilities(
        protocol_version="2025-06-18",
        client_name="test",
        client_version="1",
        features=features,
    )


# --------------------------------------------------------------- coordinator seam


def test_wait_emits_progress_when_reporter_enabled(forge_env: ForgeEnvironment) -> None:
    coordinator = forge_env.service._operation
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    running = manager.start(task.operation_id)
    manager.progress(task.operation_id, phase="working", current=1, total=3, message="step one")

    reporter = _FakeReporter(enabled=True)
    result = coordinator.execute(
        OperationCommand(
            action="wait",
            operation_id=task.operation_id,
            since_updated_at=running.updated_at,  # a change already happened -> returns fast
            timeout_seconds=1,
        ),
        progress_reporter=reporter,
    )

    assert result.action == "wait"
    assert reporter.reports  # at least the initial heartbeat
    assert reporter.reports[0][0] == 1
    assert reporter.reports[0][1] == 3


def test_wait_does_not_emit_when_reporter_disabled(forge_env: ForgeEnvironment) -> None:
    coordinator = forge_env.service._operation
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    running = manager.start(task.operation_id)
    manager.progress(task.operation_id, phase="working", current=1, total=3)

    reporter = _FakeReporter(enabled=False)
    coordinator.execute(
        OperationCommand(
            action="wait",
            operation_id=task.operation_id,
            since_updated_at=running.updated_at,
            timeout_seconds=1,
        ),
        progress_reporter=reporter,
    )
    assert reporter.reports == []


def test_wait_without_reporter_uses_null(forge_env: ForgeEnvironment) -> None:
    coordinator = forge_env.service._operation
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    running = manager.start(task.operation_id)
    # No reporter passed: behaves exactly as before.
    result = coordinator.execute(
        OperationCommand(
            action="wait",
            operation_id=task.operation_id,
            since_updated_at=running.updated_at,
            timeout_seconds=1,
        )
    )
    assert result.action == "wait"


# ------------------------------------------------------------------ MCP adapter


def test_reporter_enabled_only_with_token_and_capability() -> None:
    emitted: list[tuple[int, int | None, str | None]] = []

    def emit(current: int, total: int | None, message: str | None) -> None:
        emitted.append((current, total, message))

    enabled = build_progress_reporter(
        capabilities=_caps(progress=True), has_progress_token=True, emit=emit
    )
    assert enabled.enabled is True
    enabled.report(current=2, total=4, message="hi")
    assert emitted == [(2, 4, "hi")]

    # capability present but no progress token -> disabled
    no_token = build_progress_reporter(
        capabilities=_caps(progress=True), has_progress_token=False, emit=emit
    )
    assert no_token.enabled is False

    # token present but capability absent -> disabled
    no_cap = build_progress_reporter(
        capabilities=_caps(progress=False), has_progress_token=True, emit=emit
    )
    assert no_cap.enabled is False
    no_cap.report(current=9, total=9, message="x")
    assert emitted == [(2, 4, "hi")]  # unchanged


def test_reporter_swallows_emit_failure() -> None:
    def boom(current: int, total: int | None, message: str | None) -> None:
        raise RuntimeError("session closed")

    reporter = build_progress_reporter(
        capabilities=_caps(progress=True), has_progress_token=True, emit=boom
    )
    # must not raise
    reporter.report(current=1, total=None, message=None)


def test_null_reporter_is_disabled() -> None:
    reporter = NullProgressReporter()
    assert reporter.enabled is False
    reporter.report(current=1, total=2, message="x")
