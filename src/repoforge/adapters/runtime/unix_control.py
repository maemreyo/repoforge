"""Versioned, allowlisted, owner-only Unix-domain runtime control protocol."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import struct
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
    ):
        if max_concurrent_requests <= 0:
            raise ConfigError("max_concurrent_requests must be positive")
        self.path = path.expanduser().absolute()
        self._bound_path = resolve_unix_socket_path(self.path)
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client_failures = 0
        self._max_concurrent_requests = max_concurrent_requests
        self._admission = threading.Semaphore(max_concurrent_requests)
        self._executor: ThreadPoolExecutor | None = None
        self._in_flight_lock = threading.Lock()
        self._in_flight = 0
        self._stopped_reason: str | None = None

    @property
    def bound_path(self) -> Path:
        return self._bound_path

    def is_serving(self) -> bool:
        """Is the accept loop still running?

        The health record is written by a different loop than the one that serves control
        requests, so without this a runtime whose control plane had ended kept reporting
        `phase: healthy` -- every durable fact insisting nothing was wrong while every read
        that needed the socket timed out. That gap cost more diagnosis time than the
        original defect did.
        """
        return bool(
            self._socket is not None
            and self._thread is not None
            and self._thread.is_alive()
            and not self._stop.is_set()
        )

    def serving_diagnostic(self) -> str:
        """Why the control plane is not serving, for the health check's detail."""
        if self.is_serving():
            return "control socket is accepting requests"
        if self._socket is None:
            return "control server was never started"
        if self._stop.is_set():
            return "control server was asked to stop"
        return self._stopped_reason or "control server thread is no longer running"

    def start(self, handler: Callable[[ControlRequest], ControlResponse]) -> None:
        if self._socket is not None:
            raise ConfigError("Runtime control server is already started")
        self.bound_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.bound_path.parent, 0o700)
        self.bound_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.bound_path))
        os.chmod(self.bound_path, 0o600)
        listener.listen(8)
        listener.settimeout(0.25)
        self._socket = listener
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent_requests,
            thread_name_prefix="repoforge-control-worker",
        )

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
            try:
                with connection:
                    exchange(connection)
            except Exception:
                self._client_failures += 1
            finally:
                self._admission.release()
                with self._in_flight_lock:
                    self._in_flight -= 1

        def reject_overloaded(connection: socket.socket) -> None:
            """Answer immediately when at capacity, rather than queueing the peer.

            This runs directly on the accept thread: it is bounded (a settimeout,
            one send, one close) and never runs the real handler, so it cannot itself
            become the thing that blocks the accept loop.
            """
            try:
                connection.settimeout(_CLIENT_TIMEOUT_SECONDS)
                with self._in_flight_lock:
                    in_flight = self._in_flight
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
            except OSError:
                pass
            finally:
                connection.close()

        def serve() -> None:
            while not self._stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError as exc:
                    # Recorded, not swallowed: this ends the control plane, and a runtime
                    # whose control plane has ended must be able to say so (#322).
                    if not self._stop.is_set():
                        self._stopped_reason = f"accept failed: {type(exc).__name__}"
                    break
                # NOTHING a peer does may end this loop. The previous version let
                # `sendall` to a closed peer raise out of `serve`, which killed the thread
                # while the process kept running and the watchdog kept recording `healthy`
                # -- so the control plane was gone with every durable fact still claiming
                # otherwise. A dropped connection costs that one exchange, nothing more.
                if not self._admission.acquire(blocking=False):
                    reject_overloaded(connection)
                    continue
                with self._in_flight_lock:
                    self._in_flight += 1
                assert self._executor is not None
                try:
                    self._executor.submit(handle, connection)
                except RuntimeError:
                    # Executor is shutting down (close() raced this accept): the slot
                    # was never handed to a worker, so release it and close the
                    # connection ourselves rather than leaking either.
                    self._admission.release()
                    with self._in_flight_lock:
                        self._in_flight -= 1
                    connection.close()

        self._thread = threading.Thread(target=serve, name="repoforge-runtime-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        # Bounded, not abandoned: every in-flight handler is itself bounded by
        # `_CLIENT_TIMEOUT_SECONDS`, so waiting for the pool to drain here cannot hang
        # indefinitely, and it is what actually proves no worker thread leaks past
        # `close()` (#448 Slice 1) rather than merely stopping new admission.
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        self.bound_path.unlink(missing_ok=True)
        self._socket = None
        self._thread = None
        self._executor = None


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
