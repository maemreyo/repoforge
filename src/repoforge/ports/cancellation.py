"""Cross-thread cooperative cancellation for one bounded subprocess execution.

A `CancellationToken` is created by the thread that starts a background command
run and is safe to `cancel()` from any other thread at any time -- including
before a process has been bound, or after it has already exited. It reuses the
same process-group signal used by RepoForge's existing subprocess timeout path
(`SIGTERM`, escalating to `SIGKILL` only if the process ignores it and its own
timeout later elapses); it never invents a second kill mechanism.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
from collections.abc import Callable
from typing import Protocol


class _KillableProcess(Protocol):
    @property
    def pid(self) -> int: ...


class CancellationToken:
    """Thread-safe handoff letting one external thread terminate a bound process group."""

    def __init__(
        self,
        *,
        on_spawn: Callable[[], None] | None = None,
        raise_on_spawn_error: bool = False,
        on_bind: Callable[[int], None] | None = None,
        raise_on_bind_error: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._process: _KillableProcess | None = None
        self._cancelled = threading.Event()
        self._on_spawn = on_spawn
        self._raise_on_spawn_error = raise_on_spawn_error
        self._on_bind = on_bind
        self._raise_on_bind_error = raise_on_bind_error

    def before_spawn(self) -> None:
        """Cross the durable spawn boundary before a subprocess is created.

        A strict observer failure propagates so callers fail closed without
        creating a child whose existence recovery cannot prove.
        """
        observer = self._on_spawn
        if observer is None:
            return
        if self._raise_on_spawn_error:
            observer()
        else:
            with contextlib.suppress(Exception):
                observer()

    def bind(self, process: _KillableProcess) -> None:
        """Register the live process this token can terminate.

        If cancellation was already requested before the process started, the
        process is signalled immediately. An ``on_bind`` observer (if any) is
        notified with the process pid so a caller can durably record the worker
        it just spawned; observer failures never disturb execution.
        """
        with self._lock:
            self._process = process
            already_requested = self._cancelled.is_set()
            observer = self._on_bind
        if observer is not None:
            if self._raise_on_bind_error:
                observer(process.pid)
            else:
                with contextlib.suppress(Exception):
                    observer(process.pid)
        if already_requested:
            self._terminate(process)

    def release(self) -> None:
        """Detach the bound process once it has exited; safe to call unconditionally."""
        with self._lock:
            self._process = None

    def cancel(self) -> None:
        """Request cancellation, signalling a bound process group immediately if any."""
        with self._lock:
            self._cancelled.set()
            process = self._process
        if process is not None:
            self._terminate(process)

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @staticmethod
    def _terminate(process: _KillableProcess) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
