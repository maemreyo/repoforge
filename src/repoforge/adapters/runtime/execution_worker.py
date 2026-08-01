"""Subprocess lifecycle for the isolated durable execution worker."""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.execution_worker import ExecutionWorkerBinding
from ...domain.runtime import ChildProcess
from ...ports.execution_worker_store import ExecutionWorkerBindingStore
from ..subprocess.process_tree import read_identity
from .state_store import process_identity


class SubprocessExecutionWorker:
    def __init__(
        self,
        config_path: Path,
        *,
        bindings: ExecutionWorkerBindingStore | None = None,
    ) -> None:
        self._config_path = config_path.expanduser().resolve()
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._worker_ids: dict[int, str] = {}
        self._bindings = bindings

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
        argv = [
            sys.executable,
            "-m",
            "repoforge.interfaces.runtime.execution_worker",
            "--config",
            str(self._config_path),
            "--generation",
            str(generation),
        ]
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                argv,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        identity = process_identity(process.pid)
        if identity is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            raise ConfigError("Could not establish execution worker process identity")
        self._children[process.pid] = process
        self._record_binding(process.pid, generation, correlation_id or "", identity)
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
        self._children.pop(child.pid, None)
        self._mark_state(child.pid, "reclaimed")

    def _record_binding(
        self, pid: int, generation: int, correlation_id: str, identity: str
    ) -> None:
        """Persist the durable per-worker binding, best-effort.

        The record is what lets a supervisor that starts AFTER this one dies prove
        and reap the orphan. Best-effort by design: an unreadable owner identity or an
        unwritable store must never fail a worker spawn -- absence of the record
        degrades to fail-closed reaping (nothing is killed without proof), never to
        an unbounded orphan.
        """
        if self._bindings is None:
            return
        supervisor_identity = process_identity(os.getpid())
        if supervisor_identity is None:
            return
        proc_identity = read_identity(pid)
        token = proc_identity.start_token if proc_identity is not None else None
        worker_id = f"worker-{pid}-{hashlib.sha256((token or str(pid)).encode()).hexdigest()[:12]}"
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
            state="running",
        )
        with contextlib.suppress(Exception):
            self._bindings.put(binding)
            self._worker_ids[pid] = worker_id

    def _mark_state(self, pid: int, state: str) -> None:
        if self._bindings is None:
            return
        worker_id = self._worker_ids.get(pid)
        if worker_id is None:
            return
        with contextlib.suppress(Exception):
            self._bindings.update_state(worker_id, state)
