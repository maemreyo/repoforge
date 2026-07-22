"""Audited ad-hoc command runner for per-repository relaxed execution mode.

Principle: **iterate relaxed, gate strict**. This runner lets an agent working in a
repository the owner has explicitly configured as ``execution_mode = "relaxed"`` run
one exact allowlisted-runner command without a shell -- but the result is evidence
only. It never populates ``fresh.last_verification`` and can never satisfy
``require_verification_before_commit`` (enforced in
``src/repoforge/application/workspace/commit.py``); only an enrolled verification
profile can do that.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.adhoc import (
    CommandClass,
    ExecutionMode,
    classify_adhoc_command,
    validate_adhoc_argv,
)
from ...domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.execution_environment import build_execution_evidence
from ...domain.operation_task import OperationRetryability, OperationState
from ...domain.policy import normalize_relative_path
from ...ports.background_tasks import BackgroundTaskRunner
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from ..context import ApplicationContext
from ..dto import to_data
from ..execution.requests import adhoc_execution_request
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from ..operations.manager import OperationManager

_KIND = "workspace_run_adhoc"
_NETWORK_POLICY_LABEL = "advisory_local_only"
_ARGV_RECURRENCE_THRESHOLD = 3
_MAX_CHANGED_PATHS_REPORTED = 200


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocCommand:
    workspace_id: str
    argv: tuple[str, ...]
    working_directory: str | None = None
    background: bool = False
    expected_fingerprint: str | None = None
    expected_head_sha: str | None = None
    mutability: str = "read_only"


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocResult:
    workspace_id: str
    argv: list[str]
    runner: str
    working_directory: str
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: float
    fingerprint_before: str
    fingerprint_after: str
    fingerprint_changed: bool
    changed_paths: list[str]
    changed_paths_truncated: bool
    head_sha: str
    head_sha_before: str
    mutability: str
    command_class: str | None
    read_only_violation: bool
    network_policy: str
    evidence_only: bool
    satisfies_commit_gate: bool
    verification_invalidated: bool
    gate_guidance: str
    enrollment_nudge: str | None
    next_safe_actions: list[dict[str, object]]
    execution_evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkspaceRunAdhocBackgroundResult:
    operation_id: str
    phase: str
    safe_next_action: str


_GATE_GUIDANCE = (
    "This ad-hoc run is evidence only: it never satisfies require_verification_before_commit. "
    "Run an enrolled verification profile (workspace_verify / workspace_run_profile) on the exact "
    "tree immediately before workspace_commit."
)


def _strict_mode_error(repo_id: str) -> RepoForgeError:
    return RepoForgeError(
        f"Repository {repo_id!r} is enrolled in strict execution mode; the ad-hoc runner is disabled",
        code=ErrorCode.EXECUTION_MODE_STRICT,
        unchanged_state=("The workspace, configuration, and remote state were not modified.",),
        safe_next_action=(
            "Use an enrolled workspace_run_diagnostic template or workspace_run_profile instead. "
            "Relaxed execution mode can only be enabled by the repository owner via "
            f'repositories.{repo_id}.execution_mode = "relaxed" in configuration.'
        ),
    )


def _adhoc_error(
    message: str,
    code: ErrorCode,
    *,
    retryable: bool = False,
    mutation_possible: bool = False,
) -> RepoForgeError:
    unchanged = (
        (
            "No configuration, commit, or remote state changed; workspace paths named in the "
            "error may have changed.",
        )
        if mutation_possible
        else ("The workspace, configuration, commit history, and remote state were not modified.",)
    )
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        unchanged_state=unchanged,
        safe_next_action=(
            "Review the reported workspace paths and restore them explicitly before retrying."
            if mutation_possible
            else "Correct the reported condition and retry."
        ),
    )


def _resolve_working_directory(workspace: Path, working_directory: str | None) -> Path:
    if working_directory is None:
        return workspace
    relative = normalize_relative_path(working_directory)
    unresolved = workspace / relative
    if unresolved.is_symlink():
        raise SecurityError("Ad-hoc working_directory cannot be a symlink")
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(workspace.resolve(strict=True))
    except ValueError as exc:
        raise SecurityError("Ad-hoc working_directory escapes workspace") from exc
    if not candidate.is_dir():
        raise WorkspaceError(f"Ad-hoc working_directory does not exist: {working_directory}")
    return candidate


def _argv_shape_key(argv: tuple[str, ...]) -> str:
    """A stable, bounded key identifying this exact argv shape for recurrence tracking."""
    return hashlib.sha256("\x00".join(argv).encode("utf-8", "surrogatepass")).hexdigest()


def _safe_error_message(text: str, *, limit: int = 2_000) -> str:
    cleaned = "".join(ch for ch in text if ch in "\n\t\r" or ord(ch) >= 32).strip()
    if not cleaned:
        return "Background workspace_run_adhoc failed"
    if len(cleaned) <= limit:
        return cleaned
    marker = "\n... durable error excerpt omitted ...\n"
    if limit <= len(marker):
        return cleaned[:limit]
    available = limit - len(marker)
    head_size = available // 2
    tail_size = available - head_size
    return cleaned[:head_size] + marker + cleaned[-tail_size:]


class WorkspaceAdhocRunner:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        operations: OperationManager | None = None,
        background_tasks: BackgroundTaskRunner | None = None,
    ) -> None:
        self.ctx = ctx
        self.operations = operations
        self.background_tasks = background_tasks
        self._cancel_tokens: dict[str, CancellationToken] = {}
        self._cancel_tokens_lock = threading.Lock()

    def execute(
        self, c: WorkspaceRunAdhocCommand
    ) -> WorkspaceRunAdhocResult | WorkspaceRunAdhocBackgroundResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        if repo.execution_mode is not ExecutionMode.RELAXED:
            raise _strict_mode_error(repo.repo_id)
        if c.mutability not in {"read_only", "workspace"}:
            raise _adhoc_error(
                f"Ad-hoc mutability must be 'read_only' or 'workspace'; got {c.mutability!r}",
                ErrorCode.ADHOC_ARGV_INVALID,
            )
        argv = validate_adhoc_argv(c.argv, repo.adhoc_runners)
        # Content-inspect the exact argv: blocks irreversible/history-rewriting git forms
        # (raising ADHOC_COMMAND_FORBIDDEN before any process starts) and infers whether a
        # git command is read-only or mutating. Non-git runners return None (opaque).
        command_class = classify_adhoc_command(argv)
        declared_mutating = c.mutability == "workspace"
        effective_mutating = declared_mutating or command_class is CommandClass.MUTATING
        if command_class is CommandClass.MUTATING and not declared_mutating:
            raise _adhoc_error(
                f"{argv[0]} {argv[1] if len(argv) > 1 else ''}".strip()
                + " changes workspace or history state; call with mutability='workspace' and both "
                "expected_head_sha and expected_fingerprint so the run is bound to reviewed state",
                ErrorCode.ADHOC_ARGV_INVALID,
            )
        if effective_mutating:
            missing = [
                name
                for name, value in (
                    ("expected_head_sha", c.expected_head_sha),
                    ("expected_fingerprint", c.expected_fingerprint),
                )
                if value is None
            ]
            if missing:
                raise _adhoc_error(
                    "Mutating ad-hoc runs require an exact-state lock; missing: "
                    + ", ".join(missing),
                    ErrorCode.ADHOC_ARGV_INVALID,
                )
        command_cwd = _resolve_working_directory(path, c.working_directory)
        working_directory_display = str(command_cwd.relative_to(path.resolve(strict=True)) or ".")

        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "runner": argv[0],
            "argv_length": len(argv),
            "network_policy": _NETWORK_POLICY_LABEL,
            "expected_fingerprint": c.expected_fingerprint,
            "expected_head_sha": c.expected_head_sha,
            "mutability": c.mutability,
            "command_class": command_class.value if command_class is not None else None,
        }

        def record_command_failure(exc: CommandError) -> None:
            audit_details["exit_code"] = exc.details.get("exit_code")
            if exc.details.get("cancelled"):
                audit_details["cancelled"] = True

        def run_body(cancel_token: CancellationToken | None) -> WorkspaceRunAdhocResult:
            with self.ctx.locks.lock(c.workspace_id):
                fresh, locked_repo, locked_workspace = self.ctx.workspace(c.workspace_id)
                before_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                before = read_fingerprint(
                    self.ctx.fingerprint_cache, c.workspace_id, self.ctx.git, locked_workspace
                )
                before_fingerprint = before.fingerprint
                head_before = self.ctx.git.head_sha(locked_workspace)
                if (
                    c.expected_fingerprint is not None
                    and c.expected_fingerprint != before_fingerprint
                ):
                    raise WorkspaceError(
                        "Workspace changed since the verification plan was reviewed"
                    )
                if c.expected_head_sha is not None and c.expected_head_sha != head_before:
                    raise WorkspaceError(
                        "STALE_STATE: workspace HEAD changed since the ad-hoc run was reviewed",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        details={
                            "expected_head_sha": c.expected_head_sha,
                            "actual_head_sha": head_before,
                        },
                        unchanged_state=(
                            "No command was started; the workspace and remote state were not modified.",
                        ),
                        safe_next_action=(
                            "Read workspace_status for the current HEAD and fingerprint, then reissue "
                            "the ad-hoc run bound to the reviewed values."
                        ),
                    )
                audit_details["fingerprint_source"] = before.source
                audit_details["head_sha_before"] = head_before

                started = time.monotonic()
                result: CommandResult | None = None
                command_error: CommandError | None = None
                execution_evidence_data: dict[str, object] = {}
                execution_request = adhoc_execution_request(
                    workspace_id=c.workspace_id,
                    workspace_root=locked_workspace,
                    command_cwd=command_cwd,
                    argv=argv,
                    working_directory_policy=c.working_directory or ".",
                    timeout_seconds=locked_repo.adhoc_timeout_seconds,
                    output_limit=self.ctx.config.server.max_tool_output_chars,
                    cancel_token=cancel_token,
                )
                try:
                    with self.ctx.execution.prepare(execution_request) as session:
                        result = session.execute(argv).result
                        inspection = session.inspect()
                        execution_evidence_data = to_data(
                            build_execution_evidence(
                                execution_request.requested_policy,
                                inspection.identity,
                                inspection.effective_policy,
                                inspection.warnings,
                            )
                        )
                except CommandError as exc:
                    command_error = exc
                    record_command_failure(exc)
                duration_ms = round((time.monotonic() - started) * 1000, 3)
                audit_details["duration_ms"] = duration_ms

                after = prime_fingerprint(
                    self.ctx.fingerprint_cache, c.workspace_id, self.ctx.git, locked_workspace
                )
                after_fingerprint = after.fingerprint
                fingerprint_changed = after_fingerprint != before_fingerprint
                audit_details["fingerprint_changed"] = fingerprint_changed
                # A git command RepoForge classified read-only that nonetheless changed the
                # working tree is a contract violation worth surfacing loudly (defense in depth
                # against misclassification); the verification invalidation below still protects
                # the commit gate.
                read_only_violation = (
                    command_class is CommandClass.READ_ONLY and fingerprint_changed
                )
                audit_details["read_only_violation"] = read_only_violation

                verification_invalidated = False
                if fingerprint_changed and fresh.last_verification is not None:
                    fresh.last_verification = None
                    self.ctx.store.save(fresh)
                    verification_invalidated = True

                try:
                    after_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                    combined_paths = sorted(set(before_paths) | set(after_paths))
                except SecurityError:
                    combined_paths = sorted(before_paths)

                if command_error is not None:
                    audit_details["exit_code"] = command_error.details.get("exit_code")
                    raise command_error

                assert result is not None
                nudge: str | None = None
                tracker = self.ctx.nudge_tracker
                if tracker is not None:
                    shape_key = _argv_shape_key(argv)
                    if tracker.observe_adhoc_argv(c.workspace_id, shape_key, self.ctx.now_epoch()):
                        nudge = (
                            "This exact ad-hoc command shape has recurred at least "
                            f"{_ARGV_RECURRENCE_THRESHOLD} times in this workspace. Consider "
                            "asking the repository owner to enroll it as a workspace_run_diagnostic "
                            "template so future runs are validated and evidence-tracked."
                        )

                next_actions: list[dict[str, object]] = []
                if read_only_violation:
                    next_actions.append(
                        {
                            "action": "workspace_status",
                            "reason": (
                                "A command declared read-only changed the workspace tree; review the "
                                "unexpected mutation and restore paths if it was unintended."
                            ),
                            "required": True,
                        }
                    )
                elif fingerprint_changed:
                    next_actions.append(
                        {
                            "action": "workspace_status",
                            "reason": "The ad-hoc command changed the workspace fingerprint.",
                            "required": True,
                        }
                    )
                next_actions.append(
                    {
                        "action": "workspace_run_profile",
                        "reason": _GATE_GUIDANCE,
                        "required": True,
                    }
                )

                changed_paths = combined_paths[:_MAX_CHANGED_PATHS_REPORTED]
                return WorkspaceRunAdhocResult(
                    workspace_id=c.workspace_id,
                    argv=list(argv),
                    runner=argv[0],
                    working_directory=working_directory_display,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    stdout_truncated=result.stdout_truncated,
                    stderr_truncated=result.stderr_truncated,
                    duration_ms=duration_ms,
                    fingerprint_before=before_fingerprint,
                    fingerprint_after=after_fingerprint,
                    fingerprint_changed=fingerprint_changed,
                    changed_paths=changed_paths,
                    changed_paths_truncated=len(combined_paths) > _MAX_CHANGED_PATHS_REPORTED,
                    head_sha=self.ctx.git.head_sha(locked_workspace),
                    head_sha_before=head_before,
                    mutability=c.mutability,
                    command_class=command_class.value if command_class is not None else None,
                    read_only_violation=read_only_violation,
                    network_policy=_NETWORK_POLICY_LABEL,
                    evidence_only=True,
                    satisfies_commit_gate=False,
                    verification_invalidated=verification_invalidated,
                    gate_guidance=_GATE_GUIDANCE,
                    enrollment_nudge=nudge,
                    next_safe_actions=next_actions,
                    execution_evidence=execution_evidence_data,
                )

        if not c.background:
            return self.ctx.audited(_KIND, audit_details, lambda: run_body(None))

        return self._start_background(c, run_body, audit_details)

    # ------------------------------------------------------------------
    # Background execution via the existing durable-operations pipeline.
    # Mirrors WorkspaceProfileRunner's background path (same admission,
    # cancellation, and result-persistence contract).
    # ------------------------------------------------------------------

    def _register_cancel_token(self, operation_id: str, token: CancellationToken) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens[operation_id] = token

    def _unregister_cancel_token(self, operation_id: str) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens.pop(operation_id, None)

    def request_live_cancel(self, operation_id: str) -> bool:
        with self._cancel_tokens_lock:
            token = self._cancel_tokens.get(operation_id)
        if token is None:
            return False
        token.cancel()
        return True

    def _start_background(
        self,
        c: WorkspaceRunAdhocCommand,
        run_body: Callable[[CancellationToken | None], WorkspaceRunAdhocResult],
        audit_details: dict[str, object],
    ) -> WorkspaceRunAdhocBackgroundResult:
        operations = self.operations
        background_tasks = self.background_tasks
        result_store = self.ctx.operation_result_store
        if operations is None or background_tasks is None or result_store is None:
            raise RepoForgeError(
                "Background workspace_run_adhoc requires the durable operations manager, "
                "operation result store, and background task runner to be configured",
                code=ErrorCode.CONFIG_INVALID,
            )

        lock_cm = self.ctx.locks.lock(
            c.workspace_id,
            timeout_seconds=0,
            metadata={"purpose": "workspace_run_adhoc_background"},
        )
        lock_cm.__enter__()

        now = self.ctx.clock.now_iso()
        try:
            with self.ctx.locks.lock(
                "background-adhoc-admission",
                timeout_seconds=2,
                metadata={"purpose": "background_adhoc_admission"},
            ):
                cap = self.ctx.config.server.max_background_profiles
                running = sum(
                    1
                    for candidate in operations.list_records(max_records=2_000).records
                    if candidate.kind == _KIND and candidate.state is OperationState.RUNNING
                )
                if running >= cap:
                    raise RepoForgeError(
                        f"Background workspace_run_adhoc is at its configured concurrency cap "
                        f"of {cap} running operation(s)",
                        code=ErrorCode.RUNTIME_UNAVAILABLE,
                        retryable=True,
                        safe_next_action=(
                            f"Wait for a running background ad-hoc run to finish "
                            f"(max_background_profiles={cap}) and retry, or poll "
                            f"operation_list with scope='workspace:{c.workspace_id}' for progress."
                        ),
                        details={"max_background_profiles": cap, "running": running},
                    )
                task = operations.create(
                    kind=_KIND,
                    phase="queued",
                    cancel_supported=True,
                    workspace_id=c.workspace_id,
                    now=now,
                )
                try:
                    task = operations.start(task.operation_id, now=now)
                except Exception:
                    with contextlib.suppress(Exception):
                        operations.fail(
                            task.operation_id,
                            error_code=ErrorCode.INTERNAL_ERROR.value,
                            error_message="Background admission could not transition to running",
                        )
                    raise
        except Exception:
            lock_cm.__exit__(None, None, None)
            raise

        operation_id = task.operation_id
        cancel_token = CancellationToken()
        self._register_cancel_token(operation_id, cancel_token)

        def finish_terminal(exc: Exception | None, result: WorkspaceRunAdhocResult | None) -> None:
            finish_now = self.ctx.clock.now_iso()
            try:
                operations.status(operation_id)
            except RepoForgeError:
                return
            if exc is None and result is not None:
                try:
                    result_store.save(operation_id, to_data(result))
                    operations.succeed(
                        operation_id,
                        result_reference=f"{_KIND}:{operation_id}",
                        now=finish_now,
                    )
                except Exception as persist_exc:
                    with contextlib.suppress(Exception):
                        result_store.delete(operation_id)
                    with contextlib.suppress(RepoForgeError):
                        operations.fail(
                            operation_id,
                            error_code=ErrorCode.STATE_PERSISTENCE_FAILED.value,
                            error_message=_safe_error_message(str(persist_exc)),
                            retryability=OperationRetryability.MANUAL,
                            now=finish_now,
                        )
                return
            with contextlib.suppress(Exception):
                result_store.delete(operation_id)
            if cancel_token.is_cancelled():
                with contextlib.suppress(RepoForgeError):
                    operations.cancelled(operation_id, now=finish_now)
                return
            failure = exc or RepoForgeError(
                "Background ad-hoc run completed without a result", code=ErrorCode.INTERNAL_ERROR
            )
            code = str(
                getattr(
                    getattr(failure, "code", None),
                    "value",
                    getattr(failure, "code", "INTERNAL_ERROR"),
                )
            )
            try:
                normalized = ErrorCode(code)
            except ValueError:
                normalized = ErrorCode.INTERNAL_ERROR
            retryable = bool(getattr(failure, "retryable", False))
            with contextlib.suppress(RepoForgeError):
                operations.fail(
                    operation_id,
                    error_code=normalized.value,
                    error_message=_safe_error_message(str(failure)),
                    retryability=(
                        OperationRetryability.AUTOMATIC
                        if retryable
                        else OperationRetryability.MANUAL
                    ),
                    now=finish_now,
                )

        def run() -> None:
            failure: Exception | None = None
            result: WorkspaceRunAdhocResult | None = None
            try:
                try:
                    result = self.ctx.audited(_KIND, audit_details, lambda: run_body(cancel_token))
                except Exception as exc:
                    failure = exc
            finally:
                self._unregister_cancel_token(operation_id)
                lock_cm.__exit__(None, None, None)
            finish_terminal(failure, result)

        scheduled = background_tasks.submit(operation_id, run)
        if not scheduled:
            self._unregister_cancel_token(operation_id)
            lock_cm.__exit__(None, None, None)
            operations.fail(
                operation_id,
                error_code=ErrorCode.INTERNAL_ERROR.value,
                error_message="Background task runner could not accept the ad-hoc run",
            )
            raise RepoForgeError(
                "Background task runner rejected the ad-hoc run", code=ErrorCode.INTERNAL_ERROR
            )

        return WorkspaceRunAdhocBackgroundResult(
            operation_id=operation_id,
            phase="running",
            safe_next_action=(
                "Poll operation_status; the workspace lock is held until the run completes."
            ),
        )
