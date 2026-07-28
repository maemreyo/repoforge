"""Lease-owning polling loop for durable operation work."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import OperationRetryability, OperationState
from ...domain.operation_work import (
    OperationWorkItem,
    mark_work_child_started,
    renew_work_claim,
)
from ...domain.operation_worker import OperationWorkerBinding
from ...ports.cancellation import CancellationToken
from ..context import ApplicationContext
from ..dto import to_data
from .manager import OperationManager
from .recovery import recover_operation_work

_LEASE_SECONDS = 90
_DEFAULT_IDLE_POLL_SECONDS = 0.25
_DEFAULT_HEARTBEAT_SECONDS = 10.0
_DEFAULT_RECOVERY_SECONDS = 30.0
_COMPATIBLE_KINDS = frozenset({"profile", "adhoc", "diagnostic"})


class OperationWorkHandlers(Protocol):
    def execute(
        self,
        item: OperationWorkItem,
        *,
        cancellation_token: CancellationToken,
        progress: Callable[[str, int, int, str, str], None],
    ) -> object: ...


def _lease_deadline(now: str) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=_LEASE_SECONDS)).isoformat()


def _error_code(exc: Exception) -> ErrorCode:
    raw = getattr(getattr(exc, "code", None), "value", getattr(exc, "code", None))
    try:
        return ErrorCode(str(raw))
    except ValueError:
        return ErrorCode.INTERNAL_ERROR


class OperationWorkLoop:
    """Claim and execute one durable work item at a time."""

    def __init__(
        self,
        ctx: ApplicationContext,
        operations: OperationManager,
        handlers: OperationWorkHandlers,
        *,
        owner_id: str | None = None,
        idle_poll_seconds: float = _DEFAULT_IDLE_POLL_SECONDS,
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        recovery_interval_seconds: float = _DEFAULT_RECOVERY_SECONDS,
    ) -> None:
        if (
            ctx.operation_work_queue is None
            or ctx.operation_result_store is None
            or ctx.worker_bindings is None
            or ctx.reaper is None
        ):
            raise RepoForgeError(
                "Durable execution requires work queue and operation result stores",
                code=ErrorCode.CONFIG_INVALID,
            )
        if idle_poll_seconds <= 0:
            raise ValueError("idle_poll_seconds must be positive")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if recovery_interval_seconds <= 0:
            raise ValueError("recovery_interval_seconds must be positive")
        self._ctx = ctx
        self._operations = operations
        self._handlers = handlers
        self._queue = ctx.operation_work_queue
        self._results = ctx.operation_result_store
        self._worker_bindings = ctx.worker_bindings
        self._reaper = ctx.reaper
        self._owner_id = owner_id or f"worker-{ctx.ids.new_hex(24)}"
        self._idle_poll_seconds = idle_poll_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._recovery_interval_seconds = recovery_interval_seconds
        self._stop = threading.Event()

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def request_stop(self) -> None:
        self._stop.set()

    def run_until_stopped(self, stop_event: threading.Event | None = None) -> None:
        external_stop = stop_event or threading.Event()
        next_recovery_at = time.monotonic()
        while not self._stop.is_set() and not external_stop.is_set():
            monotonic_now = time.monotonic()
            # One iteration must not be able to end the loop. This thread IS durable
            # execution: when it dies nothing drains the queue, and the only evidence was
            # a pytest thread-exception warning -- in production, silence. So an
            # unexpected failure is recorded and the loop backs off and continues.
            try:
                if monotonic_now >= next_recovery_at:
                    recover_operation_work(
                        self._operations,
                        self._queue,
                        now=self._ctx.clock.now_iso(),
                        expected_config_generation=self._ctx.config_generation or None,
                        worker_bindings=self._worker_bindings,
                        reaper=self._reaper,
                    )
                    next_recovery_at = monotonic_now + self._recovery_interval_seconds
                if not self.run_once():
                    self._stop.wait(self._idle_poll_seconds)
            except Exception as exc:
                self._report_iteration_failure(exc)
                self._stop.wait(self._idle_poll_seconds)

    def _report_iteration_failure(self, exc: Exception) -> None:
        """Audit an iteration that failed, never re-raising into the loop."""
        with contextlib.suppress(Exception):
            self._ctx.audited(
                "operation_work_loop_iteration_failed",
                {
                    "owner_id": self._owner_id,
                    "error_code": _error_code(exc).value,
                    "error_type": type(exc).__name__,
                },
                lambda: None,
                mutating=False,
                synthetic=True,
            )

    def run_once(self) -> bool:
        claim_now = self._ctx.clock.now_iso()
        lease_expires_at = _lease_deadline(claim_now)
        item = self._queue.claim_next(
            owner_id=self._owner_id,
            now=claim_now,
            lease_expires_at=lease_expires_at,
            compatible_kinds=_COMPATIBLE_KINDS,
            config_generation=self._ctx.config_generation or None,
        )
        if item is None:
            return False

        operation_id = item.operation_id
        try:
            self._operations.start(
                operation_id,
                owner_id=self._owner_id,
                lease_expires_at=lease_expires_at,
                attempt=item.attempt,
                now=claim_now,
            )
        except RepoForgeError as exc:
            if exc.code is not ErrorCode.OPERATION_NOT_FOUND:
                raise
            # A claimed item whose operation record cannot be read is unexecutable: there
            # is nothing to transition, report progress against, or terminalize. Discard
            # the sidecar and keep serving the queue. Letting this raise killed the whole
            # worker thread, so one unreadable record stopped ALL durable execution -- the
            # queue kept filling and nothing drained it.
            self._queue.delete(operation_id)
            self._ctx.audited(
                "operation_work_unexecutable",
                {
                    "operation_id": operation_id,
                    "owner_id": self._owner_id,
                    "error_code": ErrorCode.OPERATION_NOT_FOUND.value,
                    "detail": "claimed work had no readable operation record; sidecar discarded",
                },
                lambda: None,
                mutating=True,
                synthetic=True,
            )
            return True
        state_lock = threading.Lock()

        def cross_spawn_boundary() -> None:
            with state_lock:
                current = self._queue.read(operation_id)
                if current is None:
                    raise RepoForgeError(
                        "Durable work disappeared before its spawn boundary could be recorded",
                        code=ErrorCode.STATE_INVALID,
                    )
                started = mark_work_child_started(
                    current,
                    owner_id=self._owner_id,
                    attempt=item.attempt,
                    now=self._ctx.clock.now_iso(),
                )
                self._queue.save(started, expected_updated_at=current.updated_at)

        bound_child: list[OperationWorkerBinding] = []

        def bind_child(child_pid: int) -> None:
            with state_lock:
                current = self._queue.read(operation_id)
                if current is None:
                    raise RepoForgeError(
                        "Durable work disappeared before child identity could be bound",
                        code=ErrorCode.OPERATION_STALE,
                    )
                mark_work_child_started(
                    current,
                    owner_id=self._owner_id,
                    attempt=item.attempt,
                    now=self._ctx.clock.now_iso(),
                )
                task = self._operations.status(operation_id)
                if task.state is not OperationState.RUNNING or task.owner_id != self._owner_id:
                    raise RepoForgeError(
                        "Operation ownership changed before child identity could be bound",
                        code=ErrorCode.OPERATION_STALE,
                    )
                child_token = self._reaper.read_start_token(child_pid)
                server_pid = os.getpid()
                server_token = self._reaper.read_start_token(server_pid)
                if child_token is None or server_token is None:
                    raise RepoForgeError(
                        "Could not establish PID-reuse-safe child identity",
                        code=ErrorCode.STATE_INVALID,
                    )
                binding = OperationWorkerBinding(
                    operation_id=operation_id,
                    child_pid=child_pid,
                    child_pgid=child_pid,
                    child_start_token=child_token,
                    server_pid=server_pid,
                    server_start_token=server_token,
                    created_at=self._ctx.clock.now_iso(),
                    owner_generation=self._ctx.config_generation or None,
                    owner_id=self._owner_id,
                    attempt=item.attempt,
                )
                self._worker_bindings.put(binding)
                bound_child[:] = [binding]

        cancellation_token = CancellationToken(
            on_spawn=cross_spawn_boundary,
            raise_on_spawn_error=True,
            on_bind=bind_child,
            raise_on_bind_error=True,
        )
        monitor_stop = threading.Event()
        monitor_errors: list[Exception] = []

        def renew_claims() -> None:
            with state_lock:
                task = self._operations.status(operation_id)
                if task.cancellation_requested_at is not None:
                    cancellation_token.cancel()
                    return
                heartbeat_now = self._ctx.clock.now_iso()
                renewed_deadline = _lease_deadline(heartbeat_now)
                work = self._queue.read(operation_id)
                if work is None:
                    raise RepoForgeError(
                        "Durable work disappeared while its worker was running",
                        code=ErrorCode.STATE_INVALID,
                    )
                renewed_work = renew_work_claim(
                    work,
                    owner_id=self._owner_id,
                    lease_expires_at=renewed_deadline,
                    now=heartbeat_now,
                )
                self._queue.save(renewed_work, expected_updated_at=work.updated_at)
                try:
                    self._operations.renew_ownership(
                        operation_id,
                        owner_id=self._owner_id,
                        lease_expires_at=renewed_deadline,
                        now=heartbeat_now,
                    )
                except RepoForgeError:
                    refreshed = self._operations.status(operation_id)
                    if refreshed.cancellation_requested_at is not None:
                        cancellation_token.cancel()
                        return
                    raise

        def monitor() -> None:
            while not monitor_stop.wait(self._heartbeat_interval_seconds):
                try:
                    renew_claims()
                except Exception as exc:
                    current = self._operations.status(operation_id)
                    if current.cancellation_requested_at is not None:
                        cancellation_token.cancel()
                        return
                    monitor_errors.append(exc)
                    cancellation_token.cancel()
                    return

        monitor_thread = threading.Thread(
            target=monitor,
            name=f"repoforge-operation-monitor-{operation_id}",
            daemon=True,
        )

        def stop_monitor() -> None:
            monitor_stop.set()
            monitor_thread.join(timeout=1.0)
            if monitor_thread.is_alive() and not monitor_errors:
                monitor_errors.append(
                    RepoForgeError(
                        "Durable operation heartbeat monitor did not stop",
                        code=ErrorCode.INTERNAL_ERROR,
                    )
                )
                cancellation_token.cancel()

        def progress(
            phase: str,
            current: int,
            total: int,
            unit: str,
            message: str,
        ) -> None:
            with state_lock:
                task = self._operations.status(operation_id)
                if task.cancellation_requested_at is not None:
                    cancellation_token.cancel()
                    raise RepoForgeError(
                        "Durable operation cancellation was requested",
                        code=ErrorCode.COMMAND_FAILED,
                    )
                progress_now = self._ctx.clock.now_iso()
                renewed_deadline = _lease_deadline(progress_now)
                work = self._queue.read(operation_id)
                if work is None:
                    raise RepoForgeError(
                        "Durable work disappeared while its child was running",
                        code=ErrorCode.STATE_INVALID,
                    )
                renewed_work = renew_work_claim(
                    work,
                    owner_id=self._owner_id,
                    lease_expires_at=renewed_deadline,
                    now=progress_now,
                )
                self._queue.save(renewed_work, expected_updated_at=work.updated_at)
                self._operations.progress(
                    operation_id,
                    phase=phase,
                    current=current,
                    total=total,
                    unit=unit,
                    message=message,
                    owner_id=self._owner_id,
                    lease_expires_at=renewed_deadline,
                    now=progress_now,
                )

        monitor_thread.start()
        try:
            result = self._handlers.execute(
                item,
                cancellation_token=cancellation_token,
                progress=progress,
            )
            stop_monitor()
            if monitor_errors:
                raise monitor_errors[0]
            current_task = self._operations.status(operation_id)
            if current_task.cancellation_requested_at is not None:
                cancellation_token.cancel()
                self._results.delete(operation_id)
                self._operations.cancelled(operation_id, owner_id=self._owner_id)
            else:
                result_data = to_data(result)
                if not isinstance(result_data, dict):
                    raise RepoForgeError(
                        "Durable work result must serialize to an object",
                        code=ErrorCode.STATE_INVALID,
                    )
                self._results.save(operation_id, result_data)
                self._operations.succeed(
                    operation_id,
                    result_reference=f"operation-result:{operation_id}",
                    owner_id=self._owner_id,
                )
        except Exception as exc:
            stop_monitor()
            self._results.delete(operation_id)
            current = self._operations.status(operation_id)
            if current.cancellation_requested_at is not None:
                self._operations.cancelled(operation_id, owner_id=self._owner_id)
            else:
                effective_exc = monitor_errors[0] if monitor_errors else exc
                self._operations.fail(
                    operation_id,
                    error_code=_error_code(effective_exc).value,
                    error_message=str(effective_exc),
                    retryability=(
                        OperationRetryability.AUTOMATIC
                        if bool(getattr(effective_exc, "retryable", False))
                        else OperationRetryability.MANUAL
                    ),
                    owner_id=self._owner_id,
                )
        finally:
            stop_monitor()
            for binding in bound_child:
                self._worker_bindings.delete_if_unchanged(binding)
            self._queue.delete(operation_id)
        return True
