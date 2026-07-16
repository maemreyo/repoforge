import contextlib
import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ...domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.execution_environment import EnvironmentIdentityRequest
from ...domain.operation_task import OperationRetryability, OperationState
from ...domain.policy import normalize_relative_path
from ...domain.verification import get_profile
from ...domain.workspace import VerificationReceipt
from ...ports.background_tasks import BackgroundTaskRunner
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from ...ports.execution_environment import ApprovedExecution
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint
from ..operations.manager import OperationManager

_KIND = "workspace_run_profile"


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileCommand:
    workspace_id: str
    profile_name: str
    background: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileResult:
    workspace_id: str
    profile: str
    description: str
    verification: bool
    fingerprint: str
    commands: list[dict[str, object]]
    change_metrics: dict[str, object]
    satisfies_commit_gate: bool
    head_sha: str
    working_directory: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileBackgroundResult:
    operation_id: str
    phase: str
    safe_next_action: str


def _safe_error_message(text: str, *, limit: int = 2_000) -> str:
    """Bound and sanitize a raw exception message for a durable operation record."""
    cleaned = "".join(ch for ch in text if ch in "\n\t\r" or ord(ch) >= 32)
    cleaned = cleaned[:limit].strip()
    return cleaned or "Background workspace_run_profile failed"


