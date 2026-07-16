"""Coverage for issue #145: background workspace_run_profile via durable operations."""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.testing.fakes import ManualBackgroundTaskRunner

_SLOW_PROFILE_TOML = (
    "\n[repositories.demo.profiles.slow]\n"
    'description = "Sleep briefly for background/cancellation tests"\n'
    "verification = true\n"
    'commands = [["python3", "-c", "import time; time.sleep(2)"]]\n'
    "timeout_seconds = 30\n"
)


def _add_slow_profile(env: ForgeEnvironment) -> None:
    text = env.config_path.read_text(encoding="utf-8")
    env.config_path.write_text(text + _SLOW_PROFILE_TOML, encoding="utf-8")


def _set_server_field(env: ForgeEnvironment, line: str) -> None:
    text = env.config_path.read_text(encoding="utf-8")
    assert "path_prefixes = " in text
    text = text.replace("path_prefixes = ", f"{line}\npath_prefixes = ", 1)
    env.config_path.write_text(text, encoding="utf-8")


def _manual_service(env: ForgeEnvironment) -> tuple[CodingService, ManualBackgroundTaskRunner]:
    runner = ManualBackgroundTaskRunner()
    config = load_config(env.config_path)
    application = build_application(config, overrides=AdapterOverrides(background_tasks=runner))
    return CodingService(config, application=application), runner


def _reload_service(env: ForgeEnvironment) -> CodingService:
    """Build a fresh CodingService with the real thread runner and real fcntl locks."""
    return CodingService(load_config(env.config_path))


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    audit_path = root / "state" / "audit.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    return [event for event in events if event["action"] == action]


