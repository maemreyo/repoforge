"""Control-plane isolation from a genuinely blocked repository command.

The failure this file exists to catch was observed at runtime: a long
`workspace_verify` owned an MCP request thread, and while it ran, `operation`,
`workspace_status` and cancellation calls could not complete. Durable execution
moves the command into a worker that owns no request thread, so these tests hold
a real child process open and assert the control plane still answers promptly.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from conftest import TEST_CONFIG_GENERATION, ForgeEnvironment, create_forge_environment

from repoforge.application.operations.work_executor import VerificationWorkHandlers
from repoforge.application.operations.work_loop import OperationWorkLoop
from repoforge.application.service import CodingService
from repoforge.application.workspace.run_adhoc import WorkspaceAdhocRunner
from repoforge.application.workspace.run_diagnostic import WorkspaceDiagnosticRunner
from repoforge.application.workspace.run_profile import WorkspaceProfileRunner
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.operation_worker import OperationWorkerBinding
from repoforge.testing.fakes import ManualBackgroundTaskRunner

_CONTROL_PLANE_BUDGET_SECONDS = 2.0


def _add_blocked_profile(env: ForgeEnvironment, release: Path) -> None:
    """Enroll a profile whose only command blocks until `release` appears."""
    wait_script = (
        "import time; from pathlib import Path; "
        f"release = Path({str(release)!r}); "
        'exec("while not release.exists():\\n    time.sleep(0.01)")'
    )
    command = [sys.executable, "-c", wait_script]
    profile = (
        "\n[repositories.demo.profiles.blocked]\n"
        'description = "Block until the test releases it"\n'
        "verification = true\n"
        f"commands = {json.dumps([command])}\n"
        "timeout_seconds = 60\n"
        "\n[[repositories.demo.profiles.blocked.steps]]\n"
        'id = "tests"\n'
        'kind = "business_tests"\n'
        f"command = {json.dumps(command)}\n"
    )
    env.config_path.write_text(
        env.config_path.read_text(encoding="utf-8") + profile,
        encoding="utf-8",
    )


def _poll(predicate, *, timeout: float = 20.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("Condition was not met before the bounded timeout")


def _worker_loop(config_path: Path) -> tuple[OperationWorkLoop, CodingService]:
    """Build a worker-side loop over the same state root as the request-side service."""
    config = load_config(config_path)
    # The SAME generation the request side serves. At 0 a worker claims any generation
    # (the filter degrades to None), so a mismatch could never be observed -- the mirror
    # of the production bug in #313.
    application = build_application(config, config_generation=TEST_CONFIG_GENERATION)
    service = CodingService(config, application=application)
    handlers = VerificationWorkHandlers(
        WorkspaceProfileRunner(application.context),
        WorkspaceAdhocRunner(application.context),
        WorkspaceDiagnosticRunner(application.context),
    )
    loop = OperationWorkLoop(
        application.context,
        application.operations,
        handlers,
        idle_poll_seconds=0.02,
        heartbeat_interval_seconds=0.5,
        recovery_interval_seconds=60.0,
    )
    return loop, service


@dataclass(frozen=True, slots=True)
class BlockedVerification:
    """One claimed, genuinely blocked verification and the request side that must stay live."""

    request_side: CodingService
    worker_side: CodingService
    workspace_id: str
    idle_workspace_id: str
    operation_id: str
    binding: OperationWorkerBinding
    legacy_background: ManualBackgroundTaskRunner


@contextmanager
def _blocked_verification(tmp_path: Path) -> Iterator[BlockedVerification]:
    """Run a verification whose child cannot exit, and whose workspace lock is therefore held."""
    env = create_forge_environment(tmp_path)
    release = tmp_path / "release-blocked-command"
    _add_blocked_profile(env, release)
    config = load_config(env.config_path)
    legacy_background = ManualBackgroundTaskRunner()
    request_side = CodingService(
        config,
        application=build_application(
            config,
            overrides=AdapterOverrides(background_tasks=legacy_background),
            # Both sides carry the same real generation, as they do in production. Built
            # without it, this side stamped work with 0 while the worker ran the fixture's
            # generation, and nothing could be claimed -- the #313 topology exactly.
            config_generation=TEST_CONFIG_GENERATION,
        ),
    )
    workspace_id = request_side.workspace_create("demo", "blocked control plane")["workspace_id"]
    idle_workspace_id = request_side.workspace_create("demo", "idle bystander")["workspace_id"]
    operation_id = request_side.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="blocked",
        background=True,
    )["operation"]["operation_id"]

    loop, worker_side = _worker_loop(env.config_path)
    claimed: list[bool] = []
    worker = threading.Thread(
        target=lambda: claimed.append(loop.run_once()),
        name="test-durable-execution-worker",
        daemon=True,
    )
    worker.start()
    try:
        bindings = worker_side.application.context.worker_bindings
        assert bindings is not None
        # A bound child proves the worker already holds the workspace lock: it is taken before
        # the command is spawned. Everything measured below therefore races a real lock holder.
        binding = _poll(lambda: bindings.get(operation_id))
        yield BlockedVerification(
            request_side,
            worker_side,
            workspace_id,
            idle_workspace_id,
            operation_id,
            binding,
            legacy_background,
        )
    finally:
        release.write_text("go", encoding="utf-8")
        loop.request_stop()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert claimed == [True]


def test_blocked_verification_does_not_block_the_control_plane(tmp_path) -> None:
    """Catch a regression that puts a repository command back on the request path."""
    with _blocked_verification(tmp_path) as blocked:
        request_side = blocked.request_side
        operation_id = blocked.operation_id

        started = time.monotonic()
        operation = request_side.operation("get", operation_id=operation_id)["operation"]
        listed = request_side.operation_list(state="running")
        status = request_side.workspace_status(blocked.idle_workspace_id)
        elapsed = time.monotonic() - started

        assert elapsed < _CONTROL_PLANE_BUDGET_SECONDS
        assert operation["state"] == "running"
        assert operation["heartbeat_at"] is not None
        assert any(item["operation_id"] == operation_id for item in listed["operations"])
        assert status["workspace_id"] == blocked.idle_workspace_id
        # An untouched workspace is still read under its lock, so nothing degraded silently.
        assert status["read_consistency"] == "locked"
        # The command runs in the worker, never in the process that took the request.
        assert blocked.binding.server_pid != 0
        assert blocked.legacy_background.keys == ()

        cancel_started = time.monotonic()
        cancelled = request_side.operation_cancel(operation_id)
        assert time.monotonic() - cancel_started < _CONTROL_PLANE_BUDGET_SECONDS
        assert cancelled["cancellation_requested"] is True

    terminal = blocked.request_side.operation("get", operation_id=operation_id)["operation"]
    assert terminal["state"] in {"cancelled", "succeeded"}
    assert terminal["phase"] in {"cancelled", "succeeded"}


def test_status_of_the_busy_workspace_answers_promptly_and_says_it_was_unsynchronized(
    tmp_path,
) -> None:
    """The workspace under verification must answer too -- honestly, not eventually."""
    with _blocked_verification(tmp_path) as blocked:
        request_side = blocked.request_side
        busy_workspace_id = blocked.workspace_id

        legacy_started = time.monotonic()
        legacy = request_side.workspace_status(busy_workspace_id)
        legacy_elapsed = time.monotonic() - legacy_started

        v2_started = time.monotonic()
        v2 = request_side.workspace_status_v2(busy_workspace_id, sections=("local",))
        v2_elapsed = time.monotonic() - v2_started

        assert legacy_elapsed < _CONTROL_PLANE_BUDGET_SECONDS
        assert v2_elapsed < _CONTROL_PLANE_BUDGET_SECONDS
        assert legacy["workspace_id"] == busy_workspace_id
        assert v2["workspace_id"] == busy_workspace_id
        # The read happened alongside the command instead of waiting it out, and says so.
        assert legacy["read_consistency"] == "concurrent_write"
        assert v2["read_consistency"] == "concurrent_write"
        assert legacy["head_sha"] == v2["head_sha"]


def test_queued_work_survives_a_request_process_restart_and_runs_once(tmp_path) -> None:
    """A queued job must outlive the process that admitted it and execute exactly once."""
    env = create_forge_environment(tmp_path)
    release = tmp_path / "release-restart-command"
    release.write_text("go", encoding="utf-8")
    _add_blocked_profile(env, release)
    config = load_config(env.config_path)
    admitting = CodingService(
        config,
        application=build_application(config, config_generation=TEST_CONFIG_GENERATION),
    )
    workspace_id = admitting.workspace_create("demo", "restart durable work")["workspace_id"]
    operation_id = admitting.workspace_verify(
        workspace_id,
        mode="profile",
        profile_name="blocked",
        background=True,
    )["operation"]["operation_id"]
    del admitting

    loop, worker_side = _worker_loop(env.config_path)
    assert loop.run_once() is True
    # A second worker pass finds nothing left to claim: the attempt ran once.
    assert loop.run_once() is False

    restarted_config = load_config(env.config_path)
    restarted = CodingService(
        restarted_config,
        application=build_application(restarted_config, config_generation=TEST_CONFIG_GENERATION),
    )
    terminal = restarted.operation("get", operation_id=operation_id)["operation"]
    assert terminal["state"] == "succeeded"
    assert terminal["phase"] == "succeeded"
    assert terminal["result_reference"] == f"operation-result:{operation_id}"
    assert terminal["evidence_complete"] is True
    queue = worker_side.application.context.operation_work_queue
    assert queue is not None
    assert queue.read(operation_id) is None
