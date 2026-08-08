from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repoforge.adapters.filesystem.local import LocalFileSystem
from repoforge.adapters.persistence import JsonExecutionWorkerBindingStore
from repoforge.adapters.runtime.execution_worker import SubprocessExecutionWorker
from repoforge.adapters.runtime.launcher import SubprocessRuntimeLauncher
from repoforge.adapters.runtime.profile_store import JsonTunnelProfileStore
from repoforge.adapters.runtime.state_store import JsonRuntimeStore
from repoforge.adapters.runtime.tunnel_cli import TunnelCliClient
from repoforge.domain.errors import ConfigError, ExecutionWorkerRegistrationError
from repoforge.domain.runtime import (
    ControlCommand,
    ControlRequest,
    RuntimePhase,
    RuntimeRecord,
    TunnelProfile,
)
from repoforge.domain.runtime_events import RuntimeEventV1
from repoforge.testing import InMemoryLockManager, InMemoryOperationGate
from repoforge.testing.fakes import InMemoryWorkerRegistrar

cli = importlib.import_module("repoforge.interfaces.cli.main")


def _profile(executable: str = "tunnel-client") -> TunnelProfile:
    return TunnelProfile("a" * 64, "repoforge", executable, "1.2.3", ("rf", "serve"))


def _record(*, pid: int | None = None, child_pid: int | None = None) -> RuntimeRecord:
    return RuntimeRecord(
        1,
        RuntimePhase.STARTING,
        pid,
        "b" * 64 if pid is not None else None,
        None,
        2,
        "repoforge",
        "c" * 64,
        "d" * 64,
        "now" if pid is not None else None,
        "now",
        "corr",
        child_pid=child_pid,
        child_process_identity="e" * 64 if child_pid is not None else None,
    )


