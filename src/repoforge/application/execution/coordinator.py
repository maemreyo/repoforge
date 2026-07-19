"""Coordinator-owned execution sessions with exact argv admission."""

from __future__ import annotations

from types import TracebackType

from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.execution_environment import CommandFailureMode
from ...ports.execution_environment import (
    EnvironmentInspection,
    ExecutionEnvironmentPort,
    ExecutionReceipt,
    ExecutionRequest,
    PreparedEnvironmentSession,
)


class CoordinatedExecutionSession:
    """One prepared backend session bound to a closed reviewed command set."""

    def __init__(
        self,
        backend: ExecutionEnvironmentPort,
        request: ExecutionRequest,
        prepared: PreparedEnvironmentSession,
    ) -> None:
        self._backend = backend
        self.request = request
        self.prepared = prepared
        self._closed = False

    @property
    def identity_hash(self) -> str:
        return self.prepared.identity.identity_hash

    def _require_open(self) -> None:
        if self._closed:
            raise RepoForgeError(
                "Execution session is already closed",
                code=ErrorCode.STATE_STALE,
                unchanged_state=("No repository command was started.",),
            )

    def execute(self, argv: tuple[str, ...]) -> ExecutionReceipt:
        self._require_open()
        if argv not in self.request.reviewed_commands:
            raise SecurityError(
                "Command is outside the prepared session's reviewed command set",
                details={"argv_length": len(argv)},
                unchanged_state=("No repository command was started.",),
            )
        result = self._backend.execute_in_session(
            self.prepared,
            argv,
            cwd=self.request.scope.command_cwd,
            timeout=self.request.timeout_seconds,
            output_limit=self.request.output_limit,
            check=self.request.failure_mode is CommandFailureMode.RAISE,
            cancel_token=self.request.cancel_token,
        )
        artifacts = self._backend.collect_session_artifacts(
            self.prepared,
            self.request.artifact_paths,
            root=self.request.scope.root,
        )
        return ExecutionReceipt(
            argv=argv,
            session_start_identity_hash=self.prepared.identity.identity_hash,
            result=result,
            requested_policy_hash=self.prepared.requested_policy_hash,
            effective_policy_hash=self.prepared.effective_policy_hash,
            effective_policy=self.prepared.effective_policy,
            artifacts=artifacts,
        )

    def inspect(self) -> EnvironmentInspection:
        self._require_open()
        inspection = self._backend.inspect_session(self.request, self.prepared)
        if inspection.requested_policy_hash != self.prepared.requested_policy_hash:
            raise RepoForgeError(
                "Requested execution policy binding changed during the session",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No additional repository command was started.",),
            )
        if inspection.effective_policy_hash != self.prepared.effective_policy_hash:
            raise RepoForgeError(
                "Effective execution policy changed during the session",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No additional repository command was started.",),
            )
        return inspection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.cleanup_session(self.prepared)

    def __enter__(self) -> CoordinatedExecutionSession:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


class ExecutionCoordinator:
    """Required application boundary around the selected execution backend."""

    def __init__(self, backend: ExecutionEnvironmentPort) -> None:
        self._backend = backend

    def prepare(self, request: ExecutionRequest) -> CoordinatedExecutionSession:
        prepared = self._backend.prepare_session(request)
        if prepared.requested_policy_hash != request.requested_policy.policy_hash:
            self._backend.cleanup_session(prepared)
            raise RepoForgeError(
                "Backend prepared a session for a different requested policy",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No repository command was started.",),
            )
        if prepared.effective_policy_hash != prepared.effective_policy.policy_hash:
            self._backend.cleanup_session(prepared)
            raise RepoForgeError(
                "Backend prepared an internally inconsistent effective policy",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No repository command was started.",),
            )
        return CoordinatedExecutionSession(self._backend, request, prepared)

    def inspect(self, request: ExecutionRequest) -> EnvironmentInspection:
        inspection = self._backend.inspect_session(request)
        if inspection.requested_policy_hash != request.requested_policy.policy_hash:
            raise RepoForgeError(
                "Backend inspected a different requested execution policy",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No repository command was started.",),
            )
        if inspection.effective_policy_hash != inspection.effective_policy.policy_hash:
            raise RepoForgeError(
                "Backend inspection returned an inconsistent effective policy",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No repository command was started.",),
            )
        return inspection
