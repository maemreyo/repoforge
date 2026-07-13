"""Bounded tunnel-client adapter with process-group ownership."""

from __future__ import annotations

import codecs
import contextlib
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.redaction import redact_text
from ...domain.runtime import ChildProcess, TunnelProfile
from .state_store import process_identity

_STREAM_BUFFER_LIMIT = 64 * 1024
_LOG_PUMP_FINALIZE_TIMEOUT_SECONDS = 30.0


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

    def _append_log(self, log_path: Path, text: str, *, secrets: tuple[str, ...]) -> None:
        redacted = redact_text(
            text,
            secrets=secrets,
            limit=max(64, min(8_000, self.log_max_bytes)),
        )
        encoded = redacted.encode("utf-8", errors="replace")
        if len(encoded) > self.log_max_bytes:
            marker = f"<runtime log event omitted: {len(encoded)} bytes>\n".encode()
            encoded = marker[: self.log_max_bytes]
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

    def _pump_output(
        self,
        process: subprocess.Popen[bytes],
        log_path: Path,
        *,
        secrets: tuple[str, ...],
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
                        self._append_log(
                            log_path,
                            f"<runtime log line omitted: {len(line)} characters>\n",
                            secrets=secrets,
                        )
                    else:
                        self._append_log(log_path, line + "\n", secrets=secrets)
                if len(pending) > _STREAM_BUFFER_LIMIT:
                    self._append_log(
                        log_path,
                        f"<runtime log line omitted: more than {_STREAM_BUFFER_LIMIT} characters>\n",
                        secrets=secrets,
                    )
                    pending = ""
                    discarding_oversized_line = True
            pending += decoder.decode(b"", final=True)
            if pending and not discarding_oversized_line:
                self._append_log(log_path, pending, secrets=secrets)
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

    def start(self, profile: TunnelProfile, *, env: dict[str, str], log_path: Path) -> ChildProcess:
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
            kwargs={"secrets": secrets},
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
                return process_identity(child.pid) == child.process_identity
            self._finalize_child(child.pid)
            return False
        return process_identity(child.pid) == child.process_identity

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None:
        if process_identity(child.pid) != child.process_identity:
            self._finalize_child(child.pid)
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            self._finalize_child(child.pid)
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not self.is_alive(child):
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        process = self._children.get(child.pid)
        if process is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        self._finalize_child(child.pid)