def test_tunnel_profile_store_round_trip_and_rejects_corruption(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    store = JsonTunnelProfileStore(path, LocalFileSystem())
    assert store.fingerprint() is None

    profile = _profile()
    store.commit(profile)
    assert store.fingerprint() == profile.fingerprint
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "tunnel_id" not in payload
    assert payload["mcp_argv_sha256"] == hashlib.sha256(b"rf\0serve").hexdigest()

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be an object"):
        store.fingerprint()
    path.write_text('{"fingerprint":"short"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid"):
        store.fingerprint()
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid tunnel profile"):
        store.fingerprint()


def test_runtime_store_round_trip_degrades_child_and_clears_stale_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.state_store")
    path = tmp_path / "runtime.json"
    store = JsonRuntimeStore(path)
    assert store.read() is None

    identities = {10: "b" * 64, 11: "e" * 64}
    monkeypatch.setattr(module, "process_identity", lambda pid: identities.get(pid))
    record = _record(pid=10, child_pid=11)
    store.write(record)
    assert store.read() == record

    identities.pop(11)
    child_mismatch_bytes = path.read_bytes()
    degraded = store.read()
    assert degraded is not None
    assert degraded.phase is RuntimePhase.DEGRADED
    assert degraded.child_pid is None
    assert degraded.last_error_code == "CHILD_IDENTITY_MISMATCH"
    assert path.read_bytes() == child_mismatch_bytes

    assert store.reconcile() == degraded
    assert store.read() == degraded

    identities.pop(10)
    parent_mismatch_bytes = path.read_bytes()
    assert store.read() is None
    assert path.read_bytes() == parent_mismatch_bytes
    assert store.reconcile() is None
    assert not path.exists()

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be an object"):
        store.read()
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid runtime state fields"):
        store.read()
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid runtime state"):
        store.read()


def test_runtime_reconcile_preserves_replacement_written_after_stale_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.state_store")
    identities = {10: "b" * 64}
    monkeypatch.setattr(module, "process_identity", lambda pid: identities.get(pid))
    store = JsonRuntimeStore(tmp_path / "runtime.json")
    store.write(_record(pid=10))

    identities.pop(10)
    assert store.read() is None

    replacement = _record(pid=20)
    identities[20] = "b" * 64
    store.write(replacement)

    assert store.reconcile() == replacement
    assert store.read() == replacement


def test_runtime_store_clear_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.state_store")
    monkeypatch.setattr(module, "process_identity", lambda pid: "b" * 64 if pid == 10 else None)
    store = JsonRuntimeStore(tmp_path / "runtime.json")
    store.write(_record(pid=10))
    store.clear(expected_pid=20)
    assert store.read() is not None
    store.clear(expected_pid=10)
    assert store.read() is None


def test_record_restart_never_loses_an_update_across_overlapping_writers(
    tmp_path: Path,
) -> None:
    """The exact race a bare read()/write() pair has under overlapping supervisor
    incarnations (#448 Slice 4): two writers reading the same baseline and each
    writing back the same incremented total, silently losing one restart's worth of
    evidence. `record_restart()` holds an OS-level exclusive lock across the whole
    read-modify-write, so this must not be possible even with real thread
    concurrency (not just sequential calls that never actually race).
    """
    import threading

    from repoforge.adapters.runtime.state_store import JsonRestartHistoryStore

    store = JsonRestartHistoryStore(tmp_path / "restart-history.json")
    threads_count = 8
    barrier = threading.Barrier(threads_count)

    def one_restart(index: int) -> None:
        barrier.wait(timeout=5.0)  # maximize the chance every thread reads together
        store.record_restart(
            incarnation_id="a" * 24,
            reason="concurrent restart test",
            occurred_at="2026-08-05T00:00:00+00:00",
            event_id=f"event-{index}",
        )

    threads = [threading.Thread(target=one_restart, args=(i,)) for i in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    final = store.read()
    assert final is not None
    assert final.restarts_total == threads_count, (
        f"expected exactly {threads_count} restarts recorded, got {final.restarts_total} -- "
        "an overlapping writer lost an update"
    )


def test_record_restart_is_idempotent_on_replayed_event_id(tmp_path: Path) -> None:
    """A caller that replays the same logical restart event (e.g. retrying after a
    write it could not confirm landed) must not double-count it (#448 Slice 4)."""
    from repoforge.adapters.runtime.state_store import JsonRestartHistoryStore

    store = JsonRestartHistoryStore(tmp_path / "restart-history.json")
    first = store.record_restart(
        incarnation_id="a" * 24,
        reason="tunnel failed to start",
        occurred_at="2026-08-05T00:00:00+00:00",
        event_id="event-1",
    )
    assert first.restarts_total == 1

    replayed = store.record_restart(
        incarnation_id="a" * 24,
        reason="tunnel failed to start",
        occurred_at="2026-08-05T00:00:05+00:00",  # even with a different timestamp
        event_id="event-1",  # same event_id: this is a replay, not a new restart
    )
    assert replayed.restarts_total == 1, "a replayed event_id must not increment again"
    assert replayed == first

    second = store.record_restart(
        incarnation_id="a" * 24,
        reason="tunnel failed to start again",
        occurred_at="2026-08-05T00:00:10+00:00",
        event_id="event-2",  # a genuinely new restart
    )
    assert second.restarts_total == 2


def test_every_logical_restart_increments_exactly_once_across_repeated_calls(
    tmp_path: Path,
) -> None:
    """Simulates one incarnation's restart loop calling `record_restart()` many times
    (matching all three of `supervisor.py`'s call sites sharing this same operation):
    N distinct events must produce a ledger reading exactly N, never more or less."""
    from repoforge.adapters.runtime.state_store import JsonRestartHistoryStore

    store = JsonRestartHistoryStore(tmp_path / "restart-history.json")
    for i in range(5):
        recorded = store.record_restart(
            incarnation_id="b" * 24,
            reason=f"restart {i}",
            occurred_at=f"2026-08-05T00:00:{i:02d}+00:00",
            event_id=f"b{'' * 24}-restart-{i}",
        )
        assert recorded.restarts_total == i + 1

    final = store.read()
    assert final is not None
    assert final.restarts_total == 5


def test_seed_if_missing_initializes_from_legacy_evidence_with_distinct_provenance(
    tmp_path: Path,
) -> None:
    """Migration (#448 Slice 4): a ledger-unaware release's `RuntimeRecord` can carry
    real `restarts_total` history with no ledger to match it. Seeding from that must
    be marked `provenance="legacy_runtime_record"`, never silently as `"durable"`."""
    from repoforge.adapters.runtime.state_store import JsonRestartHistoryStore

    store = JsonRestartHistoryStore(tmp_path / "restart-history.json")
    assert store.read() is None

    seeded = store.seed_if_missing(
        restarts_total=6,
        last_restart_at="2026-08-04T00:00:00+00:00",
        incarnation_id="c" * 24,
        occurred_at="2026-08-05T00:00:00+00:00",
    )

    assert seeded.restarts_total == 6
    assert seeded.provenance == "legacy_runtime_record"
    assert store.read() == seeded


def test_seed_if_missing_never_overwrites_an_existing_ledger(tmp_path: Path) -> None:
    """A ledger that already exists always wins over a legacy `RuntimeRecord`
    snapshot -- seeding must be a genuine no-op once real ledger history exists,
    never a silent downgrade back to stale evidence (#448 Slice 4)."""
    from repoforge.adapters.runtime.state_store import JsonRestartHistoryStore

    store = JsonRestartHistoryStore(tmp_path / "restart-history.json")
    recorded = store.record_restart(
        incarnation_id="d" * 24,
        reason="a genuine restart already tracked by the ledger",
        occurred_at="2026-08-05T00:00:00+00:00",
        event_id="event-1",
    )
    assert recorded.restarts_total == 1

    # A stale legacy snapshot claiming 99 restarts must not clobber the real ledger.
    seeded = store.seed_if_missing(
        restarts_total=99,
        last_restart_at="2000-01-01T00:00:00+00:00",
        incarnation_id="d" * 24,
        occurred_at="2026-08-05T00:00:05+00:00",
    )

    assert seeded == recorded
    assert seeded.restarts_total == 1
    assert store.read() == recorded


def test_a_corrupt_restart_history_ledger_fails_closed_rather_than_resetting(
    tmp_path: Path,
) -> None:
    """A corrupt ledger file is NOT equivalent to a missing one (#448 review): every
    entry point must fail typed and closed rather than silently treating unreadable
    content as "no ledger yet" -- which would either reset restart evidence to zero
    (`record_restart`) or let a stale legacy snapshot overwrite real history that
    simply couldn't be parsed this time (`seed_if_missing`).
    """
    from repoforge.adapters.runtime.state_store import JsonRestartHistoryStore

    path = tmp_path / "restart-history.json"
    corrupt_content = "{not valid json"
    path.write_text(corrupt_content, encoding="utf-8")

    store = JsonRestartHistoryStore(path)

    with pytest.raises(ConfigError) as read_error:
        store.read()
    # The path is useful for an operator to investigate; the raw corrupt content is
    # not -- it must never be echoed back into an error surface.
    assert str(path) in str(read_error.value)
    assert corrupt_content not in str(read_error.value)

    # seed_if_missing must not treat "unreadable" as "absent" and silently overwrite
    # the corrupt file with a legacy snapshot -- that would discard whatever real
    # history the corrupt file might contain, unrecoverably.
    with pytest.raises(ConfigError):
        store.seed_if_missing(
            restarts_total=99,
            last_restart_at="2000-01-01T00:00:00+00:00",
            incarnation_id="d" * 24,
            occurred_at="2026-08-05T00:00:00+00:00",
        )

    # record_restart must not treat "unreadable" as "no history yet" and start a
    # fresh count from zero -- that would silently understate real restart history.
    with pytest.raises(ConfigError):
        store.record_restart(
            incarnation_id="d" * 24,
            reason="a restart attempted while the ledger is corrupt",
            occurred_at="2026-08-05T00:00:05+00:00",
            event_id="event-1",
        )

    # The corrupt file itself is left exactly as it was, for investigation -- none of
    # the three calls above reset it, "repaired" it, or deleted it.
    assert path.read_text(encoding="utf-8") == corrupt_content


def _write_fake_tunnel(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import os
import sys
import time
args = sys.argv[1:]
if args == ['--version']:
    print('tunnel-client 9.9')
    raise SystemExit(0)
if args and args[0] == 'init':
    print('initialized')
    raise SystemExit(0)
if args and args[0] == 'doctor':
    if os.environ.get('FAIL_DOCTOR'):
        print('token=' + os.environ.get('CONTROL_PLANE_API_KEY', ''), file=sys.stderr)
        raise SystemExit(7)
    print('healthy')
    raise SystemExit(0)
if args and args[0] == 'run':
    print('running', flush=True)
    time.sleep(60)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_tunnel_cli_runtime_jsonl_lifecycle(tmp_path: Path) -> None:
    executable = tmp_path / "tunnel-client"
    _write_fake_tunnel(executable)
    client = TunnelCliClient(str(executable), default_timeout_seconds=5)
    profile = _profile(str(executable))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "REPOFORGE_TUNNEL_ID": "tunnel-secret-id",
        "CONTROL_PLANE_API_KEY": "super-secret",
    }

    assert "9.9" in client.executable_version()
    client.initialize(profile, env=env)
    ok, detail = client.doctor(profile, env=env)
    assert ok and "healthy" in detail
    failed, detail = client.doctor(profile, env={**env, "FAIL_DOCTOR": "1"})
    assert failed is False
    assert "super-secret" not in detail and "<redacted>" in detail

    log = tmp_path / "runtime.log"
    log.write_bytes(b"x" * 5_000_001)
    child = client.start(profile, env=env, log_path=log, correlation_id="runtime-corr")
    assert log.with_suffix(".log.1").is_file()
    assert client.is_alive(child)
    import time

    time.sleep(0.1)
    client.terminate(child, grace_seconds=0.1)
    for _ in range(100):
        if not client.is_alive(child):
            break
        time.sleep(0.01)
    assert not client.is_alive(child)
    lines = log.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    assert event["schema_version"] == 1
    assert event["event_kind"] == "process_output"
    assert event["stream"] == "combined"
    assert event["message"] == "running"
    assert event["correlation_id"] == "runtime-corr"

    with pytest.raises(ConfigError, match="Tunnel id"):
        client.initialize(profile, env={})


def test_tunnel_cli_reports_version_and_execution_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.tunnel_cli")
    client = TunnelCliClient("missing")

    def boom(*args: object, **kwargs: object) -> Any:
        raise OSError("no executable")

    monkeypatch.setattr(module.subprocess, "run", boom)
    with pytest.raises(ConfigError, match="Cannot inspect"):
        client.executable_version()
    with pytest.raises(ConfigError, match="failed to execute"):
        client._run(["missing"], env={}, timeout=1)


def test_runtime_launcher_foreground_background_and_identity_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.launcher")
    launcher = SubprocessRuntimeLauncher()
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "key")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, env, check: calls.append((argv, env)) or SimpleNamespace(returncode=4),
    )
    assert launcher.start(tmp_path / "config.toml", foreground=True, extra_env={"X": "1"}) == 4
    assert calls[0][1]["X"] == "1" and calls[0][1]["CONTROL_PLANE_API_KEY"] == "key"

    class FakePopen:
        pid = 321

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: FakePopen())
    assert launcher.start(tmp_path / "config.toml", foreground=False, extra_env={}) == 321

    record = _record(pid=10)
    monkeypatch.setattr(module, "process_identity", lambda pid: None)
    assert launcher.force_stop(record, grace_seconds=0) is False

    values = iter(["b" * 64, None])
    monkeypatch.setattr(module, "process_identity", lambda pid: next(values, None))
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: None)
    assert launcher.force_stop(record, grace_seconds=0.01) is True


