"""Phase 0: freeze the crash-safety contracts of the CURRENT runtime control plane.

The subsystem review of the release/activation/runtime control planes found crash
windows that the existing suite does not model. This file pins each one as a
deterministic, injection-based fault test -- no production code is changed, no real
subprocess is spawned, and every assertion documents the exact failure shape that a
later phase (3-9 of the migration) must close:

- F-001: a worker is spawned (Popen) before any durable lease exists.
- F-002: an unregistered worker is declared "terminated and confirmed dead" without
  a final identity/group-liveness re-verification after SIGKILL.
- F-003: ``reconcile(read_only=True)`` still mutates durable state (it archives and
  deletes terminal leases before the read-only scan).
- F-005: the archive checkpoint is not idempotent across a crash between archive
  write and active-record delete, because ``terminated_at`` is re-stamped on retry.
- F-007: a seam-swap abort after a successful drain never sends ``RESUME`` to the
  old child, so "old child retained" does not mean "old service resumed".
- F-008: the operation-worker store silently truncates its scan; the handoff
  reconciler has no completeness signal, so an orphan past the limit is invisible.

These tests pass on the current code -- passing is the proof that the vulnerability
exists, not that it is fixed. When a later phase closes a finding, the corresponding
test here must be tightened to assert the fixed invariant instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_execution_worker_binding_store import (
    JsonExecutionWorkerBindingStore,
)
from repoforge.adapters.persistence.json_worker_binding_store import JsonWorkerBindingStore
from repoforge.adapters.subprocess.process_tree import ProcessIdentity
from repoforge.application.activation.handoff import GenerationHandoffReconciler, OwnerIdentity
from repoforge.application.activation.seam import TunnelSeamSwapCoordinator
from repoforge.application.runtime.execution_worker_reconciler import ExecutionWorkerReconciler
from repoforge.domain.errors import RepoForgeError
from repoforge.domain.execution_worker import ExecutionWorkerArchiveEntry, ExecutionWorkerBinding
from repoforge.domain.operation_worker import OperationWorkerBinding
from repoforge.domain.runtime import ChildProcess, HealthCheck, TunnelProfile
from repoforge.ports.process_reaper import ReapOutcome
from repoforge.testing import InMemoryLockManager
from repoforge.testing.fakes import InMemoryWorkerBindingStore, RecordingProcessReaper

_SHA = "a" * 64
_WORKER = "worker-000000000001"
_EXECUTION_WORKER_ARGV = (
    "/opt/repoforge/venv/bin/python",
    "-m",
    "repoforge.interfaces.runtime.execution_worker",
    "--config",
    "/home/dev/config.toml",
    "--generation",
    "12",
)


def _binding(*, state: str = "running") -> ExecutionWorkerBinding:
    return ExecutionWorkerBinding(
        worker_id=_WORKER,
        pid=4242,
        pgid=4242,
        process_start_token="worker-start-token",
        generation=12,
        release_sha="0123abc",
        supervisor_pid=4241,
        supervisor_process_identity=_SHA,
        correlation_id="c" * 24,
        started_at="2026-07-29T09:26:21+00:00",
        state=state,
    )


def _op_binding(op: str, *, generation: int) -> OperationWorkerBinding:
    return OperationWorkerBinding(
        operation_id=op,
        child_pid=4321,
        child_pgid=4321,
        child_start_token="tok-child",
        server_pid=111,
        server_start_token="tok-server",
        created_at="2026-07-25T00:00:00+00:00",
        owner_generation=generation,
    )


# ---------------------------------------------------------------------------
# F-001 / F-002: spawn precedes durable registration; death is declared
# without re-verification.
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None

    @property
    def stdout(self):
        return None


class _FakePopen:
    """Records the moment Popen is invoked on a shared event list."""

    def __init__(self, pid: int, events: list[str]) -> None:
        self._pid = pid
        self._events = events

    def __call__(self, argv, **kwargs):
        del argv, kwargs
        self._events.append("popen")
        return _FakeProcess(self._pid)


class _RecordingBindings:
    """Records store writes on a shared event list; put can be scripted to fail."""

    def __init__(self, events: list[str], *, fail_put: bool = False) -> None:
        self._events = events
        self._fail_put = fail_put
        self.put_called = 0

    def put(self, binding: ExecutionWorkerBinding) -> None:
        del binding
        self.put_called += 1
        self._events.append("put")
        if self._fail_put:
            raise OSError("simulated crash before the durable lease was written")

    def update_state(self, worker_id: str, state: str):
        del worker_id, state
        return None

    def list_page(self, *, max_records: int = 2_000):
        del max_records
        from repoforge.ports.execution_worker_store import ExecutionWorkerBindingPage

        return ExecutionWorkerBindingPage(records=(), scan_complete=True, unreadable_ids=())


def test_f001_popen_precedes_durable_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker is spawned before its durable lease exists (F-001).

    If the supervisor is SIGKILLed between these two events, the worker runs with
    no active-registry record: no later supervisor can discover, archive, or
    quarantine it, and it can hold operation locks indefinitely.
    """
    import repoforge.adapters.runtime.execution_worker as worker_module

    events: list[str] = []
    monkeypatch.setattr(worker_module.subprocess, "Popen", _FakePopen(pid=4242, events=events))
    monkeypatch.setattr(worker_module, "process_identity", lambda pid: _SHA)
    monkeypatch.setattr(
        worker_module,
        "read_identity",
        lambda pid: ProcessIdentity(pid=pid, ppid=1, start_token="worker-start-token"),
    )
    monkeypatch.setattr(worker_module.time, "sleep", lambda seconds: None)
    bindings = _RecordingBindings(events)
    worker = worker_module.SubprocessExecutionWorker(Path("/tmp/config.toml"), bindings=bindings)

    child = worker.start(
        generation=12,
        env={"REPOFORGE_RUNNING_RELEASE_SHA": "0123abc"},
        log_path=Path("/tmp/worker.log"),
    )

    assert child.pid == 4242
    # Durable registration strictly after spawn: the crash window exists by
    # construction and this ordering is what makes it dangerous.
    assert events == ["popen", "put"]


