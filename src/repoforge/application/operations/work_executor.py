"""Typed execution boundary for claimed durable verification work."""

from __future__ import annotations

from collections.abc import Callable

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_work import OperationWorkItem, OperationWorkState
from ...ports.cancellation import CancellationToken
from ..workspace.run_adhoc import (
    WorkspaceAdhocRunner,
    WorkspaceRunAdhocCommand,
    WorkspaceRunAdhocResult,
    WorkspaceRunAdhocSequenceCommand,
    WorkspaceRunAdhocSequenceResult,
)
from ..workspace.run_diagnostic import (
    WorkspaceDiagnosticRunner,
    WorkspaceRunDiagnosticCommand,
    WorkspaceRunDiagnosticResult,
)
from ..workspace.run_profile import (
    WorkspaceProfileRunner,
    WorkspaceRunProfileCommand,
    WorkspaceRunProfileResult,
)

WorkProgress = Callable[[str, int, int, str, str], None]
VerificationWorkResult = (
    WorkspaceRunProfileResult
    | WorkspaceRunAdhocResult
    | WorkspaceRunAdhocSequenceResult
    | WorkspaceRunDiagnosticResult
)


class VerificationWorkHandlers:
    """Reconstruct exact internal commands from claimed, persisted work."""

    def __init__(
        self,
        profile_runner: WorkspaceProfileRunner,
        adhoc_runner: WorkspaceAdhocRunner,
        diagnostic_runner: WorkspaceDiagnosticRunner | None = None,
    ) -> None:
        self._profile_runner = profile_runner
        self._adhoc_runner = adhoc_runner
        self._diagnostic_runner = diagnostic_runner

    def execute(
        self,
        item: OperationWorkItem,
        *,
        cancellation_token: CancellationToken,
        progress: WorkProgress,
    ) -> VerificationWorkResult:
        if item.state is not OperationWorkState.CLAIMED:
            raise RepoForgeError(
                "Only claimed operation work can be executed",
                code=ErrorCode.OPERATION_INVALID,
            )
        request = item.request
        if request.kind == "profile":
            return self._profile_runner.execute_claimed(
                WorkspaceRunProfileCommand(
                    workspace_id=request.workspace_id,
                    profile_name=request.profile_name,
                    force_rerun=True,
                    expected_fingerprint=request.expected_fingerprint,
                    expected_head_sha=request.expected_head_sha,
                ),
                cancellation_token=cancellation_token,
                progress=progress,
            )
        if request.kind == "diagnostic":
            if self._diagnostic_runner is None or request.diagnostic_id is None:
                raise RepoForgeError(
                    "Diagnostic work handler is not configured",
                    code=ErrorCode.CONFIG_INVALID,
                )
            return self._diagnostic_runner.execute_claimed(
                WorkspaceRunDiagnosticCommand(
                    workspace_id=request.workspace_id,
                    diagnostic_id=request.diagnostic_id,
                    selector=(None if request.selector is None else list(request.selector)),
                    selector2=(None if request.selector2 is None else list(request.selector2)),
                    expected_fingerprint=request.expected_fingerprint,
                    expected_head_sha=request.expected_head_sha,
                    intent=request.intent,
                    expectation=request.expectation,
                    expected_failure_class=request.expected_failure_class,
                    force_rerun=request.force_rerun,
                    rerun_failed=request.rerun_failed,
                ),
                cancellation_token=cancellation_token,
                progress=progress,
            )
        if request.kind == "adhoc":
            if request.argv_sequence is not None:
                return self._adhoc_runner.execute_sequence_claimed(
                    WorkspaceRunAdhocSequenceCommand(
                        workspace_id=request.workspace_id,
                        argv_sequence=request.argv_sequence,
                        working_directory=request.working_directory,
                        expected_fingerprint=request.expected_fingerprint,
                        expected_head_sha=request.expected_head_sha,
                        mutability=request.mutability,
                    ),
                    cancellation_token=cancellation_token,
                    progress=progress,
                )
            return self._adhoc_runner.execute_claimed(
                WorkspaceRunAdhocCommand(
                    workspace_id=request.workspace_id,
                    argv=request.argv,
                    script=request.script,
                    shell=request.shell,
                    working_directory=request.working_directory,
                    expected_fingerprint=request.expected_fingerprint,
                    expected_head_sha=request.expected_head_sha,
                    mutability=request.mutability,
                    stdin_text=request.stdin_text,
                ),
                cancellation_token=cancellation_token,
                progress=progress,
            )
        raise RepoForgeError(
            f"Unsupported operation work kind: {request.kind}",
            code=ErrorCode.OPERATION_INVALID,
        )
