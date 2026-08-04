"""Subprocess lifecycle for the isolated durable execution worker."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from ...application.runtime.worker_lifecycle import WorkerLifecycleStore
from ...domain.durable_state import Revision
from ...domain.errors import ConfigError, ExecutionWorkerRegistrationError
from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.process_lease import ProcessLease, ProcessLeaseRole
from ...domain.runtime import ChildProcess
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ...ports.process_reaper import ProcessReaper
from ...ports.worker_registrar import WorkerRegistrar
from ..subprocess.os_process_reaper import OsProcessReaper
from ..subprocess.process_tree import read_identity
from .state_store import process_identity

# Bounded settle window for a freshly exec'd worker's process identity. On macOS a
# venv `sys.executable` shim re-execs onto the real interpreter, so `ps -o command=`
# -- and the identity hashed from it -- can change in the first moments after Popen.
# Poll until the identity is stable rather than trusting a single pre-exec sample.
_IDENTITY_SETTLE_SECONDS = 10.0
_IDENTITY_POLL_INTERVAL = 0.02
_REGISTRATION_REAP_SECONDS = 5.0

#: Env var carrying the pre-spawn lease id to the worker, so a later child-side
#: handshake can claim the same lease (F-001 protocol).
LEASE_ID_ENV = "REPOFORGE_EXECUTION_WORKER_LEASE_ID"
#: Env var carrying the supervisor pid to the worker; the child-side claim acts
#: only when this process is provably dead (F-001 P0).
SUPERVISOR_PID_ENV = "REPOFORGE_SUPERVISOR_PID"
#: Env var carrying the supervisor's process identity to the worker, so the
#: child's binding projection still names the (dead) owner a reconciler can
#: recognize.
SUPERVISOR_IDENTITY_ENV = "REPOFORGE_SUPERVISOR_PROCESS_IDENTITY"
#: Env var carrying the runtime state root to the worker, so the child-side
#: claim reads and writes the SAME lease registry the parent's stores use --
#: never a registry re-derived from config defaults (F-001 P0).
STATE_ROOT_ENV = "REPOFORGE_RUNTIME_STATE_ROOT"


@dataclass(frozen=True, slots=True)
class _ReapTarget:
    """Ad-hoc ProcessGroupTarget for spawn-time and normal-termination reaps."""

    child_pid: int
    child_pgid: int
    child_start_token: str | None


class SubprocessExecutionWorker:
    def __init__(
        self,
        config_path: Path,
        *,
        bindings: ExecutionWorkerBindingStore,
        registrar: WorkerRegistrar,
        reaper: ProcessReaper | None = None,
        lifecycle: WorkerLifecycleStore | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._config_path = config_path.expanduser().resolve()
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._worker_ids: dict[int, str] = {}
        self._bindings = bindings
        self._registrar = registrar
        self._reaper = reaper if reaper is not None else OsProcessReaper()
        self._lifecycle = (
            lifecycle
            if lifecycle is not None
            else WorkerLifecycleStore(
                bindings=bindings,
                leases=None,
                shadow=None,
                now_iso=None,
                # No lease authority was injected, so this embedder persists only the
                # binding projection (binding-only opt-out). Production always wires a
                # lease store via bootstrap, so there the lifecycle is strict (F-010).
                binding_only=True,
            )
        )
        self._state_root = state_root.expanduser().resolve() if state_root is not None else None

    def start(
        self,
        generation: int,
        *,
        env: dict[str, str],
        log_path: Path,
        correlation_id: str | None = None,
    ) -> ChildProcess:
        if generation <= 0:
            raise ConfigError("Execution worker generation must be positive")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        correlation = correlation_id or ""
        # F-001: the durable REGISTERED intent is written BEFORE any process exists,
        # so a crash at any later point leaves a discoverable record instead of an
        # invisible orphan.
        lease, revision = self._registrar.create_intent(
            role=ProcessLeaseRole.EXECUTION_DAEMON,
            correlation_id=correlation,
        )
        argv = [
            sys.executable,
            "-m",
            "repoforge.interfaces.runtime.execution_worker",
            "--config",
            str(self._config_path),
            "--generation",
            str(generation),
        ]
        worker_env = dict(env)
        worker_env[LEASE_ID_ENV] = lease.lease_id
        supervisor_identity = process_identity(os.getpid())
        worker_env[SUPERVISOR_PID_ENV] = str(os.getpid())
        if supervisor_identity is not None:
            worker_env[SUPERVISOR_IDENTITY_ENV] = supervisor_identity
        if self._state_root is not None:
            worker_env[STATE_ROOT_ENV] = str(self._state_root)
        process: subprocess.Popen[bytes] | None = None
        try:
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    argv,
                    env=worker_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            # Persist the pid the instant Popen returns: the process now has a lease AND
            # a pid, so a crash here leaves a record a later supervisor can probe. The
            # owner fields land here too (the supervisor already knows its own pid and
            # identity), so a parent that dies before complete_registration still leaves
            # a lease self-sufficient for lease-only recovery (F-001 P0).
            lease, revision = self._registrar.record_pid(
                lease,
                pid=process.pid,
                expected_revision=revision,
                owner_pid=os.getpid(),
                owner_process_identity=supervisor_identity,
            )
            identity = self._establish_stable_identity(process)
            if identity is None:
                self._fail_registration(
                    lease,
                    process.pid,
                    revision,
                    "the execution worker process identity could not be established",
                )
            self._children[process.pid] = process
            # Registration is mandatory: without a durable lease the worker could not be
            # reclaimed later, so an unregistered worker is terminated, never left to run.
            worker_id = self._register_binding(
                lease, process.pid, generation, correlation, identity, revision
            )
            self._worker_ids[process.pid] = worker_id
            # The parent returns a worker only after the lease is durably RUNNING. The
            # lease carries the worker's start token too (single authority, F-008): a
            # RUNNING lease without a token is incomplete evidence, so the token that
            # the binding carries must land on the canonical lease as well.
            proc_identity = read_identity(process.pid)
            token = proc_identity.start_token if proc_identity is not None else None
            lease, revision = self._registrar.complete_registration(
                lease,
                process_identity=identity,
                expected_revision=revision,
                process_start_token=token,
            )
        except Exception as exc:
            # Any failure after Popen must never leave a live, untraceable worker:
            # terminalize the intent (best effort), TERM -> KILL the child, confirm
            # it is gone, then raise the typed registration error so the supervisor
            # fails closed instead of treating it as a transient blip (F-001).
            if process is not None and process.poll() is None:
                self._reap_unregistered(process.pid)
            with contextlib.suppress(Exception):
                self._registrar.abort_intent(
                    lease,
                    error_code="EXECUTION_WORKER_REGISTRATION_FAILED",
                    error_message=f"{type(exc).__name__}: {exc}",
                    expected_revision=revision,
                )
            raise ExecutionWorkerRegistrationError(
                f"EXECUTION_WORKER_REGISTRATION_FAILED: {type(exc).__name__}: {exc}; "
                "the spawned worker was terminated and confirmed dead"
            ) from exc
        return ChildProcess(
            process.pid,
            identity,
            datetime.now(timezone.utc).isoformat(),
        )

    def is_alive(self, child: ChildProcess) -> bool:
        process = self._children.get(child.pid)
        return bool(
            process is not None
            and process.poll() is None
            and process_identity(child.pid) == child.process_identity
        )

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None:
        """Group-aware termination: same death semantics as the reconciler reaper.

        Leader-only ``poll()`` is not proof the process group is gone: a descendant
        can survive SIGTERM after the leader exits. All worker termination goes
        through ``OsProcessReaper`` so a live group member never becomes
        ``reclaimed``.
        """
        process = self._children.get(child.pid)
        if process is None or process_identity(child.pid) != child.process_identity:
            self._children.pop(child.pid, None)
            self._apply_lifecycle(child.pid, "already_gone")
            return
        identity = read_identity(child.pid)
        start_token = identity.start_token if identity is not None else None
        if isinstance(self._reaper, OsProcessReaper):
            reaper: ProcessReaper = OsProcessReaper(term_grace_seconds=max(0.0, grace_seconds))
        else:
            reaper = self._reaper
        outcome = reaper.reap(
            _ReapTarget(
                child_pid=child.pid,
                child_pgid=child.pid,
                child_start_token=start_token,
            )
        )
        self._children.pop(child.pid, None)
        if outcome.reaped:
            self._apply_lifecycle(child.pid, "reclaimed" if outcome.attempted else "already_gone")
        elif outcome.still_alive:
            self._apply_lifecycle(child.pid, "survived_kill")
        else:
            # PID reuse or unproven containment without a live claim: refuse reclaimed.
            self._apply_lifecycle(child.pid, "refused_unproven")

    def _establish_stable_identity(self, process: subprocess.Popen[bytes]) -> str | None:
        """Wait for the worker's process identity to settle, then record it.

        The identity hashes ``ps -o lstart= -o command=``. On macOS the venv's
        ``sys.executable`` is a shim that re-execs onto the real interpreter, so
        the command line -- and therefore the identity -- changes in the first
        moments after ``Popen`` returns. Sampling once in that window records an
        identity no later sample will match, and ``is_alive`` would report a live
        worker as gone. Poll until the identity is observed unchanged twice
        consecutively (bounded), or fail closed on ``None`` exactly as before.
        """
        deadline = time.monotonic() + _IDENTITY_SETTLE_SECONDS
        previous: str | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return None
            current = process_identity(process.pid)
            if current is not None and current == previous:
                return current
            previous = current
            time.sleep(_IDENTITY_POLL_INTERVAL)
        return None

    def _register_binding(
        self,
        lease: ProcessLease,
        pid: int,
        generation: int,
        correlation_id: str,
        identity: str,
        expected_revision: Revision,
    ) -> str:
        """Persist the durable per-worker binding; mandatory for a live worker (#424).

        The binding shares the pre-spawn lease's id so the reconciler's record and
        the registrar's lease name the same worker. Every failure path aborts the
        intent lease, terminates the spawned worker, and raises
        ``ExecutionWorkerRegistrationError`` -- a worker is never returned as started
        without a durable lease a later supervisor could prove and reclaim.
        """
        supervisor_identity = process_identity(os.getpid())
        if supervisor_identity is None:
            self._fail_registration(
                lease,
                pid,
                expected_revision,
                "the supervisor process identity could not be established",
            )
        proc_identity = read_identity(pid)
        token = proc_identity.start_token if proc_identity is not None else None
        # A running binding requires a start token; without one, record the worker as
        # an unproven concern so a later reconciler still sees it (fail closed) (#420).
        state = "running" if token is not None else "refused_unproven"
        worker_id = lease.lease_id
        binding = ExecutionWorkerBinding(
            worker_id=worker_id,
            pid=pid,
            pgid=pid,
            process_start_token=token,
            generation=generation,
            release_sha=os.environ.get("REPOFORGE_RUNNING_RELEASE_SHA"),
            supervisor_pid=os.getpid(),
            supervisor_process_identity=supervisor_identity,
            correlation_id=correlation_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            state=state,
        )
        try:
            self._bindings.put(binding)
        except Exception as exc:
            self._fail_registration(
                lease,
                pid,
                expected_revision,
                f"the durable worker lease could not be written: {exc}",
            )
        return worker_id

    def _fail_registration(
        self,
        lease: ProcessLease,
        pid: int,
        expected_revision: Revision,
        reason: str,
    ) -> NoReturn:
        """Abort the intent lease, TERM -> wait -> KILL, verify death, then raise.

        The pre-spawn lease is terminalized so it never dangles as a REGISTERED
        anomaly, then the unregistered worker is reaped with death verified (F-002)
        before the typed failure is raised.
        """
        with contextlib.suppress(Exception):
            self._registrar.abort_intent(
                lease,
                error_code="EXECUTION_WORKER_REGISTRATION_FAILED",
                error_message=reason,
                expected_revision=expected_revision,
            )
        self._reap_unregistered(pid)
        raise ExecutionWorkerRegistrationError(
            f"EXECUTION_WORKER_REGISTRATION_FAILED: {reason}; the spawned worker "
            "was terminated and confirmed dead"
        )

    def _reap_unregistered(self, pid: int) -> None:
        """Boundedly terminate a worker that must not keep running (#424).

        Uses the same group-aware reaper as normal termination and the reconciler
        so a leader-exit with live descendants never counts as confirmed dead.
        """
        identity = read_identity(pid)
        start_token = identity.start_token if identity is not None else None
        reaper = (
            OsProcessReaper(term_grace_seconds=_REGISTRATION_REAP_SECONDS)
            if isinstance(self._reaper, OsProcessReaper)
            else self._reaper
        )
        outcome = reaper.reap(
            _ReapTarget(child_pid=pid, child_pgid=pid, child_start_token=start_token)
        )
        self._children.pop(pid, None)
        if outcome.reaped:
            return
        raise ExecutionWorkerRegistrationError(
            f"EXECUTION_WORKER_NOT_CONFIRMED_DEAD: the spawned worker with pid {pid} "
            f"was not proven gone ({outcome.detail}); refusing to report it as "
            "terminated. The worker may still be running and holding locks; inspect "
            "the process table and reclaim it manually before starting a replacement."
        )

    def _apply_lifecycle(self, pid: int, state: str) -> None:
        """Persist one termination outcome through the shared lifecycle service.

        Normal termination must update the canonical ProcessLease and the shadow
        exactly like the reconciler does (single authority): a worker recorded as
        ``reclaimed`` here while its lease stayed RUNNING was the reviewed
        split-brain. A persistence failure propagates -- the caller must never
        believe the registry recorded a termination it did not.
        """
        worker_id = self._worker_ids.get(pid)
        if worker_id is None:
            return
        self._lifecycle.apply_outcome(worker_id, state)
