"""Versioned, allowlisted, owner-only Unix-domain runtime control protocol."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import queue
import socket
import struct
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import (
    RUNTIME_CONTROL_PROTOCOL_VERSION,
    ControlCommand,
    ControlRequest,
    ControlResponse,
)

_MAX_MESSAGE = 64 * 1024
_PROTOCOL = RUNTIME_CONTROL_PROTOCOL_VERSION
_MAX_SOCKET_PATH_BYTES = 100
# Matches the listener's own `listen(8)` backlog (#448 Slice 1): admission is bounded to
# roughly what the kernel would queue anyway, so a caller that would have waited in the
# backlog instead gets an immediate, typed, retryable answer.
_DEFAULT_MAX_CONCURRENT_REQUESTS = 8
# One exchange is a local request/response over a Unix socket: a peer that cannot finish
# in this long is not one this loop may wait on, because waiting means serving nobody.
_CLIENT_TIMEOUT_SECONDS = 5.0
# The overload-rejection path runs directly on the accept thread (see
# `reject_overloaded`'s own docstring), so bounding it to the same
# `_CLIENT_TIMEOUT_SECONDS` as a normal exchange would let a caller that never
# reads its response, or never lets this end finish draining, stall the *entire
# accept loop* for that whole duration -- a serial denial-of-service, not merely
# "bounded" (#448 Slice 1 hardening). Rejecting fast is the entire point of this
# path, so its budget is much shorter than a real exchange's.
_ACCEPT_THREAD_REJECTION_TIMEOUT_SECONDS = 0.2
# `close()` waits for outstanding handlers to finish, bounded by this deadline: a
# handler blocked on its own connection's socket I/O is already bounded by
# `_CLIENT_TIMEOUT_SECONDS`, but a handler hung in non-socket code cannot be forced
# to stop (Python has no safe way to kill a running thread), so this is the honest
# residual bound that keeps `close()` itself from hanging forever in that case.
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 10.0
# A queued unit of work: the function to run and the connection to run it on, or
# `None` as a one-shot "stop" signal to a single worker.
_WorkItem = tuple[Callable[[socket.socket], None], socket.socket] | None


class _ControlServerRun:
    """Everything one `start()`/`close()` generation owns, captured together.

    A handler stuck in non-socket code cannot be force-stopped (Python has no
    safe way to kill a running thread), so its worker thread can still be
    alive -- still holding an admission permit, still able to touch counters
    -- when the *same server object* is `start()`ed again. If the admission
    semaphore, work queue, worker list, or in-flight counter lived on `self`,
    a subsequent generation would silently inherit that leftover state: fewer
    permits than configured, a stale in-flight count, or (if the queue were
    reused) a stray worker picking up a task meant for the new generation.
    Every closure created in `start()` captures this object directly, never
    `self`, so a previous generation's worker can only ever mutate its own
    generation's accounting (#448 Slice 1 cross-restart isolation).
    """

    def __init__(
        self, run_id: int, listener: socket.socket, admission: threading.BoundedSemaphore
    ) -> None:
        self.run_id = run_id
        self.listener = listener
        self.admission = admission
        self.stop = threading.Event()
        self.queue: queue.SimpleQueue[_WorkItem] = queue.SimpleQueue()
        self.workers: list[threading.Thread] = []
        self.accept_thread: threading.Thread | None = None
        self.in_flight_lock = threading.Lock()
        self.in_flight = 0
        self.active_connections: set[socket.socket] = set()
        self.active_connections_lock = threading.Lock()
        self.stopped_reason: str | None = None


def resolve_unix_socket_path(path: Path) -> Path:
    """Return a deterministic portable bind path for a logical Unix socket path.

    Darwin and Linux impose small ``sockaddr_un.sun_path`` limits. Long state roots are mapped into
    a user-private temporary directory, while clients independently derive the same path.
    """
    logical = path.expanduser().absolute()
    if len(os.fsencode(str(logical))) <= _MAX_SOCKET_PATH_BYTES:
        return logical
    digest = hashlib.sha256(os.fsencode(str(logical))).hexdigest()[:32]
    filename = f"{digest}.sock"
    roots = (
        Path("/tmp") / f"rf-{os.getuid()}",
        Path(tempfile.gettempdir()).expanduser().absolute() / f"rf-{os.getuid()}",
    )
    candidates = tuple(root / filename for root in roots)
    return min(candidates, key=lambda candidate: len(os.fsencode(str(candidate))))


def _native_getpeereid_uid(descriptor: int) -> int | None:
    """Read BSD/Darwin peer credentials through libc when Python exposes no wrapper."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
    except (AttributeError, OSError):
        return None
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    getpeereid.restype = ctypes.c_int
    if getpeereid(descriptor, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        return None
    return int(uid.value)


def _peer_uid(connection: socket.socket) -> int | None:
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is not None:
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", credentials)
            return int(uid)
        except (OSError, struct.error):
            # Darwin may expose a similarly named constant with a different ABI. Fall through to
            # the BSD credential APIs rather than denying a same-owner local connection.
            pass
    getpeereid = getattr(connection, "getpeereid", None)
    if callable(getpeereid):
        try:
            uid, _ = getpeereid()
            return int(uid)
        except OSError:
            pass
    try:
        return _native_getpeereid_uid(connection.fileno())
    except OSError:
        return None


def _encode(response: ControlResponse) -> bytes:
    payload = asdict(response)
    payload["payload"] = dict(response.payload)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _drain_before_close(connection: socket.socket) -> None:
    """Best-effort: consume whatever the peer already sent before this end closes.

    Responding and closing without ever reading a peer's already-in-flight request
    can trigger the OS resetting the connection instead of closing it gracefully,
    if unread bytes remain in this end's receive buffer at close time -- which can
    turn the *peer's own pending request write* into a broken pipe on their side,
    even though the peer did nothing wrong. Bounded by whatever timeout is already
    set on `connection` (never blocks longer than that); best-effort and never
    raises, since failing to drain is not itself a correctness problem, only a
    missed optimization (#448 Slice 1).
    """
    with contextlib.suppress(OSError):
        connection.shutdown(socket.SHUT_WR)
    with contextlib.suppress(OSError):
        while connection.recv(4096):
            pass


def _decode_request(data: bytes) -> ControlRequest:
    if len(data) > _MAX_MESSAGE:
        raise ConfigError("Runtime control request is too large")
    try:
        raw: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Malformed runtime control request: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {
        "protocol_version",
        "command",
        "correlation_id",
        "payload",
    }:
        raise ConfigError("Runtime control request contains unsupported fields")
    if raw.get("protocol_version") != _PROTOCOL:
        raise ConfigError("Unsupported runtime control protocol version")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise ConfigError("Runtime control payload must be an object")
    return ControlRequest(
        protocol_version=_PROTOCOL,
        command=ControlCommand(str(raw["command"])),
        correlation_id=str(raw["correlation_id"]),
        payload=tuple(sorted(payload.items())),
    )


class UnixRuntimeControlServer:
    def __init__(
        self,
        path: Path,
        *,
        max_concurrent_requests: int = _DEFAULT_MAX_CONCURRENT_REQUESTS,
        drain_timeout_seconds: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ):
        if max_concurrent_requests <= 0:
            raise ConfigError("max_concurrent_requests must be positive")
        if drain_timeout_seconds <= 0:
            raise ConfigError("drain_timeout_seconds must be positive")
        self.path = path.expanduser().absolute()
        self._bound_path = resolve_unix_socket_path(self.path)
        self._client_failures = 0
        self._max_concurrent_requests = max_concurrent_requests
        self._drain_timeout = drain_timeout_seconds
        # Hand-rolled daemon worker pool, not `concurrent.futures.ThreadPoolExecutor`:
        # its worker threads are non-daemon, and CPython's `atexit`-registered
        # `concurrent.futures.thread._python_exit` blocks interpreter shutdown until
        # *every* such thread across the whole process finishes -- so a handler
        # callback hung forever in non-socket code would keep the entire process
        # alive even after `close()` itself had already returned (confirmed with a
        # real subprocess: `close()` returned in ~2s, the process did not exit for
        # 15s+). Daemon threads are not waited on at interpreter exit, so this
        # bounds process-exit, not just this call (#448 Slice 1 edge case B/process).
        #
        # All admission/queue/counter/worker state for the currently running (or most
        # recently closed) generation lives on `self._run`, a fresh `_ControlServerRun`
        # built in `start()` -- never directly on `self` -- so a worker thread stuck
        # from a previous generation can never leak capacity or accounting into a
        # subsequent one (#448 Slice 1 cross-restart isolation).
        self._run: _ControlServerRun | None = None
        self._run_counter = 0
        self._closed = False

    @property
    def bound_path(self) -> Path:
        return self._bound_path

    def is_serving(self) -> bool:
        """Is the accept loop still running, with a live worker pool behind it?

        The health record is written by a different loop than the one that serves control
        requests, so without this a runtime whose control plane had ended kept reporting
        `phase: healthy` -- every durable fact insisting nothing was wrong while every read
        that needed the socket timed out. That gap cost more diagnosis time than the
        original defect did.

        A live accept thread with every worker dead is not "accepting requests" in any
        useful sense either: every admitted connection would queue forever with nothing
        to ever pick it up, so that case must also report not-serving (#448 Slice 1
        worker-pool observability).
        """
        run = self._run
        if run is None or run.stop.is_set():
            return False
        if run.accept_thread is None or not run.accept_thread.is_alive():
            return False
        return any(worker.is_alive() for worker in run.workers)

    def is_healthy(self) -> bool:
        """Is the control plane serving at its full configured capacity?

        Stricter than `is_serving()` on purpose: this pool is fixed-size and never
        replenishes a worker that dies, so losing even one is a *persistent* capacity
        loss, not a transient blip -- reporting `healthy` here would repeat, one layer
        down, exactly the gap #322 was written to close (a durable fact claiming
        nothing is wrong while the real behavior has already changed). `is_serving()`
        alone must never drive a health boolean for this reason (#448 Slice 1 partial-
        worker health semantics).
        """
        run = self._run
        if run is None or run.stop.is_set():
            return False
        if run.accept_thread is None or not run.accept_thread.is_alive():
            return False
        return all(worker.is_alive() for worker in run.workers)

    def serving_diagnostic(self) -> str:
        """Why the control plane is (not) serving, for the health check's detail."""
        run = self._run
        if run is None:
            return "control server was never started"
        if run.stop.is_set():
            return run.stopped_reason or "control server was asked to stop"
        if run.accept_thread is None or not run.accept_thread.is_alive():
            return run.stopped_reason or "control server thread is no longer running"
        workers_alive = sum(1 for worker in run.workers if worker.is_alive())
        workers_expected = len(run.workers)
        # A dead worker's own reason (set by `worker_loop`'s outer boundary) is
        # worth more than the bare count -- "degraded, 1/2 alive" answers *that*
        # something is wrong, `run.stopped_reason` answers *why*, and the second
        # question is the one that actually shortens diagnosis (#448 Slice 1
        # worker-pool observability).
        reason_suffix = f": {run.stopped_reason}" if run.stopped_reason else ""
        if workers_alive == 0:
            return (
                f"control worker pool exhausted: 0/{workers_expected} workers alive "
                f"(run={run.run_id}){reason_suffix}"
            )
        detail = f"run={run.run_id}, workers={workers_alive}/{workers_expected} alive"
        if workers_alive < workers_expected:
            return f"control socket is accepting requests, degraded ({detail}){reason_suffix}"
        return f"control socket is accepting requests ({detail})"

    def start(self, handler: Callable[[ControlRequest], ControlResponse]) -> None:
        if self._run is not None:
            raise ConfigError("Runtime control server is already started")
        # Restarting after `close()` is a supported lifecycle (#448 Slice 1 edge case
        # E): a fresh `_ControlServerRun` below means this generation gets its own
        # admission semaphore, queue, counters, and worker list, so nothing a prior
        # generation's still-stuck worker does can be observed here.
        self._closed = False
        self.bound_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.bound_path.parent, 0o700)
        self.bound_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.bound_path))
        os.chmod(self.bound_path, 0o600)
        listener.listen(8)
        listener.settimeout(0.25)
        self._run_counter += 1
        run = _ControlServerRun(
            self._run_counter,
            listener,
            # A `BoundedSemaphore` rather than a plain `Semaphore`: an accidental extra
            # `.release()` (a real bug class) raises immediately instead of silently
            # letting the pool admit more than `max_concurrent_requests` callers.
            threading.BoundedSemaphore(self._max_concurrent_requests),
        )
        self._run = run
        # Captured once, directly, rather than read through `run.queue` on every
        # iteration: a test that needs to pin the exact "admitted but not yet
        # enqueued" window replaces `run.queue` with a wrapper to intercept what
        # the accept loop and `close()` see. Workers must keep servicing the one
        # real queue regardless of that swap, or such a wrapper would have to
        # reimplement the full blocking `get()` protocol just to stay a passive
        # observer of `put()`.
        work_queue = run.queue

        def worker_loop() -> None:
            while True:
                item = work_queue.get()
                if item is None:
                    return
                fn, connection = item
                try:
                    fn(connection)
                except Exception as exc:
                    # `handle()` below already catches `Exception` internally and
                    # always releases its permit / decrements in_flight in its own
                    # `finally`, so reaching here means something unexpected failed
                    # outside that boundary. Letting it propagate would silently
                    # kill this worker thread -- permanently shrinking the pool's
                    # real capacity by one with no observable sign. Recording it
                    # here means `is_serving()`/`serving_diagnostic()` can see the
                    # worker is gone instead of the pool just quietly degrading
                    # (#448 Slice 1 worker-pool observability). This worker does not
                    # loop back for more work afterward: whatever broke was outside
                    # `handle()`'s own exhaustive `except`/`finally`, so this thread's
                    # state is no longer trustworthy enough to keep reusing.
                    run.stopped_reason = (
                        f"control worker died unexpectedly: {type(exc).__name__}: {exc}"
                    )
                    return

        run.workers = [
            threading.Thread(
                target=worker_loop,
                name=f"repoforge-control-worker-{index}",
                daemon=True,
            )
            for index in range(self._max_concurrent_requests)
        ]
        for worker in run.workers:
            worker.start()

        def deny(connection: socket.socket) -> None:
            connection.sendall(
                _encode(
                    ControlResponse(
                        _PROTOCOL,
                        False,
                        "unknown",
                        "denied",
                        error_code="PEER_NOT_ALLOWED",
                        message="Control peer does not own the runtime",
                    )
                )
            )
            _drain_before_close(connection)

        def exchange(connection: socket.socket) -> None:
            """Read one request and answer it, bounded in time and in bytes."""
            # A deadline is mandatory, not defensive: `accept()` returns a socket in
            # BLOCKING mode regardless of the listener's timeout, so without this a client
            # that connects and stays silent holds this loop -- and therefore the entire
            # control plane -- forever (#322).
            connection.settimeout(_CLIENT_TIMEOUT_SECONDS)
            if _peer_uid(connection) != os.getuid():
                deny(connection)
                return
            chunks = bytearray()
            while b"\n" not in chunks and len(chunks) <= _MAX_MESSAGE:
                block = connection.recv(4096)
                if not block:
                    break
                chunks.extend(block)
            try:
                request = _decode_request(bytes(chunks).split(b"\n", 1)[0])
                response = handler(request)
            except Exception as exc:
                response = ControlResponse(
                    _PROTOCOL,
                    False,
                    "unknown",
                    "failed",
                    error_code=type(exc).__name__,
                    message=redact_text(str(exc)),
                )
            connection.sendall(_encode(response))

        def handle(connection: socket.socket) -> None:
            """Run one exchange on a bounded worker thread, never on the accept loop.

            #448 Signature C: a handler here used to run inline on the single accept
            thread, so one expensive or slow request blocked every other caller behind
            it in the kernel backlog. This now runs on one of `_max_concurrent_requests`
            bounded worker threads, admitted only once a semaphore slot was acquired;
            the slot is always released here, whether the exchange succeeded, raised, or
            the peer disconnected mid-read.
            """
            with run.active_connections_lock:
                run.active_connections.add(connection)
            try:
                with connection:
                    exchange(connection)
            except Exception:
                self._client_failures += 1
            finally:
                with run.active_connections_lock:
                    run.active_connections.discard(connection)
                run.admission.release()
                with run.in_flight_lock:
                    run.in_flight -= 1

        def reject_overloaded(connection: socket.socket) -> None:
            """Answer immediately when at capacity, rather than queueing the peer.

            This runs directly on the accept thread: it is bounded (a settimeout,
            one send, one close) and never runs the real handler, so it cannot itself
            become the thing that blocks the accept loop.
            """
            try:
                connection.settimeout(_ACCEPT_THREAD_REJECTION_TIMEOUT_SECONDS)
                with run.in_flight_lock:
                    in_flight = run.in_flight
                connection.sendall(
                    _encode(
                        ControlResponse(
                            _PROTOCOL,
                            False,
                            "unknown",
                            "overloaded",
                            payload=(
                                ("capacity", self._max_concurrent_requests),
                                ("in_flight", in_flight),
                                ("retryable", True),
                            ),
                            error_code="RUNTIME_CONTROL_OVERLOADED",
                            message=(
                                f"control socket at capacity "
                                f"({self._max_concurrent_requests} requests in flight); "
                                "retry shortly"
                            ),
                        )
                    )
                )
                # Drain whatever the caller already sent before closing: this handler
                # never reads the caller's request at all, and closing immediately
                # after responding raced a caller whose own request write was still
                # in flight often enough in practice to be a real bug, not a
                # hypothetical -- it turned their well-formed write into a broken
                # pipe purely from this end's own timing (#448 Slice 1).
                _drain_before_close(connection)
            except Exception:
                # Never allowed to propagate: this runs on the accept thread itself, so
                # any failure here (a reset peer, an encoding bug) must not kill the loop
                # that is supposed to keep serving everyone else (#322's own precedent).
                pass
            finally:
                connection.close()

        def serve() -> None:
            while not run.stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError as exc:
                    # Recorded, not swallowed: this ends the control plane, and a runtime
                    # whose control plane has ended must be able to say so (#322).
                    if not run.stop.is_set():
                        run.stopped_reason = f"accept failed: {type(exc).__name__}"
                    break
                # NOTHING a peer does may end this loop. The previous version let
                # `sendall` to a closed peer raise out of `serve`, which killed the thread
                # while the process kept running and the watchdog kept recording `healthy`
                # -- so the control plane was gone with every durable fact still claiming
                # otherwise. A dropped connection costs that one exchange, nothing more.
                if not run.admission.acquire(blocking=False):
                    reject_overloaded(connection)
                    continue
                with run.in_flight_lock:
                    run.in_flight += 1
                if run.stop.is_set():
                    # close() raced this accept and has already started signaling
                    # workers to stop: do not enqueue -- release the slot and close
                    # the connection ourselves rather than risking a work item no
                    # worker will ever pick up. `close()` also drains the queue
                    # itself as a final safety net for the residual window between
                    # this check and a `put()` that lands anyway.
                    run.admission.release()
                    with run.in_flight_lock:
                        run.in_flight -= 1
                    connection.close()
                else:
                    run.queue.put((handle, connection))

        run.accept_thread = threading.Thread(
            target=serve, name="repoforge-runtime-control", daemon=True
        )
        run.accept_thread.start()

    def close(self) -> None:
        """Stop accepting, wake in-flight handlers, and drain within a bound.

        Idempotent: a second call is a no-op, matching `start()`'s own "already
        started" guard giving both lifecycle methods clear, non-surprising semantics
        (#448 Slice 1 edge case E).
        """
        if self._closed:
            return
        self._closed = True
        run = self._run
        if run is None:
            return
        run.stop.set()
        # One overall deadline for the whole drain sequence below (accept thread,
        # then workers) rather than a separate hardcoded budget for each: the
        # accept thread must fully finish -- including whatever `put()` its very
        # last admitted iteration might still be about to make -- *before* this
        # method sends worker stop-sentinels and does its one queue-drain pass, or
        # a late `put()` from a straggling accept thread could land after that
        # drain already ran and be silently unpickable-up (#448 Slice 1 edge case
        # A). Giving it the full budget here, not a short fixed one, makes that
        # ordering hold for everything but a genuinely hung accept thread, which
        # `is_serving()`/`serving_diagnostic()` would then surface on its own.
        deadline = time.monotonic() + self._drain_timeout
        run.listener.close()
        if run.accept_thread is not None:
            run.accept_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # Deliberately does NOT force-shutdown in-flight connections' sockets here.
        # An earlier version of this did, reasoning it would wake a handler blocked on
        # socket I/O -- but a handler blocked on *its own connection's* recv/sendall is
        # already bounded by `_CLIENT_TIMEOUT_SECONDS`, so that case never needed help;
        # forcing the socket shut instead broke the common, legitimate case of a
        # handler that is merely slow and about to finish normally, severing its
        # connection out from under it and turning a real (if late) response into a
        # broken pipe (caught by this refactor's own test suite). The actual risk this
        # bound has to cover is the *handler callback itself* hanging in non-socket
        # code, which no socket operation can unblock in the first place -- Python has
        # no safe way to force-terminate a running thread.
        #
        # So there are two separate guarantees, not one (#448 Slice 1 edge case
        # B/process, found via a real subprocess reproduction: `close()` returning
        # within its bound did NOT mean the process could exit -- it hung for 15s+
        # past that on a stuck handler, because `ThreadPoolExecutor`'s non-daemon
        # workers block CPython's own `atexit` shutdown hook regardless of whether
        # `shutdown()` was ever called). Contract A ("this call returns within a
        # bound") is met by joining workers up to `self._drain_timeout` below.
        # Contract B ("the process can still exit") is met structurally: every
        # worker is a **daemon** thread (see `start()`), so a permanently stuck one
        # never blocks interpreter shutdown, regardless of how this method behaves.
        for _ in run.workers:
            run.queue.put(None)
        # Reuses the single deadline computed above (shared with the accept-thread
        # join), not a fresh one -- see that comment for why the ordering matters.
        for worker in run.workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        still_alive = [worker for worker in run.workers if worker.is_alive()]
        if still_alive:
            run.stopped_reason = (
                f"{len(still_alive)} in-flight control handler(s) did not terminate "
                f"within {self._drain_timeout}s of shutdown"
            )
        # Safety net for the residual race between the accept loop's `stop.is_set()`
        # check and its `put()`: a real work item can still be enqueued after the
        # check but before this loop, and must not leak its permit and connection.
        #
        # A worker that is still alive here (join() above timed out on a
        # genuinely stuck handler) has NOT yet consumed its stop-sentinel --
        # that `None` is what will eventually let it exit once its handler
        # returns, however long that takes. Draining it away here like any
        # other leftover item would strand that worker blocked on `get()`
        # forever, since nothing else ever sends it another sentinel. Sentinels
        # are counted and put back after real work items are cleaned up, so
        # every still-busy worker still gets exactly the one it's owed.
        pending_sentinels = 0
        while True:
            try:
                item = run.queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                pending_sentinels += 1
                continue
            _fn, connection = item
            with contextlib.suppress(OSError):
                connection.close()
            run.admission.release()
            with run.in_flight_lock:
                run.in_flight -= 1
        for _ in range(pending_sentinels):
            run.queue.put(None)
        self.bound_path.unlink(missing_ok=True)
        self._run = None


