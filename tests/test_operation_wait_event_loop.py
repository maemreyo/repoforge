"""Tests for #259: a bounded ``operation wait`` must not hold the MCP event loop.

The wait is a poll loop that sleeps, so running it inline in the async tool
handler stopped the session servicing anything else for as long as it blocked --
and left no thread-safe way to push progress on the open request. These tests
pin both halves of the fix: the wait runs in a worker thread, and the progress
bridge hands its coroutine back to the event loop that owns the session.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import anyio
import pytest
from conftest import ForgeEnvironment
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.application.operations.progress_context import (
    bind_progress_reporter,
    current_progress_reporter,
)
from repoforge.interfaces.mcp.server import _sleeps_while_waiting, create_server

_WAIT_SECONDS = 5
"""Long enough that a serialized second call could not possibly beat it."""


# ------------------------------------------------------------------- the predicate


def test_only_the_wait_action_is_offloaded() -> None:
    assert _sleeps_while_waiting("operation", {"action": "wait"}) is True
    assert _sleeps_while_waiting("operation", {"action": "get"}) is False
    assert _sleeps_while_waiting("operation", {}) is False
    # A different tool that happens to carry an action must stay on the loop.
    assert _sleeps_while_waiting("repo_issue", {"action": "wait"}) is False


# ------------------------------------------------------- the acceptance criterion


@pytest.mark.anyio
async def test_open_wait_does_not_serialize_a_second_call_on_the_same_session(
    forge_env: ForgeEnvironment,
) -> None:
    """The headline criterion: a 5 s wait must not block another call on that session."""

    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    manager.start(task.operation_id)

    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        wait_results: list[Any] = []
        wait_returned = False

        async def run_wait() -> None:
            nonlocal wait_returned
            result = await session.call_tool(
                "operation",
                {
                    "action": "wait",
                    "operation_id": task.operation_id,
                    "timeout_seconds": _WAIT_SECONDS,
                    # 'terminal' so no progress delta can end the wait early --
                    # it stays open for the full timeout.
                    "until": "terminal",
                },
            )
            wait_returned = True
            wait_results.append(result)

        started = time.monotonic()
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_wait)
            await anyio.sleep(0.25)  # let the wait reach its loop
            second = await session.call_tool(
                "operation",
                {"action": "get", "operation_id": task.operation_id},
            )
            # Ordering, not elapsed time: the second call came back before the wait
            # did. Asserting a wall-clock margin instead made this flake on slower
            # CI runners, where the wait's 10 Hz polling of the same record contends
            # on its file lock and a concurrent read can take seconds -- slow, but
            # still concurrent, which is the property under test. Before the fix this
            # could only return *after* the wait returned, so `wait_returned` would
            # already be True here.
            second_returned_first = not wait_returned
        total_elapsed = time.monotonic() - started

    assert second.isError is False
    assert second_returned_first, "the second call was not serviced until the wait finished"

    # And the wait really did stay open for its full bound, so the ordering above is
    # not a vacuous pass from a wait that never started. A lower bound on a sleep is
    # safe on any machine: a slow runner only makes it more true.
    assert total_elapsed >= _WAIT_SECONDS
    (wait_result,) = wait_results
    assert wait_result.isError is False
    assert wait_result.structuredContent is not None
    assert wait_result.structuredContent["timed_out"] is True


@pytest.mark.anyio
async def test_offloaded_wait_still_wakes_on_terminal_before_its_timeout(
    forge_env: ForgeEnvironment,
) -> None:
    """Off the event loop the wait must keep its terminal-wake semantics."""

    manager = forge_env.service.operations
    task = manager.create(kind="watch", phase="queued", cancel_supported=True)
    manager.start(task.operation_id)

    server = create_server(service=forge_env.service)
    async with create_connected_server_and_client_session(server) as session:
        started = time.monotonic()

        async def finish_soon() -> None:
            await anyio.sleep(0.3)
            manager.succeed(task.operation_id, result_reference="verify:done")

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(finish_soon)
            result = await session.call_tool(
                "operation",
                {
                    "action": "wait",
                    "operation_id": task.operation_id,
                    "timeout_seconds": _WAIT_SECONDS,
                    "until": "terminal",
                },
            )
        elapsed = time.monotonic() - started

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["timed_out"] is False
    assert result.structuredContent["operation"]["terminal"] is True
    # Returned on the outcome, not on the clock.
    assert elapsed < _WAIT_SECONDS


# ------------------------------------------------------------- the progress bridge


class _FakeSession:
    def __init__(self, *, progress: bool) -> None:
        self.client_params: dict[str, Any] = {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "test-client", "version": "1"},
            "capabilities": (
                {"experimental": {"repoforge": {"progressNotifications": True}}} if progress else {}
            ),
        }


class _FakeRequestContext:
    def __init__(self, *, token: str | None) -> None:
        self.meta = type("_Meta", (), {"progressToken": token})() if token else None


class _FakeContext:
    """The two surfaces ``_progress_bridge`` reads, plus a recording sink."""

    def __init__(self, *, token: str | None, progress: bool, fail: bool = False) -> None:
        self.session = _FakeSession(progress=progress)
        self.request_context = _FakeRequestContext(token=token)
        self._fail = fail
        self.reported: list[tuple[float, float | None, str | None]] = []
        self.reported_on_threads: set[int] = set()

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        if self._fail:
            raise RuntimeError("session closed mid-wait")
        self.reported_on_threads.add(threading.get_ident())
        self.reported.append((progress, total, message))


def _bridge(server: Any, context: _FakeContext) -> Any:
    server.get_context = lambda: context
    return server._progress_bridge()


@pytest.mark.anyio
async def test_bridge_emits_from_a_worker_thread_onto_the_event_loop(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)
    context = _FakeContext(token="tok-1", progress=True)
    reporter = _bridge(server, context)
    assert reporter.enabled is True

    loop_thread = threading.get_ident()

    def emit_from_worker() -> int:
        # This is where the wait loop calls report(): inside the worker thread.
        reporter.report(current=2, total=5, message="step two")
        return threading.get_ident()

    worker_thread = await anyio.to_thread.run_sync(emit_from_worker)

    assert worker_thread != loop_thread, "the emit must have run off the event loop"
    assert context.reported == [(2.0, 5.0, "step two")]
    # The notification itself was written by the loop thread, the only thread
    # allowed to touch the transport.
    assert context.reported_on_threads == {loop_thread}


@pytest.mark.anyio
async def test_bridge_stays_disabled_without_a_token_or_the_capability(
    forge_env: ForgeEnvironment,
) -> None:
    server = create_server(service=forge_env.service)

    no_token = _bridge(server, _FakeContext(token=None, progress=True))
    assert no_token.enabled is False

    no_capability = _bridge(server, _FakeContext(token="tok-1", progress=False))
    assert no_capability.enabled is False

    context = _FakeContext(token="tok-1", progress=False)
    reporter = _bridge(server, context)

    def emit_from_worker() -> None:
        reporter.report(current=1, total=1, message="ignored")

    await anyio.to_thread.run_sync(emit_from_worker)
    assert context.reported == []


@pytest.mark.anyio
async def test_bridge_swallows_a_session_that_closes_mid_wait(
    forge_env: ForgeEnvironment,
) -> None:
    """A closed session must not turn a successful wait into a failed tool call."""

    server = create_server(service=forge_env.service)
    context = _FakeContext(token="tok-1", progress=True, fail=True)
    reporter = _bridge(server, context)
    assert reporter.enabled is True

    def emit_from_worker() -> str:
        reporter.report(current=1, total=2, message="step one")
        return "the wait loop kept going"

    assert await anyio.to_thread.run_sync(emit_from_worker) == "the wait loop kept going"
    assert context.reported == []


@pytest.mark.anyio
async def test_bridge_is_null_outside_a_request(forge_env: ForgeEnvironment) -> None:
    server: Any = create_server(service=forge_env.service)

    def no_context() -> Any:
        raise LookupError("no active request")

    server.get_context = no_context
    assert server._progress_bridge().enabled is False


# ------------------------------------------------------------ the binding contract


def test_bound_reporter_is_visible_to_the_copied_worker_context() -> None:
    """``to_thread.run_sync`` copies the context, which is what carries the reporter."""

    class _Recorder:
        enabled = True

        def __init__(self) -> None:
            self.reports: list[int] = []

        def report(self, *, current: int, total: int | None, message: str | None) -> None:
            self.reports.append(current)

    recorder = _Recorder()

    async def main() -> None:
        assert current_progress_reporter().enabled is False

        def inside_worker() -> bool:
            with bind_progress_reporter(recorder):
                current_progress_reporter().report(current=7, total=None, message=None)
                return current_progress_reporter() is recorder

        assert await anyio.to_thread.run_sync(inside_worker) is True
        # The binding does not leak back out of the worker's copied context.
        assert current_progress_reporter().enabled is False

    anyio.run(main)
    assert recorder.reports == [7]