def test_execution_worker_adapter_launches_exact_generation_in_own_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.execution_worker")
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakePopen:
        pid = 456

        def poll(self):
            return None

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakePopen()

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        module,
        "process_identity",
        lambda pid: "d" * 64 if pid in {456, os.getpid()} else None,
    )
    monkeypatch.setattr(module, "read_identity", lambda pid: None)
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    worker = SubprocessExecutionWorker(
        tmp_path / "config.toml", bindings=bindings, registrar=InMemoryWorkerRegistrar()
    )

    child = worker.start(
        12,
        env={"PATH": "/usr/bin"},
        log_path=tmp_path / "execution-worker.log",
        correlation_id="corr-1",
    )

    assert child.pid == 456
    assert calls[0][0][-4:] == ["--config", str(tmp_path / "config.toml"), "--generation", "12"]
    assert calls[0][1]["start_new_session"] is True
    # The caller env is preserved and the pre-spawn lease id is handed to the child.
    assert calls[0][1]["env"]["PATH"] == "/usr/bin"
    assert calls[0][1]["env"]["REPOFORGE_EXECUTION_WORKER_LEASE_ID"] == "worker-" + "0" * 24
    assert worker.is_alive(child) is True
    # The durable lease is mandatory and was written before start() returned (#424).
    assert any(item.pid == 456 for item in bindings.list_all())


