"""Subprocess lifecycle for the isolated durable execution worker."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from ...domain.errors import ConfigError, ExecutionWorkerRegistrationError
from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.process_lease import ProcessLease, ProcessLeaseRole
from ...domain.runtime import ChildProcess
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ...ports.worker_registrar import WorkerRegistrar
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


class SubprocessExecutionWorker:
    def __init__(
        self,
        config_path: Path,
        *,
        bindings: ExecutionWorkerBindingStore,
        registrar: WorkerRegistrar,
    ) -> None:
        self._config_path = config_path.expanduser().resolve()
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._worker_ids: dict[int, str] = {}
        self._bindings = bindings
        self._registrar = registrar

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
        lease = self._registrar.create_intent(
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
        # a pid, so a crash here leaves a record a later supervisor can probe.
        lease = self._registrar.record_pid(lease, pid=process.pid)
        identity = self._establish_stable_identity(process)
        if identity is None:
            self._fail_registration(
                lease,
                process.pid,
                "the execution worker process identity could not be established",
            )
        self._children[process.pid] = process
        # Registration is mandatory: without a durable lease the worker could not be
        # reclaimed later, so an unregistered worker is terminated, never left to run.
        worker_id = self._register_binding(lease, process.pid, generation, correlation, identity)
        self._worker_ids[process.pid] = worker_id
        # The parent returns a worker only after the lease is durably RUNNING.
        self._registrar.complete_registration(lease, process_identity=identity)
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
        process = self._children.get(child.pid)
        if process is None or process_identity(child.pid) != child.process_identity:
            self._children.pop(child.pid, None)
            self._mark_state(child.pid, "already_gone")
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)
            # State records truth, not intent: only after SIGKILL is the identity
            # re-probed. A SIGTERM exit is already confirmed by poll(); re-probing
            # there would only add latency to every graceful shutdown (#420).
            self._children.pop(child.pid, None)
            if process_identity(child.pid) is None:
                self._mark_state(child.pid, "reclaimed")
            else:
                self._mark_state(child.pid, "survived_kill")
            return
        self._children.pop(child.pid, None)
        self._mark_state(child.pid, "reclaimed")

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
                f"the durable worker lease could not be written: {exc}",
            )
        return worker_id

    def _fail_registration(self, lease: ProcessLease, pid: int, reason: str) -> NoReturn:
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
            )
        self._reap_unregistered(pid)
        raise ExecutionWorkerRegistrationError(
            f"EXECUTION_WORKER_REGISTRATION_FAILED: {reason}; the spawned worker "
            "was terminated and confirmed dead"
        )

    def _reap_unregistered(self, pid: int) -> None:
        """Boundedly terminate a worker that must not keep running (#424)."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + _REGISTRATION_REAP_SECONDS
        while time.monotonic() < deadline:
            if process_identity(pid) is None:
                break
            time.sleep(0.05)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
            self._confirm_gone_after_kill(pid)
        self._children.pop(pid, None)

    def _confirm_gone_after_kill(self, pid: int) -> None:
        """Prove the process is absent after SIGKILL before anyone claims it is dead.

        "Confirmed dead" is a claim, not a hope: a process that survived both
        SIGTERM and SIGKILL is still running and holding locks, and reporting it
        terminated would let a replacement start on false evidence. Fail closed
        instead -- the caller must not proceed as if the worker were gone.
        """
        deadline = time.monotonic() + _REGISTRATION_REAP_SECONDS
        while time.monotonic() < deadline:
            if process_identity(pid) is None:
                return
            time.sleep(0.05)
        raise ExecutionWorkerRegistrationError(
            f"EXECUTION_WORKER_NOT_CONFIRMED_DEAD: the spawned worker with pid {pid} "
            "survived SIGTERM and SIGKILL; refusing to report it as terminated. The "
            "worker may still be running and holding locks; inspect the process "
            "table and reclaim it manually before starting a replacement."
        )

    def _mark_state(self, pid: int, state: str) -> None:
        if self._bindings is None:
            return
        worker_id = self._worker_ids.get(pid)
        if worker_id is None:
            return
        with contextlib.suppress(Exception):
            self._bindings.update_state(worker_id, state)
