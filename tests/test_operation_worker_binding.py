"""Tests for #256: worker binding + reap-on-recovery + truthful orphan/expire."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.persistence.json_worker_binding_store import JsonWorkerBindingStore
from repoforge.adapters.subprocess.os_process_reaper import OsProcessReaper
from repoforge.adapters.subprocess.process_tree import ProcessIdentity
from repoforge.application.operations.recovery import reap_running_background, recover_operations
from repoforge.application.workspace.run_adhoc import WorkspaceAdhocRunner
from repoforge.domain.errors import RepoForgeError
from repoforge.domain.operation_task import OperationState
from repoforge.domain.operation_worker import (
    OperationWorkerBinding,
    validate_operation_worker_binding,
    worker_binding_from_payload,
    worker_binding_payload,
)
from repoforge.testing.fakes import (
    FixedClock,
    InMemoryLockManager,
    InMemoryWorkerBindingStore,
    RecordingProcessReaper,
)


def _binding(
    *,
    operation_id: str = "op-0000000000000000000000aa",
    child_pid: int = 4321,
    child_start_token: str | None = "tok-child",
) -> OperationWorkerBinding:
    return OperationWorkerBinding(
        operation_id=operation_id,
        child_pid=child_pid,
        child_pgid=child_pid,
        child_start_token=child_start_token,
        server_pid=999,
        server_start_token="tok-server",
        created_at="2026-07-23T00:00:00+00:00",
    )


# --------------------------------------------------------------------------- domain


def test_worker_binding_payload_round_trip() -> None:
    binding = _binding()
    restored = worker_binding_from_payload(worker_binding_payload(binding))
    assert restored == binding


def test_worker_binding_rejects_non_positive_pid() -> None:
    with pytest.raises(RepoForgeError):
        validate_operation_worker_binding(_binding(child_pid=0))


def test_worker_binding_from_payload_rejects_extra_fields() -> None:
    payload = worker_binding_payload(_binding())
    payload["unexpected"] = 1
    with pytest.raises(RepoForgeError):
        worker_binding_from_payload(payload)


# ---------------------------------------------------------------------------- store


def test_json_worker_binding_store_crud(tmp_path) -> None:
    store = JsonWorkerBindingStore(tmp_path, InMemoryLockManager())
    binding = _binding()
    store.put(binding)
    assert store.get(binding.operation_id) == binding
    # put is idempotent overwrite
    store.put(binding)
    assert store.get(binding.operation_id) == binding
    assert store.list_all() == (binding,)
    store.delete(binding.operation_id)
    assert store.get(binding.operation_id) is None
    # delete is idempotent
    store.delete(binding.operation_id)


# --------------------------------------------------------------------------- reaper


def test_reaper_reports_already_gone_when_child_absent() -> None:
    reaper = OsProcessReaper(identity_reader=lambda pid: None)
    outcome = reaper.reap(_binding())
    assert outcome.attempted is False
    assert outcome.reaped is True
    assert outcome.still_alive is False


def test_reaper_fails_closed_on_pid_reuse() -> None:
    signalled: list[tuple[int, int]] = []
    reaper = OsProcessReaper(
        identity_reader=lambda pid: ProcessIdentity(pid=pid, ppid=1, start_token="DIFFERENT"),
        killpg=lambda pgid, sig: signalled.append((pgid, sig)),
    )
    outcome = reaper.reap(_binding(child_start_token="tok-child"))
    assert outcome.attempted is False
    assert outcome.reaped is False
    assert signalled == []  # never signalled a recycled pid


def test_reaper_reaps_via_sigterm() -> None:
    calls = {"n": 0}
    signalled: list[tuple[int, int]] = []

    def identity_reader(pid: int) -> ProcessIdentity | None:
        # alive for the first two reads (guard + first liveness poll), then gone
        calls["n"] += 1
        if calls["n"] >= 3:
            return None
        return ProcessIdentity(pid=pid, ppid=1, start_token="tok-child")

    reaper = OsProcessReaper(
        identity_reader=identity_reader,
        killpg=lambda pgid, sig: signalled.append((pgid, sig)),
        sleeper=lambda _s: None,
        monotonic=lambda: 0.0,
    )
    import signal

    outcome = reaper.reap(_binding())
    assert outcome.reaped is True
    assert signalled and signalled[0][1] == signal.SIGTERM


def test_reaper_read_start_token() -> None:
    reaper = OsProcessReaper(
        identity_reader=lambda pid: ProcessIdentity(pid=pid, ppid=1, start_token="tok-xyz"),
    )
    assert reaper.read_start_token(1234) == "tok-xyz"
    assert reaper.read_start_token(0) is None


# --------------------------------------------------------------- recovery integration


def test_recovery_reaps_and_orphans_with_truthful_reason(forge_env: ForgeEnvironment) -> None:
    manager = forge_env.service.operations
    running = manager.create(kind="watch", phase="polling", cancel_supported=True)
    manager.start(running.operation_id)

    bindings = InMemoryWorkerBindingStore()
    bindings.put(_binding(operation_id=running.operation_id, child_pid=55555))
    reaper = RecordingProcessReaper()

    report = recover_operations(
        manager,
        now="2026-07-24T00:00:00+00:00",
        worker_bindings=bindings,
        reaper=reaper,
    )

    assert report.orphaned == 1
    assert report.reaped == 1
    orphaned = manager.status(running.operation_id)
    assert orphaned.state is OperationState.ORPHANED
    assert orphaned.error_message is not None
    assert "OPERATION_WORKER_LOST" in orphaned.error_message
    assert "pgid=55555" in orphaned.error_message
    # binding is consumed once the child is reaped
    assert bindings.get(running.operation_id) is None
    assert [b.child_pid for b in reaper.reaped] == [55555]


def test_recovery_orphans_without_binding_records_reason(forge_env: ForgeEnvironment) -> None:
    manager = forge_env.service.operations
    running = manager.create(kind="watch", phase="polling", cancel_supported=True)
    manager.start(running.operation_id)

    report = recover_operations(
        manager,
        now="2026-07-24T00:00:00+00:00",
        worker_bindings=InMemoryWorkerBindingStore(),
        reaper=RecordingProcessReaper(),
    )
    assert report.orphaned == 1
    assert report.reaped == 0
    orphaned = manager.status(running.operation_id)
    assert orphaned.error_message is not None
    assert "no live worker binding" in orphaned.error_message


def test_recovery_prunes_stale_binding_for_terminal_op(forge_env: ForgeEnvironment) -> None:
    manager = forge_env.service.operations
    done = manager.create(kind="watch", phase="queued", cancel_supported=True)
    manager.start(done.operation_id)
    manager.succeed(done.operation_id, result_reference="result-x")

    bindings = InMemoryWorkerBindingStore()
    bindings.put(_binding(operation_id=done.operation_id))

    report = recover_operations(
        manager,
        now="2026-07-24T00:00:00+00:00",
        worker_bindings=bindings,
        reaper=RecordingProcessReaper(),
    )
    assert report.bindings_pruned == 1
    assert bindings.get(done.operation_id) is None


# ----------------------------------------------------------------- manager messages


def test_orphan_and_expire_always_carry_error_message(forge_env: ForgeEnvironment) -> None:
    manager = forge_env.service.operations
    orphan_target = manager.create(kind="watch", phase="queued", cancel_supported=True)
    manager.start(orphan_target.operation_id)
    orphaned = manager.orphan(orphan_target.operation_id)
    assert orphaned.error_message is not None
    assert orphaned.error_code == "OPERATION_WORKER_LOST"

    expire_target = manager.create(kind="index", phase="queued", cancel_supported=True)
    expired = manager.expire(expire_target.operation_id)
    assert expired.error_message is not None
    assert expired.error_code == "OPERATION_EXPIRED"


# ------------------------------------------------------- run_adhoc binding lifecycle


def _adhoc_runner_with(ctx_extras: dict[str, object]) -> WorkspaceAdhocRunner:
    ctx = SimpleNamespace(clock=FixedClock(), **ctx_extras)
    return WorkspaceAdhocRunner(ctx)  # type: ignore[arg-type]


def test_persist_worker_binding_records_child_group() -> None:
    bindings = InMemoryWorkerBindingStore()
    reaper = RecordingProcessReaper(start_tokens={7777: "tok-child"})
    runner = _adhoc_runner_with({"worker_bindings": bindings, "reaper": reaper})

    runner._persist_worker_binding("op-0000000000000000000000bb", 7777)

    stored = bindings.get("op-0000000000000000000000bb")
    assert stored is not None
    assert stored.child_pid == 7777
    assert stored.child_pgid == 7777
    assert stored.child_start_token == "tok-child"


def test_cross_process_cancel_falls_back_to_reaper() -> None:
    bindings = InMemoryWorkerBindingStore()
    binding = _binding(operation_id="op-0000000000000000000000cc")
    bindings.put(binding)
    reaper = RecordingProcessReaper()
    runner = _adhoc_runner_with({"worker_bindings": bindings, "reaper": reaper})

    # no in-memory token registered (simulating a post-restart process)
    assert runner.request_live_cancel("op-0000000000000000000000cc") is True
    assert [b.operation_id for b in reaper.reaped] == ["op-0000000000000000000000cc"]
    assert bindings.get("op-0000000000000000000000cc") is None


def test_cross_process_cancel_returns_false_without_binding() -> None:
    runner = _adhoc_runner_with(
        {"worker_bindings": InMemoryWorkerBindingStore(), "reaper": RecordingProcessReaper()}
    )
    assert runner.request_live_cancel("op-0000000000000000000000dd") is False


# ------------------------------------------------------------ #260 shutdown reap


def test_reap_running_background_reaps_and_orphans(forge_env: ForgeEnvironment) -> None:
    manager = forge_env.service.operations
    running = manager.create(kind="workspace_run_adhoc", phase="running", cancel_supported=True)
    manager.start(running.operation_id)

    bindings = InMemoryWorkerBindingStore()
    bindings.put(_binding(operation_id=running.operation_id, child_pid=44444))
    reaper = RecordingProcessReaper()

    count = reap_running_background(
        manager,
        now="2026-07-24T00:00:00+00:00",
        reason="OPERATION_WORKER_LOST: reaped at runtime shutdown",
        resumable_kinds=frozenset({"pr_check_watch"}),
        worker_bindings=bindings,
        reaper=reaper,
    )

    assert count == 1
    orphaned = manager.status(running.operation_id)
    assert orphaned.state is OperationState.ORPHANED
    assert orphaned.error_message is not None
    assert "runtime shutdown" in orphaned.error_message
    assert [b.child_pid for b in reaper.reaped] == [44444]
    assert bindings.get(running.operation_id) is None


def test_reap_running_background_leaves_resumable_kind(forge_env: ForgeEnvironment) -> None:
    manager = forge_env.service.operations
    watch = manager.create(kind="pr_check_watch", phase="polling", cancel_supported=True)
    manager.start(watch.operation_id)

    count = reap_running_background(
        manager,
        now="2026-07-24T00:00:00+00:00",
        reason="OPERATION_WORKER_LOST: reaped at runtime shutdown",
        resumable_kinds=frozenset({"pr_check_watch"}),
        worker_bindings=InMemoryWorkerBindingStore(),
        reaper=RecordingProcessReaper(),
    )

    assert count == 0
    assert manager.status(watch.operation_id).state is OperationState.RUNNING


def test_service_reap_background_workers(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    manager = service.operations
    running = manager.create(kind="workspace_run_adhoc", phase="running", cancel_supported=True)
    manager.start(running.operation_id)
    service.application.context.worker_bindings.put(
        _binding(operation_id=running.operation_id, child_pid=33333)
    )

    # idempotent: second call is a no-op (already terminal)
    assert service.reap_background_workers(reason="runtime shutdown") == 1
    assert service.reap_background_workers(reason="runtime shutdown") == 0
    assert manager.status(running.operation_id).state is OperationState.ORPHANED