class UnixRuntimeControlClient:
    def __init__(self, path: Path):
        self.path = path.expanduser().absolute()
        self._bound_path = resolve_unix_socket_path(self.path)

    def request(self, request: ControlRequest, *, timeout_seconds: float = 10.0) -> ControlResponse:
        payload = {
            "protocol_version": request.protocol_version,
            "command": request.command.value,
            "correlation_id": request.correlation_id,
            "payload": dict(request.payload),
        }
        data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(data) > _MAX_MESSAGE:
            raise ConfigError("Runtime control request is too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            try:
                connection.connect(str(self._bound_path))
            except OSError as exc:
                raise ConfigError(f"RUNTIME_CONTROL_UNAVAILABLE: {exc}") from exc
            connection.sendall(data)
            chunks = bytearray()
            while b"\n" not in chunks and len(chunks) <= _MAX_MESSAGE:
                block = connection.recv(4096)
                if not block:
                    break
                chunks.extend(block)
        try:
            raw: Any = json.loads(bytes(chunks).split(b"\n", 1)[0].decode())
            return ControlResponse(
                protocol_version=int(raw["protocol_version"]),
                ok=bool(raw["ok"]),
                correlation_id=str(raw["correlation_id"]),
                status=str(raw["status"]),
                payload=tuple(sorted((raw.get("payload") or {}).items())),
                error_code=str(raw["error_code"]) if raw.get("error_code") else None,
                message=str(raw["message"]) if raw.get("message") else None,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"Malformed runtime control response: {exc}") from exc