def _poll(predicate, *, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("Condition was not met before the bounded timeout")


# ---------------------------------------------------------------------------
# background=false is byte-for-byte unchanged
# ---------------------------------------------------------------------------


def test_background_false_keeps_synchronous_contract(forge_env: ForgeEnvironment) -> None:
    created = forge_env.service.workspace_create("demo", "contract check")
    workspace_id = created["workspace_id"]

    implicit = forge_env.service.workspace_run_profile(workspace_id, "quick")
    explicit = forge_env.service.workspace_run_profile(workspace_id, "quick", background=False)

    assert set(implicit) == {
        "workspace_id",
        "repo_id",
        "profile",
        "description",
        "verification",
        "fingerprint",
        "commands",
        "change_metrics",
        "satisfies_commit_gate",
        "used_default",
        "head_sha",
        "command_source_dirty",
        "command_source_dirty_paths",
        "command_source_warning",
        "working_directory",
    }
    assert implicit == explicit
    assert implicit["profile"] == "quick"
    assert implicit["verification"] is False


# ---------------------------------------------------------------------------
# background=true admission, locking, and lifecycle (deterministic, manual runner)
# ---------------------------------------------------------------------------


def test_background_admission_returns_fast_and_holds_the_workspace_lock(
    forge_env: ForgeEnvironment,
) -> None:
    _add_slow_profile(forge_env)
    service, runner = _manual_service(forge_env)
    created = service.workspace_create("demo", "background admission")
    workspace_id = created["workspace_id"]

    started = time.monotonic()
    result = service.workspace_run_profile(workspace_id, "slow", background=True)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert set(result) == {"operation_id", "phase", "safe_next_action"}
    assert result["phase"] == "running"
    operation_id = result["operation_id"]

    status = service.operation_status(operation_id)
    assert status["kind"] == "workspace_run_profile"
    assert status["state"] == "running"
    assert status["workspace_id"] == workspace_id
    assert status["cancel_supported"] is True

    # The workspace lock is held for the whole background run.
    locks = service.application.context.locks
    with (
        pytest.raises(RepoForgeError) as timeout_exc,
        locks.lock(workspace_id, timeout_seconds=0.1),
    ):
        pass
    assert timeout_exc.value.code is ErrorCode.LOCK_TIMEOUT

    # Let the queued closure actually run (manual runner does not run automatically).
    runner.run(operation_id)

    final = service.operation_status(operation_id)
    assert final["state"] == "succeeded"
    assert final["result_reference"] == f"workspace_run_profile:{operation_id}"
    result_payload = final["result"]
    assert result_payload["workspace_id"] == workspace_id
    assert result_payload["profile"] == "slow"
    assert result_payload["verification"] is True
    assert result_payload["satisfies_commit_gate"] is True
    assert len(result_payload["head_sha"]) == 40
    assert len(result_payload["fingerprint"]) == 64
    assert result_payload["commands"]

    restarted = CodingService(load_config(forge_env.config_path))
    assert restarted.operation_status(operation_id)["result"] == result_payload

    # The lock is released once the run finishes.
    with locks.lock(workspace_id, timeout_seconds=0.5):
        pass

    receipt = service.workspace_status(workspace_id)["last_verification"]
    assert receipt is not None
    assert receipt["profile"] == "slow"


def test_background_submit_raising_releases_lock_and_fails_the_operation(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `background_tasks.submit(...)` raises instead of returning False, the operation
    must not be left stuck in RUNNING while holding the workspace lock forever."""
    _add_slow_profile(forge_env)
    service, runner = _manual_service(forge_env)
    created = service.workspace_create("demo", "submit raises")
    workspace_id = created["workspace_id"]

    def raising_submit(key: str, task: object) -> bool:
        raise RuntimeError("executor pool exhausted")

    monkeypatch.setattr(runner, "submit", raising_submit)

    with pytest.raises(RuntimeError, match="executor pool exhausted"):
        service.workspace_run_profile(workspace_id, "slow", background=True)

    # The workspace lock must not be leaked when admission fails after the operation
    # record already transitioned to RUNNING.
    locks = service.application.context.locks
    with locks.lock(workspace_id, timeout_seconds=0.5):
        pass

    # The operation fails closed instead of being left stuck in RUNNING.
    records = [
        record
        for record in service.operations.list_records(max_records=10).records
        if record.workspace_id == workspace_id
    ]
    assert len(records) == 1
    status = service.operation_status(records[0].operation_id)
    assert status["state"] == "failed"
    assert status["error_code"] == "INTERNAL_ERROR"


def test_background_completion_matches_synchronous_receipt_audit_and_metrics(
    forge_env: ForgeEnvironment,
) -> None:
    _add_slow_profile(forge_env)
    service, runner = _manual_service(forge_env)

    sync_created = service.workspace_create("demo", "sync parity baseline")
    sync_workspace_id = sync_created["workspace_id"]
    sync_result = service.workspace_run_profile(sync_workspace_id, "slow")

    bg_created = service.workspace_create("demo", "background parity check")
    bg_workspace_id = bg_created["workspace_id"]
    bg_admission = service.workspace_run_profile(bg_workspace_id, "slow", background=True)
    runner.run(bg_admission["operation_id"])

    bg_receipt = service.workspace_status(bg_workspace_id)["last_verification"]
    assert bg_receipt is not None
    assert bg_receipt["profile"] == sync_result["profile"] == "slow"
    assert bg_receipt["fingerprint_matches"] is True

    sync_events = [
        event
        for event in _audit_events(forge_env.root, "workspace_run_profile")
        if event["details"].get("workspace_id") == sync_workspace_id and event["success"]
    ]
    bg_events = [
        event
        for event in _audit_events(forge_env.root, "workspace_run_profile")
        if event["details"].get("workspace_id") == bg_workspace_id and event["success"]
    ]
    assert len(sync_events) == 1
    assert len(bg_events) == 1
    sync_details, bg_details = sync_events[0]["details"], bg_events[0]["details"]
    # Same bounded audit shape; only workspace-identifying and timing/size values differ.
    assert (
        set(sync_details)
        == set(bg_details)
        == {
            "workspace_id",
            "profile",
            "used_default",
            "correlation_id",
            "duration_ms",
            "result_bytes",
            "command_source_dirty",
            "is_mutating",
            "repo_id",
        }
    )
    assert sync_details["profile"] == bg_details["profile"] == "slow"
    assert sync_details["used_default"] is bg_details["used_default"] is False
    assert isinstance(sync_details["result_bytes"], int) and sync_details["result_bytes"] > 0
    assert isinstance(bg_details["result_bytes"], int) and bg_details["result_bytes"] > 0


def test_same_workspace_background_admission_fails_with_lock_timeout_naming_the_operation(
    forge_env: ForgeEnvironment,
) -> None:
    _add_slow_profile(forge_env)
    service, runner = _manual_service(forge_env)

    first = service.workspace_create("demo", "first background run")
    first_id = first["workspace_id"]
    second = service.workspace_create("demo", "second workspace runs concurrently")
    second_id = second["workspace_id"]

    started = service.workspace_run_profile(first_id, "slow", background=True)
    running_operation_id = started["operation_id"]

    with pytest.raises(RepoForgeError) as exc:
        service.workspace_run_profile(first_id, "slow", background=True)
    assert exc.value.code is ErrorCode.LOCK_TIMEOUT
    assert exc.value.retryable is True
    assert running_operation_id in str(exc.value)

    # A different workspace proceeds concurrently while the first is still locked.
    concurrent = service.workspace_run_profile(second_id, "quick", background=True)
    assert concurrent["phase"] == "running"

    runner.run(running_operation_id)
    runner.run(concurrent["operation_id"])
    assert service.operation_status(running_operation_id)["state"] == "succeeded"
    assert service.operation_status(concurrent["operation_id"])["state"] == "succeeded"


def test_background_admission_over_cap_is_retryable_and_states_the_cap(
    forge_env: ForgeEnvironment,
) -> None:
    _add_slow_profile(forge_env)
    _set_server_field(forge_env, "max_background_profiles = 1")
    service, runner = _manual_service(forge_env)

    first = service.workspace_create("demo", "cap holder")
    first_id = first["workspace_id"]
    second = service.workspace_create("demo", "cap rejected")
    second_id = second["workspace_id"]

    started = service.workspace_run_profile(first_id, "slow", background=True)

    with pytest.raises(RepoForgeError) as exc:
        service.workspace_run_profile(second_id, "slow", background=True)
    assert exc.value.code is ErrorCode.RUNTIME_UNAVAILABLE
    assert exc.value.retryable is True
    assert "1" in str(exc.value)
    assert exc.value.details.get("max_background_profiles") == 1

    # The rejected admission touched neither the lock nor the durable operation store.
    locks = service.application.context.locks
    with locks.lock(second_id, timeout_seconds=0.1):
        pass

    runner.run(started["operation_id"])
    assert service.operation_status(started["operation_id"])["state"] == "succeeded"

    # Once the slot frees up, admission succeeds again.
    retried = service.workspace_run_profile(second_id, "slow", background=True)
    runner.run(retried["operation_id"])
    assert service.operation_status(retried["operation_id"])["state"] == "succeeded"


def test_background_global_cap_admission_is_atomic_across_workspaces(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_slow_profile(forge_env)
    _set_server_field(forge_env, "max_background_profiles = 1")
    service, runner = _manual_service(forge_env)
    first_id = service.workspace_create("demo", "atomic cap first")["workspace_id"]
    second_id = service.workspace_create("demo", "atomic cap second")["workspace_id"]

    original_list = service.operations.list_records
    snapshots_ready = threading.Barrier(2)

    def synchronized_snapshot(*, max_records: int = 2_000):
        page = original_list(max_records=max_records)
        with contextlib.suppress(threading.BrokenBarrierError):
            snapshots_ready.wait(timeout=0.3)
        return page

    monkeypatch.setattr(service.operations, "list_records", synchronized_snapshot)

    def admit(workspace_id: str) -> tuple[str, object]:
        try:
            return "accepted", service.workspace_run_profile(workspace_id, "slow", background=True)
        except RepoForgeError as exc:
            return "rejected", exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, (first_id, second_id)))

    accepted = [value for status, value in outcomes if status == "accepted"]
    rejected = [value for status, value in outcomes if status == "rejected"]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0], RepoForgeError)
    assert rejected[0].code is ErrorCode.RUNTIME_UNAVAILABLE

    operation_id = accepted[0]["operation_id"]
    runner.run(operation_id)
    assert service.operation_status(operation_id)["state"] == "succeeded"


def _start_background_profile_in_subprocess(
    config_path: str,
    workspace_id: str,
    ready: multiprocessing.synchronize.Event,
) -> None:
    from repoforge.application.service import CodingService
    from repoforge.config import load_config

    service = CodingService(load_config(Path(config_path)))
    service.workspace_run_profile(workspace_id, "slow", background=True)
    ready.set()
    time.sleep(30)  # Killed by the parent long before this returns.


def test_background_crash_recovery_orphans_with_lock_already_released(
    forge_env: ForgeEnvironment,
) -> None:
    _add_slow_profile(forge_env)
    created = forge_env.service.workspace_create("demo", "orphan recovery")
    workspace_id = created["workspace_id"]

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_start_background_profile_in_subprocess,
        args=(str(forge_env.config_path), workspace_id, ready),
    )
    process.start()
    try:
        assert ready.wait(10), "background admission did not complete in the worker process"
        # Give the worker's background thread a moment to actually start the subprocess
        # before the hard kill, so this exercises a genuinely running operation.
        time.sleep(0.3)
    finally:
        process.kill()
        process.join(5)

    # The worker process is gone: its OS-level flock on the workspace lock file is
    # released by the kernel, exactly as on a real crash. A fresh application build
    # (as happens on server restart) must run recovery and see the true state.
    config = load_config(forge_env.config_path)
    restarted = build_application(config)

    running_operations = [
        task
        for task in restarted.operations.list_records(max_records=2_000).records
        if task.kind == "workspace_run_profile" and task.workspace_id == workspace_id
    ]
    assert len(running_operations) == 1
    operation_id = running_operations[0].operation_id
    status = restarted.operations.status(operation_id)
    assert status.state.value == "orphaned"
    assert status.error_code == "OPERATION_WORKER_LOST"

    # No stale lock: the same workspace is immediately usable again.
    recovered_service = CodingService(config, application=restarted)
    with recovered_service.application.context.locks.lock(workspace_id, timeout_seconds=0.5):
        pass
    result = recovered_service.workspace_run_profile(workspace_id, "quick")
    assert result["profile"] == "quick"


# ---------------------------------------------------------------------------
# Real-subprocess cancellation (real thread runner, real fcntl lock)
# ---------------------------------------------------------------------------


def test_background_cancellation_kills_the_process_group_releases_the_lock_and_leaves_no_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    _add_slow_profile(forge_env)
    service = _reload_service(forge_env)
    created = service.workspace_create("demo", "real cancellation")
    workspace_id = created["workspace_id"]

    started = service.workspace_run_profile(workspace_id, "slow", background=True)
    operation_id = started["operation_id"]

    # Wait for the operation to actually be running before requesting cancellation.
    _poll(lambda: service.operation_status(operation_id)["state"] == "running")
    time.sleep(0.3)  # let the "python3 -c time.sleep(5)" subprocess actually start

    cancel_result = service.operation_cancel(operation_id)
    assert cancel_result["cancellation_requested"] is True

    def _terminal_status() -> dict[str, object] | None:
        status = service.operation_status(operation_id)
        return status if status["state"] in {"cancelled", "succeeded", "failed"} else None

    final_status = _poll(_terminal_status, timeout=10.0)
    assert final_status["state"] == "cancelled"
    assert final_status["error_code"] is None

    # No receipt was written for the cancelled run.
    status = service.workspace_status(workspace_id)
    assert status["last_verification"] is None

    # The lock was released: another mutation on the same workspace proceeds promptly.
    quick_started = time.monotonic()
    quick_result = service.workspace_run_profile(workspace_id, "quick")
    assert time.monotonic() - quick_started < 5.0
    assert quick_result["profile"] == "quick"

    failure_events = [
        event
        for event in _audit_events(forge_env.root, "workspace_run_profile")
        if event["details"].get("workspace_id") == workspace_id and not event["success"]
    ]
    assert len(failure_events) == 1
    details = failure_events[0]["details"]
    assert details["error_code"] == "COMMAND_FAILED"
    assert details.get("cancelled") is True
    assert details["exit_code"] is not None and details["exit_code"] != 0
