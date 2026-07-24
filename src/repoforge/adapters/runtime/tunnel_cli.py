"""Bounded tunnel-client adapter with process-group ownership."""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import ChildProcess, HealthCheck, TunnelProfile
from ...domain.runtime_events import RuntimeEventV1, encode_runtime_event
from .state_store import process_identity

_STREAM_BUFFER_LIMIT = 64 * 1024
_LOG_PUMP_FINALIZE_TIMEOUT_SECONDS = 30.0
_HEALTH_RESPONSE_LIMIT = 64 * 1024
_RESPONSE_FAILURE_THRESHOLD = 2


class TunnelCliClient:
    def __init__(
        self,
        executable: str,
        *,
        default_timeout_seconds: int = 60,
        log_max_bytes: int = 5_000_000,
        log_backup_count: int = 3,
    ):
        self.executable = executable
        self.default_timeout_seconds = default_timeout_seconds
        self.log_max_bytes = max(1, log_max_bytes)
        self.log_backup_count = max(1, log_backup_count)
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._log_threads: dict[int, threading.Thread] = {}
        self._log_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._health_urls: dict[int, str] = {}
        self._response_failures: dict[int, tuple[int, float, str]] = {}
        self._response_success_at: dict[int, float] = {}

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _rotate_log(self, log_path: Path, incoming_bytes: int = 0) -> None:
        if not log_path.is_file() or log_path.stat().st_size + incoming_bytes <= self.log_max_bytes:
            return
        log_path.with_suffix(log_path.suffix + f".{self.log_backup_count}").unlink(missing_ok=True)
        for index in range(self.log_backup_count - 1, 0, -1):
            source = log_path.with_suffix(log_path.suffix + f".{index}")
            if source.exists():
                os.replace(source, log_path.with_suffix(log_path.suffix + f".{index + 1}"))
        os.replace(log_path, log_path.with_suffix(log_path.suffix + ".1"))
        self._fsync_dir(log_path.parent)

    def _append_runtime_event(
        self,
        log_path: Path,
        event: RuntimeEventV1,
        *,
        secrets: tuple[str, ...],
    ) -> None:
        redacted_message = redact_text(
            event.message,
            secrets=secrets,
            limit=max(64, min(8_000, self.log_max_bytes)),
        )
        safe_event = replace(event, message=redacted_message)
        encoded = (encode_runtime_event(safe_event) + "\n").encode("utf-8", errors="replace")
        if len(encoded) > self.log_max_bytes:
            omitted = replace(
                safe_event,
                level="WARNING",
                event_kind="oversized_event",
                message=f"runtime event omitted because it encoded to {len(encoded)} bytes",
                action=None,
                duration_ms=None,
            )
            encoded = (encode_runtime_event(omitted) + "\n").encode("utf-8", errors="replace")
        with self._log_lock:
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(log_path.parent, 0o700)
            self._rotate_log(log_path, len(encoded))
            existed = log_path.exists()
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "ab", buffering=0) as handle:
                handle.write(encoded)
                os.fsync(handle.fileno())
            os.chmod(log_path, 0o600)
            if not existed:
                self._fsync_dir(log_path.parent)

    def _observe_log_line(self, pid: int, line: str) -> None:
        """Track secret-free health signals already emitted by tunnel-client."""
        message = line
        health_url: str | None = None
        status: object = None
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            raw_message = payload.get("msg")
            if isinstance(raw_message, str):
                message = raw_message
            raw_url = payload.get("health_url")
            if isinstance(raw_url, str) and raw_url.startswith("http://127.0.0.1:"):
                health_url = raw_url
            status = payload.get("status")
        lowered = message.lower()
        now = time.monotonic()
        with self._health_lock:
            if health_url is not None:
                self._health_urls[pid] = health_url
            if "dispatcher acknowledged notification with control plane" in lowered:
                self._response_success_at[pid] = now
                self._response_failures.pop(pid, None)
                return
            failure = (
                "failed to post" in lowered
                or "failed to process polled command" in lowered
                or "context canceled" in lowered
                or status in {502, 503, 504}
                or any(f" {code} " in f" {lowered} " for code in ("502", "503", "504"))
            )
            if failure:
                count, _, _ = self._response_failures.get(pid, (0, now, ""))
                detail = redact_text(message, limit=500)
                if status in {502, 503, 504} and str(status) not in detail:
                    detail = f"HTTP {status}: {detail}"
                self._response_failures[pid] = (count + 1, now, detail)

    @staticmethod
    def _runtime_event_from_line(
        line: str,
        *,
        correlation_id: str | None,
    ) -> RuntimeEventV1:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = None

        if not isinstance(payload, dict):
            return RuntimeEventV1(
                observed_at=datetime.now(timezone.utc).isoformat(),
                component="tunnel_client",
                stream="combined",
                level="INFO",
                event_kind="process_output",
                message=line,
                correlation_id=correlation_id[:160] if correlation_id else None,
            )

        def bounded_text(key: str, default: str, limit: int) -> str:
            value = payload.get(key)
            return value[:limit] if isinstance(value, str) and value else default

        raw_message = payload.get("message")
        if not isinstance(raw_message, str):
            raw_message = payload.get("msg")
        message = raw_message if isinstance(raw_message, str) else line
        raw_action = payload.get("action")
        action = raw_action[:160] if isinstance(raw_action, str) and raw_action else None
        raw_duration = payload.get("duration_ms")
        duration_ms = (
            float(raw_duration)
            if isinstance(raw_duration, (int, float))
            and not isinstance(raw_duration, bool)
            and raw_duration >= 0
            else None
        )
        return RuntimeEventV1(
            observed_at=datetime.now(timezone.utc).isoformat(),
            component=bounded_text("component", "tunnel_client", 160),
            stream="combined",
            level=bounded_text("level", "INFO", 30),
            event_kind=bounded_text("event_kind", "tunnel_event", 160),
            message=message,
            action=action,
            duration_ms=duration_ms,
            correlation_id=correlation_id[:160] if correlation_id else None,
        )

    def _pump_output(
        self,
        process: subprocess.Popen[bytes],
        log_path: Path,
        *,
        secrets: tuple[str, ...],
        correlation_id: str | None,
    ) -> None:
        stream = process.stdout
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        discarding_oversized_line = False
        try:
            while True:
                block = stream.read(4096)
                if not block:
                    break
                decoded = decoder.decode(block)
                if discarding_oversized_line:
                    newline = decoded.find("\n")
                    if newline < 0:
                        continue
                    decoded = decoded[newline + 1 :]
                    discarding_oversized_line = False
                pending += decoded
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if len(line) > _STREAM_BUFFER_LIMIT:
                        self._append_runtime_event(
                            log_path,
                            RuntimeEventV1(
                                observed_at=datetime.now(timezone.utc).isoformat(),
                                component="tunnel_client",
                                stream="combined",
                                level="WARNING",
                                event_kind="oversized_line",
                                message=f"runtime log line omitted: {len(line)} characters",
                                correlation_id=correlation_id,
                            ),
                            secrets=secrets,
                        )
                    else:
                        self._observe_log_line(process.pid, line)
                        self._append_runtime_event(
                            log_path,
                            self._runtime_event_from_line(
                                line,
                                correlation_id=correlation_id,
                            ),
                            secrets=secrets,
                        )
                if len(pending) > _STREAM_BUFFER_LIMIT:
                    self._append_runtime_event(
                        log_path,
                        RuntimeEventV1(
                            observed_at=datetime.now(timezone.utc).isoformat(),
                            component="tunnel_client",
                            stream="combined",
                            level="WARNING",
                            event_kind="oversized_line",
                            message=(
                                "runtime log line omitted: more than "
                                f"{_STREAM_BUFFER_LIMIT} characters"
                            ),
                            correlation_id=correlation_id,
                        ),
                        secrets=secrets,
                    )
                    pending = ""
                    discarding_oversized_line = True
            pending += decoder.decode(b"", final=True)
            if pending and not discarding_oversized_line:
                self._observe_log_line(process.pid, pending)
                self._append_runtime_event(
                    log_path,
                    self._runtime_event_from_line(
                        pending,
                        correlation_id=correlation_id,
                    ),
                    secrets=secrets,
                )
        finally:
            stream.close()

    def _finalize_child(self, pid: int) -> None:
        thread = self._log_threads.get(pid)
        if thread is not None and thread is not threading.current_thread():
            # Once the child has exited, its pipe will reach EOF. Do not report lifecycle completion
            # until the bounded log pump has consumed that EOF and persisted its final marker.
            thread.join(timeout=_LOG_PUMP_FINALIZE_TIMEOUT_SECONDS)
            if thread.is_alive():
                # A descendant may still own the inherited pipe. Preserve tracking and retry the
                # finalization later rather than falsely reporting a fully drained child.
                return
        self._log_threads.pop(pid, None)
        self._children.pop(pid, None)
        with self._health_lock:
            self._health_urls.pop(pid, None)
            self._response_failures.pop(pid, None)
            self._response_success_at.pop(pid, None)

    @staticmethod
    def _run(argv: list[str], *, env: dict[str, str], timeout: int) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                argv, env=env, capture_output=True, check=False, timeout=timeout
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(f"Tunnel command failed to execute: {exc}") from exc
        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        redacted = redact_text(output, secrets=(env.get("CONTROL_PLANE_API_KEY", ""),))
        if completed.returncode != 0:
            raise ConfigError(
                f"Tunnel command failed with exit code {completed.returncode}: {redacted}"
            )
        return completed.returncode, redacted

    def executable_version(self) -> str:
        try:
            completed = subprocess.run(
                [self.executable, "--version"], capture_output=True, check=False, timeout=10
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(f"Cannot inspect tunnel-client version: {exc}") from exc
        if completed.returncode != 0:
            raise ConfigError(
                "Cannot inspect tunnel-client version: "
                + redact_text(
                    (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
                )
            )
        return redact_text(
            (completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip()
        )

    def initialize(self, profile: TunnelProfile, *, env: dict[str, str]) -> None:
        tunnel_id = env.get("REPOFORGE_TUNNEL_ID")
        if not tunnel_id:
            raise ConfigError("Tunnel id is available only in the activation environment")
        self._run(
            [
                self.executable,
                "init",
                "--force",
                "--sample",
                "sample_mcp_stdio_local",
                "--profile",
                profile.profile,
                "--tunnel-id",
                tunnel_id,
                "--mcp-command",
                shlex.join(profile.mcp_argv),
            ],
            env=env,
            timeout=self.default_timeout_seconds,
        )

    def doctor(self, profile: TunnelProfile, *, env: dict[str, str]) -> tuple[bool, str]:
        try:
            _, output = self._run(
                [self.executable, "doctor", "--profile", profile.profile, "--explain"],
                env=env,
                timeout=self.default_timeout_seconds,
            )
            return True, output[-8000:]
        except ConfigError as exc:
            return False, str(exc)

    def start(
        self,
        profile: TunnelProfile,
        *,
        env: dict[str, str],
        log_path: Path,
        correlation_id: str | None = None,
    ) -> ChildProcess:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(log_path.parent, 0o700)
        with self._log_lock:
            self._rotate_log(log_path)
        try:
            process = subprocess.Popen(
                [self.executable, "run", "--profile", profile.profile],
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise ConfigError(f"Tunnel runtime failed to start: {exc}") from exc
        identity = process_identity(process.pid)
        if identity is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            raise ConfigError("Cannot bind tunnel child process identity")
        self._children[process.pid] = process
        secrets = tuple(value for value in (env.get("CONTROL_PLANE_API_KEY", ""),) if value)
        thread = threading.Thread(
            target=self._pump_output,
            args=(process, log_path),
            kwargs={"secrets": secrets, "correlation_id": correlation_id},
            name=f"repoforge-tunnel-log-{process.pid}",
            daemon=True,
        )
        self._log_threads[process.pid] = thread
        thread.start()
        return ChildProcess(process.pid, identity, str(time.time_ns()))

    def is_alive(self, child: ChildProcess) -> bool:
        process = self._children.get(child.pid)
        if process is not None:
            if process.poll() is None:
                observed_identity = process_identity(child.pid)
                if observed_identity == child.process_identity:
                    return True
                if observed_identity is None:
                    # The owned Popen handle is authoritative while its child has not been reaped.
                    # Identity inspection can temporarily lose the process during a fast exit on
                    # Darwin; recheck before deciding whether lifecycle finalization is required.
                    if process.poll() is None:
                        return True
                elif process.poll() is None:
                    # Identity mismatch while poll() says child is alive.  On Darwin a
                    # short-lived zombie (<defunct>) can briefly occupy the PID while
                    # the owned Popen handle is not yet reaped.  Recheck Popen before
                    # treating the child as gone.
                    return True
            self._finalize_child(child.pid)
            return child.pid in self._children
        return process_identity(child.pid) == child.process_identity

    def health(self, child: ChildProcess, *, timeout_seconds: float) -> tuple[HealthCheck, ...]:
        child_alive = self.is_alive(child)
        child_check = HealthCheck(
            "tunnel_child",
            child_alive,
            "managed child process is alive" if child_alive else "managed child process exited",
        )
        if not child_alive:
            return (
                child_check,
                HealthCheck("tunnel_admin", False, "admin health endpoint is unavailable"),
                HealthCheck("control_plane_response", False, "tunnel process is not running"),
            )

        with self._health_lock:
            health_url = self._health_urls.get(child.pid)
            failure = self._response_failures.get(child.pid)
            success_at = self._response_success_at.get(child.pid, 0.0)
        if health_url is None:
            admin_check = HealthCheck(
                "tunnel_admin",
                True,
                "tunnel-client has not advertised an admin health endpoint yet",
            )
        else:
            try:
                request = urllib.request.Request(health_url, method="GET")
                with urllib.request.urlopen(
                    request, timeout=max(0.01, timeout_seconds)
                ) as response:
                    body = response.read(_HEALTH_RESPONSE_LIMIT + 1)
                    status_code = int(getattr(response, "status", 200))
                admin_ok = 200 <= status_code < 400 and len(body) <= _HEALTH_RESPONSE_LIMIT
                admin_detail = (
                    f"admin endpoint returned HTTP {status_code}"
                    if len(body) <= _HEALTH_RESPONSE_LIMIT
                    else "admin endpoint response exceeded the bounded health payload"
                )
            except (OSError, ValueError, urllib.error.URLError) as exc:
                admin_ok = False
                admin_detail = redact_text(
                    f"admin health probe failed: {type(exc).__name__}: {exc}"
                )
            admin_check = HealthCheck("tunnel_admin", admin_ok, admin_detail)

        response_ok = True
        response_detail = "no unresolved control-plane response failures"
        if failure is not None:
            count, failed_at, detail = failure
            if count >= _RESPONSE_FAILURE_THRESHOLD and failed_at >= success_at:
                response_ok = False
                response_detail = f"{count} consecutive response-path failures; latest: {detail}"
            else:
                response_detail = f"transient response-path failure observed: {detail}"
        return (
            child_check,
            admin_check,
            HealthCheck("control_plane_response", response_ok, response_detail),
        )

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None:
        process = self._children.get(child.pid)
        if process is not None:
            if process.poll() is not None:
                self._finalize_child(child.pid)
                return
            # The owned Popen handle is authoritative while its child is alive. A shebang exec can
            # temporarily change the process facts used for identity hashing, especially on Darwin.
        elif process_identity(child.pid) != child.process_identity:
            self._finalize_child(child.pid)
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            # Darwin can report EPERM instead of ESRCH for a process group that
            # has already been reaped; both mean there is nothing left to signal.
            self._finalize_child(child.pid)
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not self.is_alive(child):
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)
        process = self._children.get(child.pid)
        if process is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        self._finalize_child(child.pid)