def test_execution_worker_adapter_records_a_stable_identity_across_the_exec_flap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A freshly spawned worker's identity can flap once on macOS (shim re-exec); the
    recorded identity must be the settled one so later `is_alive` samples match."""
    module = importlib.import_module("repoforge.adapters.runtime.execution_worker")
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakePopen:
        pid = 789

        def poll(self):
            return None

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakePopen()

    # First sample lands in the fork->exec window (transient), then the identity
    # settles: exactly the macOS shim race that broke CI on the 26.5 image.
    identities = iter(["a" * 64, "b" * 64, "b" * 64, "b" * 64])
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        module,
        "process_identity",
        lambda pid: next(identities) if pid == 789 else "d" * 64,
    )
    monkeypatch.setattr(module, "read_identity", lambda pid: None)
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    worker = SubprocessExecutionWorker(
        tmp_path / "config.toml", bindings=bindings, registrar=InMemoryWorkerRegistrar()
    )

    child = worker.start(
        3,
        env={"PATH": "/usr/bin"},
        log_path=tmp_path / "execution-worker.log",
        correlation_id="corr-flap",
    )

    assert child.process_identity == "b" * 64
    assert worker.is_alive(child) is True


def test_execution_worker_adapter_terminates_a_worker_it_cannot_register(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker whose durable lease cannot be written is terminated, never left running."""
    import signal

    module = importlib.import_module("repoforge.adapters.runtime.execution_worker")
    calls: list[tuple[list[str], dict[str, object]]] = []
    killed: list[tuple[int, int]] = []
    identity_calls = {"child": 0}

    class FakePopen:
        pid = 2468

        def poll(self):
            return None

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakePopen()

    def fake_identity(pid: int) -> str | None:
        if pid == 2468:
            identity_calls["child"] += 1
            # Settle needs two identical samples; the reap loop then sees it gone.
            return "d" * 64 if identity_calls["child"] <= 2 else None
        return "s" * 64

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "process_identity", fake_identity)
    monkeypatch.setattr(module, "read_identity", lambda pid: None)
    monkeypatch.setattr(module.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    class FailingBindings:
        def put(self, binding):
            raise OSError("disk full")

        def get(self, worker_id):
            del worker_id
            return None

        def update_state(self, worker_id, state):
            del worker_id, state
            return None

        def list_page(self, *, max_records=2_000):
            del max_records
            raise NotImplementedError

        def collect_terminal(self, *, max_records=5_000):
            del max_records
            return 0

    from repoforge.ports.process_reaper import ReapOutcome

    class FakeReaper:
        """Signal the unregistered worker group; never probes the real process table.

        The real OsProcessReaper probes identity via `ps`; the test's Popen double
        must not be allowed to leak into that probe, so the reap is faked here and
        the assertions cover the durable-signal contract instead.
        """

        def reap(self, target):
            killed.append((target.child_pgid, signal.SIGTERM))
            return ReapOutcome(
                attempted=True,
                reaped=True,
                still_alive=False,
                detail="faked reaper signalled the unregistered worker group",
            )

    worker = SubprocessExecutionWorker(
        tmp_path / "config.toml",
        bindings=FailingBindings(),
        registrar=InMemoryWorkerRegistrar(),
        reaper=FakeReaper(),
    )

    with pytest.raises(
        ExecutionWorkerRegistrationError, match="EXECUTION_WORKER_REGISTRATION_FAILED"
    ):
        worker.start(
            1,
            env={"PATH": "/usr/bin"},
            log_path=tmp_path / "execution-worker.log",
            correlation_id="corr-fail",
        )

    assert killed, "the unregistered worker's process group was never signalled"
    assert any(sig == signal.SIGTERM for _, sig in killed) or any(
        sig == signal.SIGKILL for _, sig in killed
    )


def test_execution_worker_cli_binds_exact_config_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("repoforge.interfaces.runtime.execution_worker")
    captured: dict[str, object] = {}

    def run(config_path, *, generation):
        captured.update(config_path=config_path, generation=generation)
        return 7

    monkeypatch.setattr(module, "run_execution_worker", run)

    assert module.main(["--config", str(tmp_path / "config.toml"), "--generation", "12"]) == 7
    assert captured == {
        "config_path": tmp_path / "config.toml",
        "generation": 12,
    }


def test_serve_control_handler_covers_health_drain_resume_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_module = importlib.import_module("repoforge.interfaces.mcp.server")
    gate = InMemoryOperationGate()
    active = SimpleNamespace(generation=7)

    class Store:
        root = tmp_path

        def activation_target(self) -> object:
            return active

        def active(self) -> object:
            return active

        def resolved_path(self, generation: int) -> Path:
            assert generation == 7
            return tmp_path / "resolved.toml"

    store = Store()
    captured: dict[str, Any] = {}

    class Control:
        def start(self, handler: Any) -> None:
            captured["handler"] = handler

        def close(self) -> None:
            captured["closed"] = True

    class Service:
        def repo_list(self, *, synthetic: bool = False) -> dict[str, Any]:
            return {"repositories": [{"repo_id": "demo"}]}

    class MCP:
        def run(self, *, transport: str) -> None:
            assert transport == "stdio"
            handler = captured["handler"]
            assert handler(ControlRequest(1, ControlCommand.PING, "p")).ok
            assert handler(ControlRequest(1, ControlCommand.STATUS, "s")).ok
            assert handler(ControlRequest(1, ControlCommand.HEALTH, "h")).status == "healthy"
            invalid = handler(
                ControlRequest(1, ControlCommand.DRAIN, "d", (("timeout_seconds", 999),))
            )
            assert invalid.error_code == "INVALID_DRAIN_TIMEOUT"
            drained = handler(
                ControlRequest(1, ControlCommand.DRAIN, "d2", (("timeout_seconds", 0),))
            )
            assert drained.status == "drained"
            assert handler(ControlRequest(1, ControlCommand.RESUME, "r")).status == "open"
            assert (
                handler(
                    ControlRequest(1, ControlCommand.FAIL_CLOSED, "f", (("reason", "test"),))
                ).status
                == "fail_closed"
            )
            assert (
                handler(ControlRequest(1, ControlCommand.SHUTDOWN, "u")).error_code
                == "UNSUPPORTED_CONTROL_COMMAND"
            )

    monkeypatch.setattr(cli, "_ensure_generation", lambda path: store)
    monkeypatch.setattr(cli, "load_config", lambda path: object())
    monkeypatch.setattr(cli, "build_operation_gate", lambda: gate)
    # `**kwargs` on purpose: this stub stands in for a composition whose keyword
    # arguments are load-bearing elsewhere (config_generation), and a stub that
    # pins today's exact signature fails the moment one is added -- which is how a
    # correct fix to that composition first showed up as a red test here.
    monkeypatch.setattr(cli, "build_application", lambda config, **kwargs: object())
    monkeypatch.setattr(cli, "CodingService", lambda config, application: Service())
    monkeypatch.setattr(cli, "build_runtime_control_server", lambda path: Control())
    monkeypatch.setattr(
        cli,
        "write_runtime_state",
        lambda path, generation, surface: SimpleNamespace(pid=55),
    )
    monkeypatch.setattr(cli, "clear_runtime_state", lambda path, pid: captured.update(cleared=pid))
    monkeypatch.setattr(mcp_module, "tool_surface_hash", lambda: "a" * 64)

    def create_server(
        *,
        router: object,
        admin: object | None = None,
        contract_identity_provider: Any,
    ) -> MCP:
        del router, admin
        identity = contract_identity_provider()
        assert identity.active_generation == 7
        captured["contract_identity"] = identity.as_dict()
        return MCP()

    monkeypatch.setattr(mcp_module, "create_server", create_server)

    assert cli._serve(tmp_path / "config.toml") == 0
    assert captured["closed"] is True and captured["cleared"] == 55
    assert captured["contract_identity"]["active_generation"] == 7


def test_serve_health_failure_and_missing_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyStore:
        def activation_target(self) -> None:
            return None

        def active(self) -> None:
            return None

        def current(self) -> None:
            return None

    monkeypatch.setattr(cli, "_ensure_generation", lambda path: EmptyStore())
    with pytest.raises(ConfigError, match="No accepted configuration generation"):
        cli._serve(tmp_path / "config.toml")


def test_tunnel_health_uses_advertised_admin_endpoint_and_response_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("repoforge.adapters.runtime.tunnel_cli")
    client = TunnelCliClient("tunnel-client")
    child = module.ChildProcess(321, "a" * 64, "now")
    monkeypatch.setattr(client, "is_alive", lambda value: value == child)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self, limit: int) -> bytes:
            assert limit <= 65_537
            return b'{"status":"ok"}'

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    client._observe_log_line(
        child.pid,
        '{"level":"INFO","msg":"WEB UI","health_url":"http://127.0.0.1:8080"}',
    )
    healthy = client.health(child, timeout_seconds=0.1)
    assert all(check.ok for check in healthy)
    assert {check.name for check in healthy} == {
        "tunnel_child",
        "tunnel_admin",
        "control_plane_response",
    }

    client._observe_log_line(
        child.pid,
        '{"level":"ERROR","msg":"failed to post error response to control plane"}',
    )
    client._observe_log_line(
        child.pid,
        '{"level":"ERROR","msg":"failed to process polled command","status":502}',
    )
    degraded = client.health(child, timeout_seconds=0.1)
    response_check = next(check for check in degraded if check.name == "control_plane_response")
    assert response_check.ok is False
    assert "502" in response_check.detail

    client._observe_log_line(
        child.pid,
        '{"level":"INFO","msg":"dispatcher acknowledged notification with control plane"}',
    )
    recovered = client.health(child, timeout_seconds=0.1)
    assert next(check for check in recovered if check.name == "control_plane_response").ok


