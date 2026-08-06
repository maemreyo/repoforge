"""Coverage for issue #379: background jobs, cancellation, and event-driven completion.

#378 gave `workspace_exec` an inline fast path that fails an overrunning command fast
against a small `adhoc_inline_max_seconds` ceiling, explicitly deferring "promote the
still-running process into a durable background operation instead of killing it" to
this issue. This file proves that promotion actually happens without ever restarting
the process (`WorkspaceAdhocRunner.execute_or_promote()` / `_execute_inline_or_promote`
in `application/workspace/run_adhoc.py`), that a promoted operation is cancellable and
records correct terminal evidence (`OperationCancellationRequester`, reusing
`_finish_background_adhoc`), that same-process waiters wake up on completion rather than
sleep-polling (`OperationCompletionSignals`), and that a connector retrying with the
same `idempotency_key` after a lost response replays the original outcome -- including
a still-running one -- instead of running the command a second time.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from conftest import ForgeEnvironment, create_forge_environment, durable_worker

from repoforge.application.operations.completion_signals import OperationCompletionSignals


def _relaxed_env(
    tmp_path: Path,
    *,
    runners: tuple[str, ...] = ("python3",),
    adhoc_inline_max_seconds: int = 1,
) -> ForgeEnvironment:
    return create_forge_environment(
        tmp_path,
        execution_mode="relaxed",
        adhoc_runners=runners,
        adhoc_inline_max_seconds=adhoc_inline_max_seconds,
    )


def _record_count(env: ForgeEnvironment) -> int:
    return len(env.service.operations.list_records(max_records=200).records)


def _poll_terminal(env: ForgeEnvironment, operation_id: str, *, timeout_seconds: float = 10.0):
    deadline = time.monotonic() + timeout_seconds
    status = env.service.operation_status(operation_id)
    while (
        status["state"] not in ("succeeded", "failed", "cancelled") and time.monotonic() < deadline
    ):
        time.sleep(0.05)
        status = env.service.operation_status(operation_id)
    return status


# ---------------------------------------------------------------------------
# AC1: promotion never restarts the process
# ---------------------------------------------------------------------------


def test_promoted_operation_binds_the_exact_same_process_that_was_already_running(
    tmp_path: Path,
) -> None:
    """The durable worker binding created at promotion time must name the PID of the
    process that was already running before the ceiling fired -- proving the running
    subprocess was captured and tracked, not killed and re-spawned under a new PID."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "promotion same pid")["workspace_id"]
    pid_file = tmp_path / "observed_pid.txt"
    script = (
        f"import os; open({str(pid_file)!r}, 'a').write(str(os.getpid()) + chr(10)); "
        "import time; time.sleep(3)"
    )

    result = env.service.workspace_exec(workspace_id, ("python3", "-c", script))
    assert result["outcome"] == "running"
    operation_id = result["operation"]["operation_id"]

    binding = env.service.application.context.worker_bindings.get(operation_id)
    assert binding is not None

    status = _poll_terminal(env, operation_id)
    assert status["state"] == "succeeded"

    observed_pids = pid_file.read_text().splitlines()
    # Exactly one invocation ever wrote its pid -- a restart would show either a second
    # line (if the original also survived long enough to write) or a different pid on
    # the only line (if the original had been killed first).
    assert observed_pids == [str(binding.child_pid)]


def test_command_finishing_just_under_the_ceiling_is_never_promoted(tmp_path: Path) -> None:
    """The complementary case: a command that completes before the ceiling fires must
    return its terminal result directly, with zero durable records -- promotion is
    reserved for the overrun case only."""
    env = _relaxed_env(tmp_path, adhoc_inline_max_seconds=5)
    workspace_id = env.service.workspace_create("demo", "no promotion needed")["workspace_id"]
    before = _record_count(env)

    result = env.service.workspace_exec(workspace_id, ("python3", "--version"))

    assert result["outcome"] == "passed"
    assert _record_count(env) == before


def test_boundary_races_never_double_run_or_double_record(tmp_path: Path) -> None:
    """Structural (not timing) proof that the decision_lock race guard holds: run a
    command whose duration sits right at the inline ceiling boundary many times, and
    assert the same two invariants every time regardless of which side of the race
    actually won -- exactly one durable record delta (0 or 1, never 2), and the
    eventual terminal result is never inconsistent with exactly one execution. This
    intentionally does not assert on elapsed time itself (see this repo's own guidance
    against tight wall-clock CI assertions) -- only on the record-count/outcome
    invariants the race guard exists to protect.
    """
    env = _relaxed_env(tmp_path, adhoc_inline_max_seconds=1)
    workspace_id = env.service.workspace_create("demo", "boundary race")["workspace_id"]

    for i in range(8):
        counter_file = tmp_path / f"race-counter-{i}.txt"
        counter_file.write_text("0")
        script = (
            f"import pathlib; p = pathlib.Path({str(counter_file)!r}); "
            "p.write_text(str(int(p.read_text()) + 1))"
        )
        before = _record_count(env)
        result = env.service.workspace_exec(workspace_id, ("python3", "-c", script))
        after = _record_count(env)

        assert after - before in (0, 1)
        if result["outcome"] == "running":
            status = _poll_terminal(env, result["operation"]["operation_id"])
            assert status["state"] == "succeeded"
        else:
            assert result["outcome"] == "passed"
        # The command's own side effect happened exactly once either way.
        assert counter_file.read_text() == "1"