def test_f002_death_is_declared_without_reverification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM->SIGKILL with no final identity check still claims "confirmed dead".

    The reaper waits a fixed window and returns; the failure path then raises
    ``EXECUTION_WORKER_REGISTRATION_FAILED ... terminated and confirmed dead`` even
    when the process table still reports the process alive (an injected identity
    reader that never returns None). ``confirmed dead`` is intent, not evidence.
    """
    import repoforge.adapters.runtime.execution_worker as worker_module

    events: list[str] = []
    identity_checked_after_kill: list[bool] = []

    def always_live(pid: int) -> str:
        del pid
        identity_checked_after_kill.append(True)
        return "still-alive-identity"

    monkeypatch.setattr(worker_module.subprocess, "Popen", _FakePopen(pid=4242, events=events))
    monkeypatch.setattr(worker_module, "process_identity", always_live)
    monkeypatch.setattr(
        worker_module,
        "read_identity",
        lambda pid: ProcessIdentity(pid=pid, ppid=1, start_token="worker-start-token"),
    )
    monkeypatch.setattr(worker_module.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(worker_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(worker_module, "_REGISTRATION_REAP_SECONDS", 0.01)
    bindings = _RecordingBindings(events, fail_put=True)
    worker = worker_module.SubprocessExecutionWorker(Path("/tmp/config.toml"), bindings=bindings)

    with pytest.raises(Exception) as exc:
        worker.start(
            generation=12,
            env={"REPOFORGE_RUNNING_RELEASE_SHA": "0123abc"},
            log_path=Path("/tmp/worker.log"),
        )

    from repoforge.domain.errors import ExecutionWorkerRegistrationError

    assert isinstance(exc.value, ExecutionWorkerRegistrationError)
    assert "terminated and confirmed dead" in str(exc.value)
    # The process was never observed absent: the "confirmed dead" claim rested on
    # nothing but a sleep window.
    assert identity_checked_after_kill
    assert all(identity_checked_after_kill)


# ---------------------------------------------------------------------------
# F-003: read-only preflight mutates durable state.
# ---------------------------------------------------------------------------


class _NeverCalled:
    def reap(self, binding: object) -> ReapOutcome:
        raise AssertionError("read-only reconcile must never reap")

    def __call__(self, pid: int) -> None:
        del pid
        raise AssertionError("read-only reconcile must never read process identity")


def test_f003_read_only_preflight_archives_terminal_leases(tmp_path: Path) -> None:
    """``reconcile(read_only=True)`` still mutates durable state (F-003).

    The preflight collects terminal leases (archive + delete) before the read-only
    scan, so a caller reasoning "this pass has no effect" is wrong: a terminal lease
    present before the preflight is gone after it. A failure during that cleanup is
    also suppressed, making the preflight's outcome depend on hidden state.
    """
    store = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    store.put(_binding(state="already_gone"))  # terminal lease in the active registry

    reaper = _NeverCalled()
    reconciler = ExecutionWorkerReconciler(
        bindings=store,
        reaper=reaper,
        owner_identity_reader=_NeverCalled(),
        command_line_reader=_NeverCalled(),
        identity_reader=_NeverCalled(),
        process_group_gone=None,
    )

    report = reconciler.reconcile(read_only=True)

    # The preflight was supposed to be read-only, yet the terminal lease was
    # archived and removed from the active registry.
    assert store.get(_WORKER) is None
    assert report.inspected == 0


# ---------------------------------------------------------------------------
# F-005: the archive checkpoint is not idempotent across a crash.
# ---------------------------------------------------------------------------


def test_f005_archive_checkpoint_is_not_idempotent_across_crash(tmp_path: Path) -> None:
    """A crash between archive write and active delete cannot be retried (F-005).

    ``_archive_and_delete`` stamps ``terminated_at`` fresh on every attempt, so a
    retry after the crash builds a different payload than the archive entry that
    already exists; ``create_or_read_equal`` refuses it. The terminal lease is then
    never deleted: the active registry keeps accumulating and can eventually fail
    closed. This is a white-box test: it writes the half-crashed state (archive
    entry present, active terminal record still present) exactly as a crash would
    leave it, then runs the retry.
    """
    store = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    terminal = _binding(state="already_gone")
    store.put(terminal)
    # Half of a crashed `_archive_and_delete`: the archive entry exists with the
    # first attempt's timestamp, the active terminal record is still present.
    first_attempt = ExecutionWorkerArchiveEntry.from_binding(
        terminal, terminated_at="2026-07-30T00:00:00+00:00"
    )
    store._history.create_or_read_equal(_WORKER, first_attempt)

    with pytest.raises(RepoForgeError) as exc:
        store.collect_terminal()

    assert "already exists" in str(exc.value)
    # Self-heal failed: the active terminal lease is still there.
    assert store.get(_WORKER) is not None


# ---------------------------------------------------------------------------
# F-007: a seam-swap abort after a successful drain never resumes the old child.
# ---------------------------------------------------------------------------


class _Sleeper:
    def sleep(self, seconds: float) -> None:
        del seconds


class _FakeTunnel:
    def __init__(self) -> None:
        self.terminated: list[int] = []

    def start(self, profile: TunnelProfile, *, env: dict[str, str], log_path: Path) -> ChildProcess:
        del profile, env, log_path
        return ChildProcess(pid=999, process_identity=_SHA, started_at="2026-07-25T00:00:00+00:00")

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None:
        del grace_seconds
        self.terminated.append(child.pid)

    def is_alive(self, child: ChildProcess) -> bool:
        del child
        return True

    def health(self, child: ChildProcess, *, timeout_seconds: float) -> tuple[HealthCheck, ...]:
        del timeout_seconds
        return (HealthCheck(name="tunnel", ok=True, detail="ok"),)


class _Control:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def request(self, request, *, timeout_seconds: float = 10.0):
        from repoforge.domain.runtime import ControlResponse

        del timeout_seconds
        self.commands.append(request.command.value)
        return ControlResponse(1, True, request.correlation_id, "drained")


class _Ids:
    def new_hex(self, length: int = 10) -> str:
        del length
        return "c" * 24


def test_f007_handoff_abort_never_resumes_the_drained_old_child() -> None:
    """An abort after a successful drain drops the old child without RESUME (F-007).

    The drain is not reversible: ``ControlCommand.RESUME`` exists in the protocol,
    but the conflict-abort path only terminates the candidate and reports "old
    child retained". A child that drained is not a child that serves: it may still
    be alive yet refuse new work, which is a logical outage with a healthy-looking
    process tree. The report must prove the old service was resumed, not just that
    its pid was not killed.
    """
    store = InMemoryWorkerBindingStore()
    # A prior generation's worker that survives termination: the handoff must fail
    # closed, aborting the swap.
    store.put(_op_binding("op-000000000000000000000001", generation=6))
    reaper = RecordingProcessReaper(
        outcome=ReapOutcome(attempted=True, reaped=False, still_alive=True, detail="survived")
    )
    reconciler = GenerationHandoffReconciler(bindings=store, reaper=reaper)
    tunnel = _FakeTunnel()
    control = _Control()
    coordinator = TunnelSeamSwapCoordinator(
        tunnel=tunnel,
        reconciler=reconciler,
        sleeper=_Sleeper(),
        control=control,
        ids=_Ids(),
        health_attempts=3,
        health_interval_seconds=0.0,
    )

    result = coordinator.swap(
        old_child=ChildProcess(
            pid=111, process_identity=_SHA, started_at="2026-07-25T00:00:00+00:00"
        ),
        candidate_profile=TunnelProfile(
            tunnel_id_fingerprint=_SHA,
            profile="tunnel",
            executable="rf",
            executable_version="2.2.0",
            mcp_argv=("rf", "serve"),
        ),
        env={},
        log_path=Path("/tmp/tunnel.log"),
        current_owner=OwnerIdentity(server_pid=222, server_start_token="tok", generation=7),
        old_surface_hash="old",
        new_surface_hash="new",
    )

    assert result.status == "aborted"
    assert control.commands == ["drain"]
    # RESUME exists in the protocol but is never sent: the old child was drained
    # and never told to serve again.
    assert "resume" not in control.commands
    assert tunnel.terminated == [999]


# ---------------------------------------------------------------------------
# F-008: operation-worker scan truncation is silent.
# ---------------------------------------------------------------------------


def test_f008_operation_worker_scan_truncation_is_silent(tmp_path: Path) -> None:
    """A scan truncated at ``max_records`` carries no completeness signal (F-008).

    The execution-worker registry exposes ``scan_complete`` and ``unreadable_ids``
    so a reconciler fails closed on an incomplete scan. The operation-worker store
    discards those signals and returns a bare tuple, so the handoff reconciler
    believes it reconciled the whole registry when an orphan past the limit -- a
    prior generation's worker still producing side effects -- was never seen.
    """
    store = JsonWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    # The filesystem scan truncates by ascending lexicographic filename, so with
    # max_records=5 the record truncated is op-...06 -- which is exactly the prior
    # generation's orphan whose side effects the handoff must not miss.
    for index in range(1, 7):
        store.put(_op_binding(f"op-{index:024x}", generation=6 if index == 6 else 7))

    reconciler = GenerationHandoffReconciler(
        bindings=store, reaper=RecordingProcessReaper(), max_records=5
    )
    report = reconciler.reconcile(
        current_owner=OwnerIdentity(server_pid=111, server_start_token="tok-server", generation=7)
    )

    # Five of six records seen; the truncated prior-generation binding is absent
    # from every field, and no field says the scan was incomplete.
    assert report.scanned == 5
    assert report.ok
    assert "op-000000000000000000000006" not in report.retained
    assert "op-000000000000000000000006" not in report.reaped
    assert "op-000000000000000000000006" not in report.released
    assert "op-000000000000000000000006" not in report.resumable_kept
    assert "op-000000000000000000000006" not in [op for op, _ in report.conflicts]
