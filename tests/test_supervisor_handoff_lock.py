"""The supervisor handoff must not race itself for `runtime-single-instance` (#304).

An activation replaces the supervisor process, so the incoming one starts while the
outgoing one is still shutting down and still holds the single-instance lock. Taking that
lock with no wait made every handoff a race: on a real activation of `5350da9` the
incoming supervisor died immediately on `LOCK_TIMEOUT`, no live record was published, and
an otherwise fine activation was declared a failure.

These tests use the REAL `FcntlLockManager` and a real competing holder, so the race is
reproduced deterministically -- an outgoing holder plus an incoming start -- rather than
by timing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from repoforge.adapters.locking.fcntl import FcntlLockManager
from repoforge.application.runtime.supervisor import RuntimeSupervisor
from repoforge.domain.config_generation import CapabilityDeltaKind, ConfigGeneration
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import ChildProcess, ControlResponse, RuntimeRecord, TunnelProfile
from repoforge.testing import FixedClock, SequenceIdGenerator


class _NullRestartHistory:
    """In-memory fake: durable restart-history ledger (#448 Slice 4).

    Most tests here don't exercise restart-history semantics directly, so this
    just reflects whatever was last written -- `None` until then, like a fresh
    install with no ledger yet.
    """

    def __init__(self) -> None:
        self.record: object | None = None

    def read(self) -> object | None:
        return self.record

    def write(self, record: object) -> None:
        self.record = record

    def record_restart(
        self, *, incarnation_id: str, reason: str | None, occurred_at: str, event_id: str
    ) -> object:
        from repoforge.domain.runtime import RestartHistoryRecord

        current = self.record
        if current is not None and getattr(current, "last_event_id", None) == event_id:
            return current
        self.record = RestartHistoryRecord(
            protocol_version=1,
            restarts_total=(getattr(current, "restarts_total", 0) if current is not None else 0)
            + 1,
            last_restart_at=occurred_at,
            incarnation_id=incarnation_id,
            updated_at=occurred_at,
            last_event_id=event_id,
            last_restart_reason=reason,
            provenance="durable",
        )
        return self.record

    def seed_if_missing(
        self,
        *,
        restarts_total: int,
        last_restart_at: str | None,
        incarnation_id: str,
        occurred_at: str,
    ) -> object:
        from repoforge.domain.runtime import RestartHistoryRecord

        if self.record is not None:
            return self.record
        self.record = RestartHistoryRecord(
            protocol_version=1,
            restarts_total=max(0, restarts_total),
            last_restart_at=last_restart_at,
            incarnation_id=incarnation_id,
            updated_at=occurred_at,
            provenance="legacy_runtime_record",
        )
        return self.record


_GENERATION = 2


def _generation(number: int) -> ConfigGeneration:
    return ConfigGeneration(
        number,
        "a" * 64,
        "b" * 64,
        (),
        "now",
        "test",
        None,
        None,
        CapabilityDeltaKind.EQUIVALENT,
        number - 1 or None,
        active=False,
    )


class _Configs:
    def __init__(self) -> None:
        self.active_item = replace(_generation(_GENERATION), active=True)

    @property
    def source_path(self) -> Path:
        return Path("/config")

    def active(self) -> ConfigGeneration | None:
        return self.active_item

    def activate(self, generation: int, *, expected_active: int | None = None) -> ConfigGeneration:
        self.active_item = replace(_generation(generation), active=True)
        return self.active_item

    def clear_activation_target(self, *, expected_generation: int | None = None) -> None:
        return None


class _Runtime:
    def __init__(self) -> None:
        self.record: RuntimeRecord | None = None
        self.phases: list[str] = []

    def read(self) -> RuntimeRecord | None:
        return self.record

    def write(self, record: RuntimeRecord) -> None:
        self.record = record
        self.phases.append(record.phase.value)

    def clear(self, *, expected_pid: int | None = None) -> None:
        self.record = None

    def peek_restart_evidence(self) -> tuple[int, str | None] | None:
        return None


class _Server:
    def start(self, handler) -> None:
        self.handler = handler

    def close(self) -> None:
        return None

    def is_serving(self) -> bool:
        return True

    def is_healthy(self) -> bool:
        return True

    def serving_diagnostic(self) -> str:
        return "fake control server"


class _Mcp:
    """Answers HEALTH, then asks the supervisor to stop so `run` returns."""

    def __init__(self) -> None:
        self.on_health = lambda: None

    def request(self, request, *, timeout_seconds: float = 10.0) -> ControlResponse:
        self.on_health()
        return ControlResponse(1, True, request.correlation_id, "healthy")


class _Processes:
    def identity(self, pid: int) -> str | None:
        return "f" * 64 if pid > 0 else None


class _Tunnel:
    def __init__(self) -> None:
        self.alive = True

    def executable_version(self) -> str:
        return "1.0"

    def initialize(self, profile, *, env) -> None:
        return None

    def doctor(self, profile, *, env) -> tuple[bool, str]:
        return (True, "ok")

    def start(self, profile, *, env, log_path, correlation_id) -> ChildProcess:
        return ChildProcess(222, "f" * 64, "now")

    def terminate(self, child, *, grace_seconds) -> None:
        self.alive = False

    def is_alive(self, child) -> bool:
        return self.alive


class _ProfileStore:
    def __init__(self) -> None:
        self.value: str | None = None

    def fingerprint(self) -> str | None:
        return self.value

    def commit(self, profile) -> None:
        self.value = profile.fingerprint


def _supervisor(
    tmp_path: Path, locks: FcntlLockManager, *, wait_seconds: float
) -> tuple[RuntimeSupervisor, _Runtime, _Mcp]:
    runtime = _Runtime()
    mcp = _Mcp()
    # The MCP worker's own record, which the health probe cross-checks against the
    # generation the supervisor is starting.
    (tmp_path / "runtime.json").write_text(
        json.dumps({"pid": 999, "process_identity": "f" * 64, "active_generation": _GENERATION}),
        encoding="utf-8",
    )
    supervisor = RuntimeSupervisor(
        store=runtime,
        restart_history=_NullRestartHistory(),
        configs=_Configs(),
        locks=locks,
        control=_Server(),
        mcp_control=mcp,
        tunnel=_Tunnel(),
        profile_store=_ProfileStore(),
        clock=FixedClock("2026-07-28T00:00:00+00:00"),
        ids=SequenceIdGenerator(("supervisor", "health")),
        processes=_Processes(),
        mcp_runtime_path=tmp_path / "runtime.json",
        log_path=tmp_path / "runtime.log",
        health_timeout_seconds=1.0,
        max_restarts=0,
        single_instance_wait_seconds=wait_seconds,
    )
    mcp.on_health = supervisor._stop.set
    return supervisor, runtime, mcp


def _run(supervisor: RuntimeSupervisor) -> int:
    return supervisor.run(
        generation=_GENERATION,
        profile=TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve")),
        tool_surface_hash="b" * 64,
        environment={},
    )


def test_the_incoming_supervisor_waits_for_the_outgoing_one_to_release_the_lock(
    tmp_path: Path,
) -> None:
    """The exact handoff #304 failed on: the outgoing holder is still alive at start."""
    locks = FcntlLockManager(tmp_path / "locks")
    supervisor, runtime, _ = _supervisor(tmp_path, locks, wait_seconds=30.0)
    held = threading.Event()
    hold_seconds = 0.5

    def outgoing() -> None:
        # A separate file handle, so this is real cross-holder contention rather than a
        # re-entrant acquisition. `run` installs signal handlers and so must stay on the
        # main thread; the OUTGOING supervisor is the one modelled by this thread.
        with locks.lock(
            "runtime-single-instance", timeout_seconds=5.0, metadata={"role": "outgoing"}
        ):
            held.set()
            time.sleep(hold_seconds)

    worker = threading.Thread(target=outgoing, daemon=True)
    worker.start()
    assert held.wait(timeout=5.0), "precondition: the outgoing holder took the lock"
    assert runtime.record is None

    started = time.monotonic()
    assert _run(supervisor) == 0
    waited = time.monotonic() - started

    # It waited for the handoff instead of dying on the lock, and only then published a
    # live record -- so exactly one supervisor was ever the writer of runtime state.
    assert waited >= hold_seconds * 0.8, f"did not wait for the outgoing holder ({waited:.3f}s)"
    assert "healthy" in runtime.phases
    worker.join(timeout=5.0)


def test_a_lock_that_is_never_released_is_a_typed_handoff_timeout(tmp_path: Path) -> None:
    """Waiting is bounded: an incumbent that is not leaving is reported, not waited on
    forever, and never joined by a second live supervisor."""
    locks = FcntlLockManager(tmp_path / "locks")
    supervisor, runtime, _ = _supervisor(tmp_path, locks, wait_seconds=0.2)

    with locks.lock("runtime-single-instance", timeout_seconds=5.0, metadata={"role": "outgoing"}):
        with pytest.raises(ConfigError, match="RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT") as raised:
            _run(supervisor)
        # The message names the holder from the lock file, so an operator can act on it.
        recorded = json.loads(locks.path_for("runtime-single-instance").read_text(encoding="utf-8"))
        assert f"holder pid {recorded['pid']}" in str(raised.value)

    # The refused supervisor must not have touched runtime state owned by the incumbent.
    assert runtime.record is None


def test_a_still_alive_holder_is_diagnosed_as_a_handoff_not_a_stuck_lock(
    tmp_path: Path,
) -> None:
    """A fast respawn racing a still-shutting-down prior instance (#448 Slice 5).

    The holder in this scenario is genuinely alive throughout -- exactly the shape of a
    real handoff, not a stuck/stale lock -- so the diagnostic must say so explicitly
    rather than the generic "holder unknown" a dead or unreadable lock file would get.
    The same failure must also be appended to the runtime log as a structured event: the
    refused incarnation's own `RuntimeRecord` write is correctly skipped (see the test
    above), so the log is the only place this evidence can appear for `rf runtime
    status`/an operator to find, other than a raw stderr traceback.
    """
    locks = FcntlLockManager(tmp_path / "locks")
    supervisor, runtime, _ = _supervisor(tmp_path, locks, wait_seconds=0.2)
    log_path = tmp_path / "runtime.log"

    with locks.lock("runtime-single-instance", timeout_seconds=5.0, metadata={"role": "outgoing"}):
        with pytest.raises(ConfigError, match="RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT") as raised:
            _run(supervisor)
        assert "is still alive" in str(raised.value)
        assert "shutting down" in str(raised.value)

    assert runtime.record is None

    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "the handoff timeout left no evidence in the runtime log"
    events = [json.loads(line) for line in lines]
    handoff_events = [event for event in events if event.get("event_kind") == "handoff_timeout"]
    assert len(handoff_events) == 1, events
    event = handoff_events[0]
    assert event["component"] == "supervisor"
    assert event["level"] == "ERROR"
    assert "RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT" in event["message"]
    assert "is still alive" in event["message"]


def test_an_uncontended_start_still_takes_the_lock_and_serves(tmp_path: Path) -> None:
    """The wait must not have turned the lock into an advisory no-op."""
    locks = FcntlLockManager(tmp_path / "locks")
    supervisor, runtime, _ = _supervisor(tmp_path, locks, wait_seconds=0.2)

    assert _run(supervisor) == 0
    assert "healthy" in runtime.phases
    # Released on exit, so the next handoff can take it.
    with locks.lock("runtime-single-instance", timeout_seconds=0):
        pass
