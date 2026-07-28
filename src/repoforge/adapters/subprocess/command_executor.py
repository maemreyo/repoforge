"""Bounded subprocess adapter with process-group timeout cleanup."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...config import ServerConfig
from ...domain.errors import CommandError, ErrorCode
from ...domain.redaction import redact_text
from ...domain.repository_auth_broker import EphemeralSecret
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from .process_tree import inspect_descendants, kill_identity, wait_identities_gone

_MAX_FAILED_SELECTORS = 100
_MAX_FAILURE_OUTPUT_ARTIFACT_BYTES = 10 * 1024 * 1024
_PYTEST_LEADING_SELECTOR = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<selector>[^\s]+(?:\:\:[^\s]+)*)",
    re.MULTILINE,
)
_PYTEST_TRAILING_SELECTOR = re.compile(
    r"^(?P<selector>[^\s]+\:\:[^\s]+)\s+(?:FAILED|ERROR)\b",
    re.MULTILINE,
)


def _failed_selectors(output: str) -> tuple[str, ...]:
    selectors: list[str] = []
    seen: set[str] = set()
    for pattern in (_PYTEST_LEADING_SELECTOR, _PYTEST_TRAILING_SELECTOR):
        for match in pattern.finditer(output):
            selector = match.group("selector").replace("\\", "/").rstrip(":")
            if not selector or selector in seen:
                continue
            seen.add(selector)
            selectors.append(selector)
            if len(selectors) >= _MAX_FAILED_SELECTORS:
                return tuple(selectors)
    return tuple(selectors)


class SubprocessCommandExecutor:
    def __init__(self, config: ServerConfig):
        self.config = config

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in self.config.allowed_environment if k in os.environ}
        inherited = env.get("PATH", "")
        parts = [*self.config.path_prefixes]
        if inherited:
            parts.append(inherited)
        env["PATH"] = os.pathsep.join(dict.fromkeys(p for p in parts if p))
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GH_PROMPT_DISABLED"] = "1"
        if extra:
            env.update(extra)
        return env

    @staticmethod
    def _truncate(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        half = max(1, limit // 2)
        removed = len(text) - half * 2
        return (
            f"{text[:half]}\n\n... <{removed} characters omitted> ...\n\n{text[-half:]}",
            True,
        )

    def _persist_failure_output(self, stdout: str, stderr: str) -> str | None:
        payload = ("--- stdout ---\n" + stdout + "\n--- stderr ---\n" + stderr).encode(
            "utf-8", errors="replace"
        )
        if not payload or len(payload) > _MAX_FAILURE_OUTPUT_ARTIFACT_BYTES:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        root = self.config.state_root / "failure-output-artifacts"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        target = root / f"{digest}.blob"
        if not target.exists():
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.tmp-", dir=root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            finally:
                temporary.unlink(missing_ok=True)
        return f"failure-output:{digest}"

    def _communicate(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_data: str | bytes | None,
        text: bool,
        timeout: int,
        extra_env: Mapping[str, str] | None,
        exact_env: Mapping[str, str] | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> tuple[subprocess.Popen[Any], tuple[str | bytes, str | bytes]]:
        if exact_env is not None and extra_env is not None:
            raise CommandError("exact_env and extra_env are mutually exclusive")
        environment = dict(exact_env) if exact_env is not None else self.environment(extra_env)
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text,
                encoding="utf-8" if text else None,
                errors="replace" if text else None,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CommandError(
                f"Executable not found: {argv[0]}",
                code=ErrorCode.NOT_FOUND,
                details={"executable": argv[0]},
            ) from exc
        except OSError as exc:
            raise CommandError(
                f"Cannot execute {' '.join(argv)}: {exc}",
                code=ErrorCode.COMMAND_FAILED,
            ) from exc
        if cancel_token is not None:
            cancel_token.bind(process)
        try:
            try:
                return (process, process.communicate(input_data, timeout=timeout))
            except subprocess.TimeoutExpired as exc:
                # Snapshot descendants before sending any kill signal: a child
                # that daemonized a grandchild via its own start_new_session/
                # setsid is still reachable by ppid here as long as it (or
                # something else in the tree) is still alive -- which it is,
                # since that's why the overall command timed out in the first
                # place. Only taken on the timeout path so the common
                # (successful, fast) case never pays for an extra `ps` call.
                pre_snapshot = inspect_descendants(process.pid)
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=2)
                # Repeat bounded discovery after group termination. This can
                # catch a child created after the first snapshot while the
                # root is still attributable; if the root has already exited,
                # the empty complete result records that the rescan happened.
                post_snapshot = inspect_descendants(process.pid)
                # A PermissionError from killpg is treated as "already reaped", but
                # that assumption can be wrong; verify with a bounded drain, and
                # if the process is provably still alive, make one last direct
                # single-process kill() attempt (not the process-group killpg,
                # which may be the thing that kept failing) before giving up --
                # this bounds the caller's wait either way, but still tries hard
                # not to leave the child orphaned.
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        process.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=2)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.communicate(timeout=2)
                # killpg/kill above only reach processes still in this
                # session's process group; a descendant that daemonized with
                # its own start_new_session/setsid escapes that and survives
                # every attempt above untouched. Sweep both bounded snapshots
                # through atomic process handles; this does not depend on
                # group membership and cannot target a reused PID.
                descendants = {
                    (identity.pid, identity.start_token): identity
                    for identity in (
                        *pre_snapshot.identities,
                        *post_snapshot.identities,
                    )
                }
                signalled_identities = tuple(
                    descendant
                    for descendant in descendants.values()
                    if kill_identity(descendant, signal.SIGKILL)
                )
                survivors = wait_identities_gone(signalled_identities)
                raise CommandError(
                    f"Command timed out after {timeout}s: {' '.join(argv)}",
                    code=ErrorCode.COMMAND_TIMEOUT,
                    details={
                        "timeout_seconds": timeout,
                        "descendant_inspection_complete": (
                            pre_snapshot.inspection_complete and post_snapshot.inspection_complete
                        ),
                        "descendant_inspection_status": (
                            f"pre:{pre_snapshot.diagnostic};post:{post_snapshot.diagnostic}"
                        ),
                        "descendant_snapshot_count": len(descendants),
                        "descendant_signal_count": len(signalled_identities),
                        "descendant_survivor_count": len(survivors),
                    },
                ) from exc
        finally:
            if cancel_token is not None:
                cancel_token.release()

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        return self._run_text(
            argv,
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
            check=check,
            extra_env=extra_env,
            output_limit=output_limit,
            cancel_token=cancel_token,
        )

    def run_isolated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        """Run with an exact environment and redact the issued secrets at capture time."""

        raw_secrets = tuple(secret for secret in secrets if secret)
        visible_inputs = (*argv, str(cwd), input_text or "")
        if any(secret in value for secret in raw_secrets for value in visible_inputs):
            raise CommandError(
                "Raw repository-auth material is not allowed in argv, URLs, cwd, or stdin.",
                code=ErrorCode.CREDENTIAL_LEAK_BLOCKED,
                unchanged_state=("No child process was started.",),
            )
        return self._run_text(
            argv,
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
            check=check,
            exact_env=environment,
            secrets=raw_secrets,
            output_limit=output_limit,
            cancel_token=cancel_token,
        )

    def _run_sensitive_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        timeout: int | None,
        max_bytes: int,
        cancel_token: CancellationToken | None,
    ) -> bytes:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise CommandError("Command argv must contain non-empty strings")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 1 <= max_bytes <= 10_000_000
        ):
            raise CommandError("Sensitive command max_bytes must be between 1 and 10000000")
        raw_secrets = tuple(secret for secret in secrets if secret)
        visible_inputs = (*argv, str(cwd))
        if any(secret in value for secret in raw_secrets for value in visible_inputs):
            raise CommandError(
                "Raw repository-auth material is not allowed in argv, URLs, or cwd.",
                code=ErrorCode.CREDENTIAL_LEAK_BLOCKED,
                unchanged_state=("No child process was started.",),
            )
        process, (stdout, stderr) = self._communicate(
            argv,
            cwd=cwd,
            input_data=None,
            text=False,
            timeout=timeout or self.config.default_command_timeout_seconds,
            extra_env=None,
            exact_env=environment,
            cancel_token=cancel_token,
        )
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise CommandError("Sensitive command returned text-mode output")
        if (process.returncode or 0) != 0:
            raise CommandError(
                "Sensitive command failed without exposing captured output.",
                code=ErrorCode.COMMAND_FAILED,
                details={"command": argv[0], "exit_code": process.returncode or 0},
                unchanged_state=("No captured secret was returned or persisted.",),
            )
        if len(stdout) > max_bytes:
            raise CommandError(
                "Sensitive command output exceeded its reviewed byte bound.",
                code=ErrorCode.COMMAND_FAILED,
                details={"command": argv[0], "max_bytes": max_bytes},
                unchanged_state=("No captured secret was returned or persisted.",),
            )
        return stdout

    def run_secret_text(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str] = (),
        timeout: int | None = None,
        max_bytes: int = 100_000,
        cancel_token: CancellationToken | None = None,
    ) -> EphemeralSecret:
        """Capture one secret as ephemeral memory, never as a ``CommandResult``."""

        raw = self._run_sensitive_bytes(
            argv,
            cwd=cwd,
            environment=environment,
            secrets=secrets,
            timeout=timeout,
            max_bytes=max_bytes,
            cancel_token=cancel_token,
        )
        buffer = bytearray(raw)
        del raw
        while buffer and chr(buffer[0]).isspace():
            del buffer[0]
        while buffer and chr(buffer[-1]).isspace():
            buffer.pop()
        try:
            return EphemeralSecret.from_bytes(buffer)
        finally:
            for index in range(len(buffer)):
                buffer[index] = 0

    def run_secret_json(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        field: str,
        timeout: int | None = None,
        max_bytes: int = 1_000_000,
        cancel_token: CancellationToken | None = None,
    ) -> tuple[dict[str, object], EphemeralSecret]:
        """Parse bounded JSON and detach one secret field before returning metadata."""

        if not isinstance(field, str) or not field or len(field) > 128:
            raise CommandError("Sensitive JSON field must be bounded non-empty text")
        raw = self._run_sensitive_bytes(
            argv,
            cwd=cwd,
            environment=environment,
            secrets=secrets,
            timeout=timeout,
            max_bytes=max_bytes,
            cancel_token=cancel_token,
        )
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CommandError(
                "Sensitive command returned invalid JSON without exposing captured output.",
                code=ErrorCode.COMMAND_FAILED,
                unchanged_state=("No captured secret was returned or persisted.",),
            ) from None
        finally:
            del raw
        if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
            raise CommandError(
                "Sensitive command returned an invalid JSON object.",
                code=ErrorCode.COMMAND_FAILED,
                unchanged_state=("No captured secret was returned or persisted.",),
            )
        value = parsed.pop(field, None)
        if not isinstance(value, str) or not value:
            raise CommandError(
                "Sensitive command omitted the reviewed secret field.",
                code=ErrorCode.COMMAND_FAILED,
                unchanged_state=("No captured secret was returned or persisted.",),
            )
        metadata = {str(key): item for key, item in parsed.items()}
        return metadata, EphemeralSecret.from_text(value)

    def _run_text(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        exact_env: Mapping[str, str] | None = None,
        secrets: Sequence[str] = (),
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        if not argv or not all(isinstance(x, str) and x for x in argv):
            raise CommandError("Command argv must contain non-empty strings")
        actual_timeout = timeout or self.config.default_command_timeout_seconds
        limit = output_limit or self.config.max_tool_output_chars
        process, (stdout, stderr) = self._communicate(
            argv,
            cwd=cwd,
            input_data=input_text,
            text=True,
            timeout=actual_timeout,
            extra_env=extra_env,
            exact_env=exact_env,
            cancel_token=cancel_token,
        )
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise CommandError("Text command returned binary output")
        redact_limit = max(1, min(max(len(stdout), len(stderr), limit), 1_000_000))
        safe_stdout = redact_text(stdout, secrets=secrets, limit=redact_limit)
        safe_stderr = redact_text(stderr, secrets=secrets, limit=redact_limit)
        selectors = _failed_selectors(
            "\n".join(part for part in (safe_stdout, safe_stderr) if part)
        )
        bounded_stdout, stdout_truncated = self._truncate(safe_stdout, limit)
        bounded_stderr, stderr_truncated = self._truncate(safe_stderr, limit)
        artifact_reference = (
            self._persist_failure_output(safe_stdout, safe_stderr)
            if (process.returncode or 0) != 0 and (stdout_truncated or stderr_truncated)
            else None
        )
        result = CommandResult(
            tuple(argv),
            str(cwd),
            process.returncode or 0,
            bounded_stdout,
            bounded_stderr,
            stdout_truncated,
            stderr_truncated,
            selectors,
            artifact_reference,
        )
        if check and result.returncode != 0:
            cancelled = cancel_token is not None and cancel_token.is_cancelled()
            message = (
                f"Command was cancelled by operator request: {' '.join(argv)}"
                if cancelled
                else f"Command failed with exit code {result.returncode}: {' '.join(argv)}\n{result.combined or '<no output>'}"
            )
            stdout_excerpt, stdout_excerpt_truncated = self._truncate(result.stdout, 2_000)
            stderr_excerpt, stderr_excerpt_truncated = self._truncate(result.stderr, 2_000)
            raise CommandError(
                message,
                code=ErrorCode.COMMAND_FAILED,
                details={
                    "command": argv[0],
                    "argv": [redact_text(item, secrets=secrets, limit=256) for item in argv[:32]],
                    "exit_code": result.returncode,
                    "stdout_excerpt": redact_text(stdout_excerpt, secrets=secrets, limit=2_000),
                    "stderr_excerpt": redact_text(stderr_excerpt, secrets=secrets, limit=2_000),
                    "stdout_truncated": result.stdout_truncated or stdout_excerpt_truncated,
                    "stderr_truncated": result.stderr_truncated or stderr_excerpt_truncated,
                    **(
                        {
                            "failed_selectors": list(result.failed_selectors),
                            "tests": list(result.failed_selectors),
                        }
                        if result.failed_selectors
                        else {}
                    ),
                    **(
                        {"output_artifact_reference": result.output_artifact_reference}
                        if result.output_artifact_reference is not None
                        else {}
                    ),
                    **({"cancelled": True} if cancelled else {}),
                },
            )
        return result

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        actual_timeout = timeout or self.config.default_command_timeout_seconds
        process, (stdout, stderr) = self._communicate(
            argv,
            cwd=cwd,
            input_data=None,
            text=False,
            timeout=actual_timeout,
            extra_env=None,
        )
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise CommandError("Binary command returned text output")
        if process.returncode != 0:
            raise CommandError(
                f"Command failed with exit code {process.returncode}: {' '.join(argv)}\n{stderr.decode('utf-8', errors='replace')}",
                code=ErrorCode.COMMAND_FAILED,
                details={"exit_code": process.returncode},
            )
        if len(stdout) > max_bytes:
            raise CommandError(
                f"Command output exceeds fingerprint limit of {max_bytes} bytes: {' '.join(argv)}"
            )
        return stdout


CommandRunner = SubprocessCommandExecutor
