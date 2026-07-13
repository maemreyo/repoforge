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
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import ControlCommand, ControlRequest, ControlResponse

_MAX_MESSAGE = 64 * 1024
_PROTOCOL = 1
_MAX_SOCKET_PATH_BYTES = 100


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
    def __init__(self, path: Path):
        self.path = path.expanduser().absolute()
        self._bound_path = resolve_unix_socket_path(self.path)
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def bound_path(self) -> Path:
        return self._bound_path

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

        def serve() -> None:
            while not self._stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with connection:
                    peer = _peer_uid(connection)
                    if peer != os.getuid():
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
                        continue
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

        self._thread = threading.Thread(target=serve, name="repoforge-runtime-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.bound_path.unlink(missing_ok=True)
        self._socket = None
        self._thread = None


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