def test_tunnel_writer_persists_secret_safe_runtime_jsonl(tmp_path: Path) -> None:
    client = TunnelCliClient("tunnel-client")
    log_path = tmp_path / "managed-runtime.log"

    client._append_runtime_event(
        log_path,
        RuntimeEventV1(
            observed_at="2026-07-21T12:00:00+00:00",
            component="tunnel_client",
            stream="stdout",
            level="INFO",
            event_kind="process_output",
            message="token=secret-value",
        ),
        secrets=("secret-value",),
    )

    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert payload["schema_version"] == 1
    assert payload["component"] == "tunnel_client"
    assert payload["event_kind"] == "process_output"
    assert "secret-value" not in json.dumps(payload)
    assert payload["message"].startswith("token=<redacted")


def test_tunnel_projects_child_json_fields() -> None:
    client = TunnelCliClient("tunnel-client")

    event = client._runtime_event_from_line(
        json.dumps(
            {
                "level": "ERROR",
                "msg": "failed safely",
                "action": "workspace_push",
                "duration_ms": 12.5,
            }
        ),
        correlation_id="runtime-corr",
    )

    assert event.component == "tunnel_client"
    assert event.stream == "combined"
    assert event.level == "ERROR"
    assert event.event_kind == "tunnel_event"
    assert event.message == "failed safely"
    assert event.action == "workspace_push"
    assert event.duration_ms == 12.5
    assert event.correlation_id == "runtime-corr"