class WorkspaceProfileRunner:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        operations: OperationManager | None = None,
        background_tasks: BackgroundTaskRunner | None = None,
    ):
        self.ctx: ApplicationContext = ctx
        self.operations = operations
        self.background_tasks = background_tasks
        self._cancel_tokens: dict[str, CancellationToken] = {}
        self._cancel_tokens_lock = threading.Lock()

    @staticmethod
    def public(r: CommandResult) -> dict[str, object]:
        return {
            "argv": list(r.argv),
            "returncode": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }

    @staticmethod
    def receipt(r: CommandResult) -> dict[str, object]:
        return {
            "argv": list(r.argv),
            "returncode": r.returncode,
            "stdout_sha256": hashlib.sha256(r.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(r.stderr.encode()).hexdigest(),
        }

    def _register_cancel_token(self, operation_id: str, token: CancellationToken) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens[operation_id] = token

    def _unregister_cancel_token(self, operation_id: str) -> None:
        with self._cancel_tokens_lock:
            self._cancel_tokens.pop(operation_id, None)

    def request_live_cancel(self, operation_id: str) -> bool:
        """Signal the process group of a live background run; a no-op if none is bound."""
        with self._cancel_tokens_lock:
            token = self._cancel_tokens.get(operation_id)
        if token is None:
            return False
        token.cancel()
        return True

    def _running_operation_id(self, workspace_id: str) -> str | None:
        if self.operations is None:
            return None
        for task in self.operations.list_records(max_records=2_000).records:
            if (
                task.kind == _KIND
                and task.workspace_id == workspace_id
                and task.state is OperationState.RUNNING
            ):
                return task.operation_id
        return None

    def execute(
        self, c: WorkspaceRunProfileCommand
    ) -> WorkspaceRunProfileResult | WorkspaceRunProfileBackgroundResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        profile = get_profile(repo, c.profile_name)
        command_cwd = path
        if profile.working_directory:
            relative = normalize_relative_path(profile.working_directory)
            unresolved = path / relative
            if unresolved.is_symlink():
                raise SecurityError("Profile working_directory cannot be a symlink")
            command_cwd = unresolved.resolve(strict=False)
            try:
                _ = command_cwd.relative_to(path.resolve(strict=True))
            except ValueError as exc:
                raise SecurityError("Profile working_directory escapes workspace") from exc
            if not command_cwd.is_dir():
                raise WorkspaceError(
                    f"Profile working_directory does not exist: {profile.working_directory}"
                )

        def record_command_failure(
            exc: CommandError, command: tuple[str, ...], steps_completed: int
        ) -> None:
            fallback = command[0] if command else None
            audit_details["failed_command"] = exc.details.get("command", fallback)
            audit_details["exit_code"] = exc.details.get("exit_code")
            audit_details["steps_completed"] = steps_completed
            if exc.details.get("cancelled"):
                audit_details["cancelled"] = True

        def run_body(
            cancel_token: CancellationToken | None,
            on_before_command: Callable[[], None] | None,
        ) -> WorkspaceRunProfileResult:
            fresh = self.ctx.store.load(c.workspace_id)
            timeout = profile.timeout_seconds or self.ctx.config.server.verification_timeout_seconds
            environment_hash: str | None = None
            if self.ctx.execution_environment is not None:
                request = EnvironmentIdentityRequest(
                    workspace_root=path,
                    command_cwd=command_cwd,
                    commands=profile.commands,
                    working_directory_policy=profile.working_directory or ".",
                )
                self.ctx.execution_environment.prepare(request)
                try:
                    identity = self.ctx.execution_environment.identity(request)
                    receipts = []
                    for step, command in enumerate(profile.commands):
                        if on_before_command is not None:
                            on_before_command()
                        try:
                            receipts.append(
                                self.ctx.execution_environment.execute(
                                    ApprovedExecution(
                                        command, request, identity, timeout, cancel_token
                                    )
                                )
                            )
                        except CommandError as exc:
                            record_command_failure(exc, command, step)
                            raise
                finally:
                    self.ctx.execution_environment.cleanup(request)
                results = [receipt.result for receipt in receipts]
                environment_hash = identity.identity_hash
            else:
                results = []
                for step, command in enumerate(profile.commands):
                    if on_before_command is not None:
                        on_before_command()
                    try:
                        if cancel_token is None:
                            results.append(
                                self.ctx.commands.run(command, cwd=command_cwd, timeout=timeout)
                            )
                        else:
                            results.append(
                                self.ctx.commands.run(
                                    command,
                                    cwd=command_cwd,
                                    timeout=timeout,
                                    cancel_token=cancel_token,
                                )
                            )
                    except CommandError as exc:
                        record_command_failure(exc, command, step)
                        raise
            _ = self.ctx.git.changed_paths(path, repo)
            metrics = self.ctx.git.enforce_change_budget(path, repo)
            fingerprint = prime_fingerprint(
                self.ctx.fingerprint_cache,
                c.workspace_id,
                self.ctx.git,
                path,
            )
            fp = fingerprint.fingerprint
            if profile.verification:
                fresh.last_verification = VerificationReceipt(
                    profile.name,
                    fp,
                    self.ctx.clock.now_iso(),
                    [self.receipt(r) for r in results],
                    environment_hash,
                )
                self.ctx.store.save(fresh)
            return WorkspaceRunProfileResult(
                c.workspace_id,
                profile.name,
                profile.description,
                profile.verification,
                fp,
                [self.public(r) for r in results],
                metrics,
                profile.verification,
                self.ctx.git.head_sha(path),
                profile.working_directory,
            )

        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "profile": c.profile_name,
        }

        if not c.background:

            def op() -> WorkspaceRunProfileResult:
                with self.ctx.locks.lock(c.workspace_id):
                    return run_body(None, None)

            return self.ctx.audited(
                "workspace_run_profile",
                audit_details,
                op,
            )

        return self._start_background(c, run_body, audit_details)

    def _start_background(
        self,
        c: WorkspaceRunProfileCommand,
        run_body: Callable[
            [CancellationToken | None, Callable[[], None] | None], WorkspaceRunProfileResult
        ],
        audit_details: dict[str, object],
    ) -> WorkspaceRunProfileBackgroundResult:
        operations = self.operations
        background_tasks = self.background_tasks
        if operations is None or background_tasks is None:
            raise RepoForgeError(
                "Background workspace_run_profile requires the durable operations manager "
                "and background task runner to be configured",
                code=ErrorCode.CONFIG_INVALID,
            )

        cap = self.ctx.config.server.max_background_profiles
        running = sum(
            1
            for task in operations.list_records(max_records=2_000).records
            if task.kind == _KIND and task.state is OperationState.RUNNING
        )
        if running >= cap:
            raise RepoForgeError(
                f"Background workspace_run_profile is at its configured concurrency cap "
                f"of {cap} running operation(s)",
                code=ErrorCode.RUNTIME_UNAVAILABLE,
                retryable=True,
                safe_next_action=(
                    f"Wait for a running background profile to finish (max_background_profiles"
                    f"={cap}) and retry, or poll operation_list with "
                    f"scope='workspace:{c.workspace_id}' for progress."
                ),
                details={"max_background_profiles": cap, "running": running},
            )

        lock_cm = self.ctx.locks.lock(
            c.workspace_id,
            timeout_seconds=0,
            metadata={"purpose": "workspace_run_profile_background"},
        )
        try:
            lock_cm.__enter__()
        except RepoForgeError as exc:
            if exc.code is ErrorCode.LOCK_TIMEOUT:
                holder = self._running_operation_id(c.workspace_id)
                suffix = f" (currently running as operation {holder!r})" if holder else ""
                raise RepoForgeError(
                    f"Workspace {c.workspace_id!r} is locked by another running operation{suffix}",
                    code=ErrorCode.LOCK_TIMEOUT,
                    retryable=True,
                    safe_next_action=(
                        f"Poll operation_status for {holder!r} and retry once it completes."
                        if holder
                        else "Poll workspace_status and retry once the current mutation completes."
                    ),
                    details={
                        "workspace_id": c.workspace_id,
                        **({"operation_id": holder} if holder else {}),
                    },
                ) from exc
            raise

        now = self.ctx.clock.now_iso()
        try:
            task = operations.create(
                kind=_KIND,
                phase="queued",
                cancel_supported=True,
                workspace_id=c.workspace_id,
                now=now,
            )
        except Exception:
            lock_cm.__exit__(None, None, None)
            raise
        try:
            task = operations.start(task.operation_id, now=now)
        except Exception:
            # The operation record was created but never reached "running"; fail it
            # closed rather than leaving a PENDING record that recovery does not scan.
            with contextlib.suppress(Exception):
                operations.fail(
                    task.operation_id,
                    error_code=ErrorCode.INTERNAL_ERROR.value,
                    error_message="Background admission could not transition to running",
                )
            lock_cm.__exit__(None, None, None)
            raise

        operation_id = task.operation_id
        cancel_token = CancellationToken()
        self._register_cancel_token(operation_id, cancel_token)

        def on_before_command() -> None:
            current = operations.status(operation_id)
            if current.cancellation_requested_at is not None:
                cancel_token.cancel()
                raise RepoForgeError(
                    "Background profile run was cancelled before the next command started",
                    code=ErrorCode.COMMAND_FAILED,
                    details={"cancelled": True},
                )

        def finish_terminal(exc: Exception | None) -> None:
            finish_now = self.ctx.clock.now_iso()
            try:
                current = operations.status(operation_id)
            except RepoForgeError:
                return
            if exc is None:
                with contextlib.suppress(RepoForgeError):
                    operations.succeed(
                        operation_id,
                        result_reference=f"{_KIND}:{operation_id}",
                        now=finish_now,
                    )
                return
            if current.cancellation_requested_at is not None or cancel_token.is_cancelled():
                with contextlib.suppress(RepoForgeError):
                    operations.cancelled(operation_id, now=finish_now)
                return
            code = str(
                getattr(getattr(exc, "code", None), "value", getattr(exc, "code", "INTERNAL_ERROR"))
            )
            try:
                normalized = ErrorCode(code)
            except ValueError:
                normalized = ErrorCode.INTERNAL_ERROR
            retryable = bool(getattr(exc, "retryable", False))
            with contextlib.suppress(RepoForgeError):
                operations.fail(
                    operation_id,
                    error_code=normalized.value,
                    error_message=_safe_error_message(str(exc)),
                    retryability=(
                        OperationRetryability.AUTOMATIC
                        if retryable
                        else OperationRetryability.MANUAL
                    ),
                    now=finish_now,
                )

        def run() -> None:
            failure: Exception | None = None
            try:
                try:
                    self.ctx.audited(
                        "workspace_run_profile",
                        audit_details,
                        lambda: run_body(cancel_token, on_before_command),
                    )
                except Exception as exc:
                    failure = exc
            finally:
                # Release the lock before marking the operation terminal, matching the
                # documented order: terminate/finish -> release lock -> mark terminal.
                self._unregister_cancel_token(operation_id)
                lock_cm.__exit__(None, None, None)
            finish_terminal(failure)

        scheduled = background_tasks.submit(operation_id, run)
        if not scheduled:
            # An operation_id collision is not expected to happen in practice (the id space
            # is a random 24-byte hex string); fail closed rather than run silently untracked.
            self._unregister_cancel_token(operation_id)
            lock_cm.__exit__(None, None, None)
            operations.fail(
                operation_id,
                error_code=ErrorCode.INTERNAL_ERROR.value,
                error_message="Background task runner could not accept the profile run",
            )
            raise RepoForgeError(
                "Background task runner rejected the profile run",
                code=ErrorCode.INTERNAL_ERROR,
            )

        return WorkspaceRunProfileBackgroundResult(
            operation_id=operation_id,
            phase="running",
            safe_next_action=(
                "Poll operation_status; the workspace lock is held until the run completes."
            ),
        )
