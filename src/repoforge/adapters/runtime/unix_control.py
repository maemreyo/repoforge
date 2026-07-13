"""Versioned, allowlisted, owner-only Unix-domain runtime control protocol."""

from __future__ import annotations

import json
import os
import socket
import struct
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


def _peer_uid(connection: socket.socket) -> int | None:
    if hasattr(socket, "SO_PEERCRED"):
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _, uid, _ = struct.unpack("3i", credentials)
            return int(uid)
        except OSError:
            return None
    getpeereid = getattr(connection, "getpeereid", None)
    if getpeereid is not None:
        try:
            uid, _ = getpeereid()
            return int(uid)
        except OSError:
            return None
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
        self.path = path
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, handler: Callable[[ControlRequest], ControlResponse]) -> None:
        if self._socket is not None:
            raise ConfigError("Runtime control server is already started")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
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
        self.path.unlink(missing_ok=True)
        self._socket = None
        self._thread = None


class UnixRuntimeControlClient:
    def __init__(self, path: Path):
        self.path = path

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
                connection.connect(str(self.path))
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
