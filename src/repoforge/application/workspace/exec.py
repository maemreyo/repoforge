"""`workspace_exec` (#376): first-class ad-hoc command execution.

Supersedes `workspace_verify(mode="adhoc")` for the run-a-command intent (see
docs/architecture/autonomy-policy-model.md §9) by promoting the SAME underlying
machinery -- `WorkspaceAdhocRunner`, `classify_adhoc_command`, the durable admission
queue -- behind a dedicated, leaner tool contract instead of reimplementing execution.
`workspace_verify(mode="adhoc")` keeps working during the deprecation window §11
requires; this module does not remove or alter that path.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ...domain.errors import ErrorCode, WorkspaceError
from ...domain.operation_task import OperationState, OperationTask
from ...domain.operation_work import OperationWorkRequest
from ..context import ApplicationContext
from ..operations import durable_wait
from ..operations.manager import OperationManager
from ..operations.work_admission import DurableWorkAdmission
from .run_adhoc import (
    WorkspaceAdhocRunner,
    WorkspaceRunAdhocBackgroundResult,
    WorkspaceRunAdhocResult,
    WorkspaceRunAdhocSequenceResult,
)
from .snapshot import WorkspaceSnapshotReader
from .verify import _adhoc_evidence, _command_evidence

_WAIT_SAFE_NEXT_ACTION = (
    "Wait for operation {operation_id} with until='terminal' and timeout_seconds=60, "
    "re-issuing the same call while it times out. 60 is the safe default: some clients "
    "block a tool call held much longer, and a blocked call costs a whole turn while a "
    "re-issued wait costs nothing. Do not spin on operation get -- progress mode wakes "
    "you once per step and tells you nothing extra."
)


def _adhoc_sequence_evidence(result: WorkspaceRunAdhocSequenceResult) -> dict[str, object]:
    """Project a sequence's (#443) policy facts onto the same AdhocEvidence shape a
    single command uses. `command_class`/`observed_effect` have no single value across a
    sequence's (possibly mixed) elements, so they are always null here (#382);
    `effect_mismatch` is still meaningful as an aggregate -- see
    WorkspaceRunAdhocSequenceResult.effect_mismatch. `content_inspected` is true only when
    every element was git and actually inspected -- see
    WorkspaceRunAdhocSequenceResult.all_content_inspected."""
    return {
        "mutability": result.mutability,
        "command_class": None,
        "declared_effect": result.declared_effect,
        "observed_effect": None,
        "effect_mismatch": result.effect_mismatch,
        "content_inspected": result.all_content_inspected,
        "fingerprint_changed": result.fingerprint_changed,
        "read_only_violation": result.read_only_violation,
        "changed_paths": list(result.changed_paths),
        "changed_paths_truncated": result.changed_paths_truncated,
        "network_policy": result.network_policy,
        "verification_invalidated": result.verification_invalidated,
    }


@dataclass(frozen=True, slots=True)
class WorkspaceExecCommand:
    workspace_id: str
    argv: tuple[str, ...] | None = None
    # Reviewed shell-script form (#377) and bounded fail-fast argv sequence (#443):
    # mutually exclusive with argv and with each other -- the contract layer
    # (WorkspaceExecInput) enforces exactly one of the three is set.
    script: str | None = None
    shell: str | None = None
    argv_sequence: tuple[tuple[str, ...], ...] | None = None
    working_directory: str | None = None
    stdin_text: str | None = None
    expected_fingerprint: str | None = None
    expected_head_sha: str | None = None
    mutability: str = "read_only"
    declared_effect: str | None = None
    background: bool = False
    #: Opaque #383 `trusted_host` lease token (never persisted or echoed back --
    #: only whether one resolved to an active lease is recorded as evidence).
    lease_token: str | None = None
    #: Request the #384 `sandboxed_turbo` execution backend for this call.
    sandbox_requested: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceExecResult:
    workspace_id: str
    outcome: str
    next_action: str | None
    operation: dict[str, object] | None
    commands: list[dict[str, object]]
    satisfies_commit_gate: bool
    head_sha: str
    workspace_fingerprint: str
    execution_evidence: dict[str, object] = field(default_factory=dict)
    adhoc_evidence: dict[str, object] | None = None


class WorkspaceExecutor:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        adhoc: WorkspaceAdhocRunner,
        admission: DurableWorkAdmission,
        operations: OperationManager,
    ) -> None:
        self.ctx = ctx
        self._snapshot = WorkspaceSnapshotReader(ctx)
        self._adhoc = adhoc
        self._admission = admission
        self._operations = operations

    def execute(self, command: WorkspaceExecCommand) -> WorkspaceExecResult:
        audit_details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "mutability": command.mutability,
            "background": command.background,
        }
        return self.ctx.audited(
            "workspace_exec",
            audit_details,
            lambda: self._execute(command),
            mutating=True,
        )

    def _execute(self, command: WorkspaceExecCommand) -> WorkspaceExecResult:
        snapshot = self._snapshot.capture(command.workspace_id)
        head_sha = snapshot.head_sha
        workspace_fingerprint = snapshot.workspace_fingerprint
        if (
            command.expected_fingerprint is not None
            and command.expected_fingerprint != workspace_fingerprint
        ):
            raise WorkspaceError(
                "Workspace changed since the requested execution snapshot was reviewed"
            )
        if command.expected_head_sha is not None and command.expected_head_sha != head_sha:
            raise WorkspaceError(
                "STALE_STATE: workspace HEAD changed since the requested execution "
                "snapshot was reviewed",
                code=ErrorCode.STALE_STATE,
                retryable=True,
                details={
                    "expected_head_sha": command.expected_head_sha,
                    "actual_head_sha": head_sha,
                },
            )

        admitted = self._admission.admit(
            OperationWorkRequest.adhoc(
                workspace_id=command.workspace_id,
                argv=command.argv or (),
                script=command.script,
                shell=command.shell,
                argv_sequence=command.argv_sequence,
                working_directory=command.working_directory,
                mutability=command.mutability,
                expected_head_sha=head_sha,
                expected_fingerprint=workspace_fingerprint,
                config_generation=self.ctx.config_generation,
                stdin_text=command.stdin_text,
                declared_effect=command.declared_effect,
                lease_token=command.lease_token,
                sandbox_requested=command.sandbox_requested,
            ),
            operation_kind="workspace_run_adhoc",
        )
        task: OperationTask = admitted
        stored: dict[str, Any] | None = None
        if not command.background:
            task, stored = durable_wait.wait_for_operation(
                self.ctx, self._operations, admitted.operation_id
            )
        delegated: (
            WorkspaceRunAdhocResult
            | WorkspaceRunAdhocSequenceResult
            | WorkspaceRunAdhocBackgroundResult
        )
        if task.state is OperationState.SUCCEEDED and stored is not None:
            delegated = (
                WorkspaceRunAdhocSequenceResult(**stored)
                if command.argv_sequence is not None
                else WorkspaceRunAdhocResult(**stored)
            )
        else:
            delegated = WorkspaceRunAdhocBackgroundResult(
                operation_id=admitted.operation_id,
                phase=task.phase,
                safe_next_action=_WAIT_SAFE_NEXT_ACTION.format(operation_id=admitted.operation_id),
            )
        result = self._project(
            command, delegated, head_sha=head_sha, workspace_fingerprint=workspace_fingerprint
        )
        if isinstance(delegated, WorkspaceRunAdhocBackgroundResult):
            result = replace(result, operation=durable_wait.operation_projection(task))
        return result

    @staticmethod
    def _project(
        command: WorkspaceExecCommand,
        delegated: (
            WorkspaceRunAdhocResult
            | WorkspaceRunAdhocSequenceResult
            | WorkspaceRunAdhocBackgroundResult
        ),
        *,
        head_sha: str,
        workspace_fingerprint: str,
    ) -> WorkspaceExecResult:
        if isinstance(delegated, WorkspaceRunAdhocBackgroundResult):
            return WorkspaceExecResult(
                workspace_id=command.workspace_id,
                outcome="running",
                next_action=delegated.safe_next_action,
                operation={
                    "operation_id": delegated.operation_id,
                    "kind": "workspace_run_adhoc",
                    "state": "pending" if delegated.phase == "queued" else "running",
                    "phase": delegated.phase,
                },
                commands=[],
                satisfies_commit_gate=False,
                head_sha=head_sha,
                workspace_fingerprint=workspace_fingerprint,
            )
        if isinstance(delegated, WorkspaceRunAdhocSequenceResult):
            last_returncode = delegated.commands[-1]["returncode"] if delegated.commands else 1
            outcome = "passed" if last_returncode == 0 and not delegated.stopped_early else "failed"
            return WorkspaceExecResult(
                workspace_id=command.workspace_id,
                outcome=outcome,
                next_action=None,
                operation=None,
                commands=[_command_evidence(item) for item in delegated.commands],
                satisfies_commit_gate=False,
                head_sha=delegated.head_sha,
                workspace_fingerprint=delegated.fingerprint_after,
                execution_evidence=delegated.execution_evidence,
                adhoc_evidence=_adhoc_sequence_evidence(delegated),
            )
        raw = {
            "argv": delegated.argv,
            "returncode": delegated.returncode,
            "duration_ms": delegated.duration_ms,
            "stdout": delegated.stdout,
            "stderr": delegated.stderr,
            "output_artifact_reference": delegated.output_artifact_reference,
            "output_artifact_status": delegated.output_artifact_status,
        }
        return WorkspaceExecResult(
            workspace_id=command.workspace_id,
            outcome="passed" if delegated.returncode == 0 else "failed",
            next_action=None,
            operation=None,
            commands=[_command_evidence(raw)],
            satisfies_commit_gate=False,
            head_sha=delegated.head_sha,
            workspace_fingerprint=delegated.fingerprint_after,
            execution_evidence=delegated.execution_evidence,
            adhoc_evidence=_adhoc_evidence(delegated),
        )