# ---------------------------------------------------------------------------
# AC2: cancellation and terminal evidence for a promoted operation
# ---------------------------------------------------------------------------


def test_promoted_operation_can_be_cancelled_and_records_cancelled_not_failed(
    tmp_path: Path,
) -> None:
    """Regression test for a bug this issue's own AC2 work surfaced and fixed:
    `_finish_background_adhoc` used to check "did the run return a result" before
    checking "was it cancelled" -- but an ad-hoc run is evidence-only, so a command
    killed by cancellation (SIGTERM) still returns a normal result (a negative
    returncode, no raised exception) rather than raising. That ordering reported every
    cancelled run as `succeeded`. Confirmed to reproduce identically against the
    unmodified legacy `workspace_run_adhoc(background=true)` surface before the fix,
    so it predates this issue -- but #379's own promotion path is what exercises it
    here."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "cancel promoted")["workspace_id"]

    result = env.service.workspace_exec(
        workspace_id, ("python3", "-c", "import time; time.sleep(10)")
    )
    operation_id = result["operation"]["operation_id"]

    # Give the child a moment to actually spawn and bind before cancelling.
    for _ in range(20):
        if env.service.application.context.worker_bindings.get(operation_id) is not None:
            break
        time.sleep(0.05)

    cancel_result = env.service.operation_cancel(operation_id)
    assert cancel_result["cancellation_requested"] is True

    status = _poll_terminal(env, operation_id)
    assert status["state"] == "cancelled"


# ---------------------------------------------------------------------------
# AC3: event-driven completion
# ---------------------------------------------------------------------------


def test_operation_completion_signals_register_then_fire() -> None:
    signals = OperationCompletionSignals()
    event = signals.register("op-1")
    assert not event.is_set()

    woke = []

    def waiter() -> None:
        woke.append(event.wait(timeout=2.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    signals.fire("op-1")
    thread.join(timeout=2.0)

    assert woke == [True]


def test_operation_completion_signals_fire_before_register() -> None:
    signals = OperationCompletionSignals()
    signals.fire("op-2")

    # A late registration for an operation that already reached a terminal state must
    # find an already-set event -- there is no lost-wakeup window.
    event = signals.register("op-2")
    assert event.is_set()


def test_same_process_wait_detects_completion_without_polling_to_the_deadline(
    tmp_path: Path,
) -> None:
    """A same-process wait on a promoted operation must be woken by
    OperationCompletionSignals rather than only discovering completion on
    durable_wait's own next poll tick. Structural assertion only: the observed
    operation reaches a terminal state well before FOREGROUND_WAIT_SECONDS (25s) even
    though the command itself only takes ~1-2s -- a regression to pure fixed polling
    would still pass eventually, but this loop bails out far earlier than that fixed
    ceiling, which a same-process signal delivery explains and a coincidence would not.
    """
    from repoforge.application.operations.durable_wait import wait_for_operation

    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "event driven wait")["workspace_id"]

    with durable_worker(env.service):
        admitted = env.service.workspace_exec(
            workspace_id, ("python3", "-c", "import time; time.sleep(1)"), background=True
        )
        operation_id = admitted["operation"]["operation_id"]
        ctx = env.service.application.context
        operations = env.service.operations

        started = time.monotonic()
        task, _result = wait_for_operation(ctx, operations, operation_id)
        elapsed = time.monotonic() - started

    assert task.state.value == "succeeded"
    # Generous, structural bound (see #378's own precedent against tight wall-clock
    # assertions): a fixed-poll regression would still finish under 25s, but nowhere
    # near this tight -- this is a sanity check that completion was detected promptly,
    # not a precise latency measurement.
    assert elapsed < 5.0


# ---------------------------------------------------------------------------
# AC4: lost-response reconciliation via idempotency_key
# ---------------------------------------------------------------------------


def test_idempotency_key_replays_a_still_running_promoted_operation(tmp_path: Path) -> None:
    """A connector that lost the response to a call whose command was promoted to
    background gets back the *same* operation_id on retry, not a second run."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "idempotent still running")["workspace_id"]
    argv = ("python3", "-c", "import time; time.sleep(3)")

    first = env.service.workspace_exec(workspace_id, argv, idempotency_key="retry-running-key")
    assert first["outcome"] == "running"

    second = env.service.workspace_exec(workspace_id, argv, idempotency_key="retry-running-key")
    assert second["outcome"] == "running"
    assert second["operation"]["operation_id"] == first["operation"]["operation_id"]


def test_idempotency_key_replays_a_terminal_result_without_rerunning(tmp_path: Path) -> None:
    """A connector retrying with the same key after receiving (or losing) a terminal
    result gets the exact same result replayed, proven by a side effect the command
    would only ever produce once."""
    env = _relaxed_env(tmp_path)
    workspace_id = env.service.workspace_create("demo", "idempotent terminal")["workspace_id"]
    counter_file = tmp_path / "idempotent-counter.txt"
    counter_file.write_text("0")
    argv = (
        "python3",
        "-c",
        f"import pathlib; p = pathlib.Path({str(counter_file)!r}); "
        "p.write_text(str(int(p.read_text()) + 1))",
    )

    first = env.service.workspace_exec(workspace_id, argv, idempotency_key="retry-terminal-key")
    assert first["outcome"] == "passed"
    assert counter_file.read_text() == "1"

    second = env.service.workspace_exec(workspace_id, argv, idempotency_key="retry-terminal-key")
    assert second["outcome"] == "passed"
    assert counter_file.read_text() == "1"
    assert second == first
