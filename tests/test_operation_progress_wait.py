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


# ------------------------------------------------- bounded emission (Layer 1 ceiling)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_pushed_progress_is_rate_limited_but_never_silent() -> None:
    emitted: list[int] = []
    clock = _Clock()
    reporter = build_progress_reporter(
        capabilities=_caps(progress=True),
        has_progress_token=True,
        emit=lambda current, total, message: emitted.append(current),
        min_interval_s=0.5,
        clock=clock,
    )

    reporter.report(current=1, total=9, message=None)  # first always goes through
    reporter.report(current=2, total=9, message=None)  # same instant -> dropped
    clock.now += 0.4
    reporter.report(current=3, total=9, message=None)  # still inside the window
    clock.now += 0.2
    reporter.report(current=4, total=9, message=None)  # 0.6s since the last emit

    assert emitted == [1, 4]


def test_a_disabled_reporter_does_not_consume_the_rate_limit_budget() -> None:
    emitted: list[int] = []
    clock = _Clock()
    reporter = build_progress_reporter(
        capabilities=_caps(progress=False),
        has_progress_token=True,
        emit=lambda current, total, message: emitted.append(current),
        clock=clock,
    )
    reporter.report(current=1, total=2, message=None)
    assert emitted == []


# --------------------------------------- chosen strategy + resubscribe cursor (#257)


def test_wait_names_the_delivery_mechanism_it_actually_used(
    forge_env: ForgeEnvironment,
) -> None:
    coordinator = forge_env.service._operation
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    running = manager.start(task.operation_id)
    manager.progress(task.operation_id, phase="working", current=1, total=3)

    pushed = coordinator.execute(
        OperationCommand(
            action="wait",
            operation_id=task.operation_id,
            since_updated_at=running.updated_at,
            timeout_seconds=1,
        ),
        progress_reporter=_FakeReporter(enabled=True),
    )
    assert pushed.progress_delivery == "pushed"

    polled = coordinator.execute(
        OperationCommand(
            action="wait",
            operation_id=task.operation_id,
            since_updated_at=running.updated_at,
            timeout_seconds=1,
        ),
        progress_reporter=_FakeReporter(enabled=False),
    )
    assert polled.progress_delivery == "poll"


def test_a_progress_wait_that_times_out_with_no_delta_still_returns_a_cursor(
    forge_env: ForgeEnvironment,
) -> None:
    """The gap this closes: that case used to return no evidence and nothing to resume from."""

    coordinator = forge_env.service._operation
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    running = manager.start(task.operation_id)

    result = coordinator.execute(
        OperationCommand(
            action="wait",
            operation_id=task.operation_id,
            # Baseline is already the current state, so nothing can count as a delta.
            since_updated_at=running.updated_at,
            until="progress",
            timeout_seconds=1,
        )
    )

    assert result.timed_out is True
    assert result.changed_since is False
    assert result.operation is None  # unchanged: no delta means no evidence to report
    # ...but the caller can now continue instead of being left with only `timed_out`.
    assert result.next_since_updated_at == running.updated_at
    assert result.suggested_poll_after_s is not None
    assert result.progress_delivery == "poll"


def test_a_terminal_wait_offers_no_cursor_to_resume_from(
    forge_env: ForgeEnvironment,
) -> None:
    coordinator = forge_env.service._operation
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    manager.start(task.operation_id)
    manager.succeed(task.operation_id, result_reference="verify:done")

    result = coordinator.execute(
        OperationCommand(
            action="wait",
            operation_id=task.operation_id,
            until="terminal",
            timeout_seconds=1,
        )
    )

    assert result.timed_out is False
    assert result.next_since_updated_at is None
    assert result.suggested_poll_after_s is None


def test_non_wait_actions_carry_no_wait_only_fields(forge_env: ForgeEnvironment) -> None:
    coordinator = forge_env.service._operation
    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    manager.start(task.operation_id)

    for command in (
        OperationCommand(action="get", operation_id=task.operation_id),
        OperationCommand(action="list"),
    ):
        result = coordinator.execute(command)
        assert result.progress_delivery is None
        assert result.next_since_updated_at is None
        assert result.suggested_poll_after_s is None
