"""Execute an accepted immutable plan as one durable background operation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import platform
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ...domain.errors import CommandError, ErrorCode, RepoForgeError
from ...domain.execution_plan import (
    ExecutionPlan,
    PlanStage,
    PlanStageBoundary,
    PlanStageKind,
    StageFailurePolicy,
)
from ...domain.execution_receipt import (
    ArtifactDigest,
    StageCacheStatus,
    StageReceipt,
    StageReceiptStatus,
    WorkspaceIdentity,
    create_stage_receipt,
    receipt_payload,
)
from ...domain.operation_task import OperationRetryability
from ...domain.verification_dag import (
    CacheMissReason,
    CachePolicy,
    IterationCacheKey,
    VerificationDagStage,
    build_iteration_cache_key,
    compile_plan_dag,
    create_iteration_cache_entry,
)
from ...ports.background_tasks import BackgroundTaskRunner
from ...ports.cancellation import CancellationToken
from ...ports.execution_receipt_store import ExecutionReceiptStore
from ...ports.iteration_cache import IterationCache
from ..context import ApplicationContext, repository_policy_snapshot
from ..dto import to_data
from ..operations.manager import OperationManager
from .execution_plan import ExecutionPlanService
from .failure_intelligence import FailureIntelligenceService
from .run_diagnostic import WorkspaceDiagnosticRunner, WorkspaceRunDiagnosticCommand
from .run_profile import WorkspaceProfileRunner, WorkspaceRunProfileCommand

ExecutionBoundary = Literal["iteration", "full"]
_KIND = "workspace_execute_plan"


@dataclass(frozen=True, slots=True)
class WorkspaceExecutePlanCommand:
    workspace_id: str
    plan_id: str
    through: ExecutionBoundary | str


@dataclass(frozen=True, slots=True)
class WorkspaceExecutePlanAdmission:
    operation_id: str
    phase: str
    safe_next_action: str


@dataclass(frozen=True, slots=True)
class WorkspaceExecutionReceiptsResult:
    plan_id: str
    stage_receipts: tuple[dict[str, object], ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class WorkspaceExecutePlanResult:
    operation_id: str
    workspace_id: str
    plan_id: str
    plan_hash: str
    through: str
    stage_receipts: tuple[dict[str, object], ...]
    satisfies_commit_gate: bool
    head_sha: str
    workspace_fingerprint: str


def _safe_error_message(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:2_000]


def _stable_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _workspace_files_digest(root: Path, names: tuple[str, ...]) -> str:
    payload: list[dict[str, str]] = []
    for name in names:
        path = root / name
        if not path.is_file() or path.is_symlink():
            payload.append({"path": name, "sha256": "missing"})
            continue
        payload.append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return _stable_digest(payload)


class WorkspacePlanExecutor:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        operations: OperationManager,
        background_tasks: BackgroundTaskRunner,
        profile_runner: WorkspaceProfileRunner,
        diagnostic_runner: WorkspaceDiagnosticRunner,
        failure_intelligence: FailureIntelligenceService,
    ) -> None:
        self.ctx = ctx
        self.operations = operations
        self.background_tasks = background_tasks
        self.profile_runner = profile_runner
        self.diagnostic_runner = diagnostic_runner
        self.failure_intelligence = failure_intelligence
        self.plan_service = ExecutionPlanService(ctx)
        self._tokens: dict[str, CancellationToken] = {}
        self._tokens_lock = threading.Lock()

    def _receipt_store(self) -> ExecutionReceiptStore:
        if self.ctx.execution_receipts is None:
            raise RepoForgeError(
                "Execution receipt store is unavailable",
                code=ErrorCode.CONFIG_INVALID,
            )
        return self.ctx.execution_receipts

    def _cache_store(self) -> IterationCache | None:
        return self.ctx.iteration_cache

    @staticmethod
    def _boundary(value: ExecutionBoundary | str) -> ExecutionBoundary:
        if value not in {"iteration", "full"}:
            raise RepoForgeError(
                "through must be either 'iteration' or 'full'",
                code=ErrorCode.STATE_INVALID,
            )
        return value  # type: ignore[return-value]

    @staticmethod
    def _selected_stages(plan: ExecutionPlan, through: ExecutionBoundary) -> tuple[PlanStage, ...]:
        if through == "full":
            return plan.ordered_stages
        return tuple(
            stage for stage in plan.ordered_stages if stage.boundary is PlanStageBoundary.ITERATION
        )

    def _identity(self, workspace_id: str) -> WorkspaceIdentity:
        _, repo, path = self.ctx.workspace(workspace_id)
        try:
            config_generation = hashlib.sha256(self.ctx.config.source_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RepoForgeError(
                "Active configuration generation cannot be read during plan execution",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        policy_hash = repository_policy_snapshot(repo).get("sha256")
        if not isinstance(policy_hash, str):
            raise RepoForgeError(
                "Repository policy hash is unavailable", code=ErrorCode.STATE_INVALID
            )
        return WorkspaceIdentity(
            head_sha=self.ctx.git.head_sha(path).lower(),
            workspace_fingerprint=self.ctx.git.fingerprint(path),
            config_generation=config_generation,
            policy_hash=policy_hash,
        )

    @staticmethod
    def _artifact_digest(path: Path, relative: str) -> ArtifactDigest | None:
        candidate = path / relative
        if not candidate.is_file() or candidate.is_symlink():
            return None
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return ArtifactDigest(relative, digest.hexdigest())

    def _artifact_digests(self, workspace_id: str, stage: PlanStage) -> tuple[ArtifactDigest, ...]:
        _, _, path = self.ctx.workspace(workspace_id)
        artifacts = [
            digest
            for relative in stage.artifact_paths
            if (digest := self._artifact_digest(path, relative)) is not None
        ]
        return tuple(sorted(artifacts, key=lambda item: item.path))

    def _target_payload(self, plan: ExecutionPlan, stage: PlanStage) -> object:
        _, repo, _ = self.ctx.workspace(plan.workspace_id)
        if stage.kind is PlanStageKind.PROFILE:
            return to_data(repo.profiles[stage.target])
        return to_data(repo.diagnostics[stage.target])

    def _environment_identity(self, plan: ExecutionPlan, stage: PlanStage) -> str:
        adapter = self.ctx.execution_environment
        return _stable_digest(
            {
                "adapter": type(adapter).__qualname__ if adapter is not None else "none",
                "machine": platform.machine(),
                "platform": platform.platform(),
                "python": sys.version,
                "target": self._target_payload(plan, stage),
            }
        )

    def _cache_key(
        self,
        plan: ExecutionPlan,
        stage: PlanStage,
        dag_stage: VerificationDagStage,
        identity: WorkspaceIdentity,
        receipts_by_stage: dict[str, StageReceipt],
    ) -> IterationCacheKey:
        _, _, workspace = self.ctx.workspace(plan.workspace_id)
        target_payload = self._target_payload(plan, stage)
        dependency_hashes = tuple(
            _stable_digest(receipt_payload(receipts_by_stage[dependency]))
            for dependency in dag_stage.dependencies
        )
        toolchain_hash = _stable_digest(
            {
                "machine": platform.machine(),
                "platform": platform.platform(),
                "python": sys.version,
                "target": target_payload,
            }
        )
        provider_hash = _stable_digest(to_data(self.ctx.config.providers))
        return build_iteration_cache_key(
            workspace_identity=identity.workspace_fingerprint,
            declared_input_hash=identity.workspace_fingerprint,
            stage_definition_hash=dag_stage.definition_hash,
            target_identity=_stable_digest(target_payload),
            working_directory=dag_stage.working_directory,
            environment_identity=self._environment_identity(plan, stage),
            toolchain_hash=toolchain_hash,
            lockfile_hash=_workspace_files_digest(
                workspace,
                ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
            ),
            config_generation=identity.config_generation,
            policy_hash=identity.policy_hash,
            provider_hash=provider_hash,
            network_policy=dag_stage.network_policy,
            dependency_receipt_hashes=dependency_hashes,
        )

    def _register_token(self, operation_id: str, token: CancellationToken) -> None:
        with self._tokens_lock:
            self._tokens[operation_id] = token

    def _unregister_token(self, operation_id: str) -> None:
        with self._tokens_lock:
            self._tokens.pop(operation_id, None)

    def request_live_cancel(self, operation_id: str) -> bool:
        with self._tokens_lock:
            token = self._tokens.get(operation_id)
        if token is None:
            return False
        token.cancel()
        return True

    def _check_cancelled(self, operation_id: str, token: CancellationToken) -> None:
        task = self.operations.status(operation_id)
        if task.cancellation_requested_at is not None:
            token.cancel()
        if token.is_cancelled():
            raise RepoForgeError(
                "Plan execution was cancelled before the next stage",
                code=ErrorCode.COMMAND_FAILED,
                details={"cancelled": True, "operation_id": operation_id},
            )

    def _execute_stage(
        self,
        operation_id: str,
        plan: ExecutionPlan,
        stage: PlanStage,
        token: CancellationToken,
    ) -> dict[str, object]:
        def before_command() -> None:
            self._check_cancelled(operation_id, token)

        if stage.kind is PlanStageKind.PROFILE:
            profile_result = self.profile_runner.execute(
                WorkspaceRunProfileCommand(
                    workspace_id=plan.workspace_id,
                    profile_name=stage.target,
                    background=False,
                    force_rerun=True,
                    cancellation_token=token,
                    before_command=before_command,
                )
            )
            data = to_data(profile_result)
            if not isinstance(data, dict):
                raise RepoForgeError(
                    "Profile result is not structured", code=ErrorCode.INTERNAL_ERROR
                )
            return data
        diagnostic_result = self.diagnostic_runner.execute(
            WorkspaceRunDiagnosticCommand(
                workspace_id=plan.workspace_id,
                diagnostic_id=stage.target,
                selector=stage.selector,
                expected_fingerprint=None,
                intent="final",
                expectation="pass",
                expected_failure_class=None,
                selector2=None,
                force_rerun=True,
                cancellation_token=token,
                before_command=before_command,
            )
        )
        data = to_data(diagnostic_result)
        if not isinstance(data, dict):
            raise RepoForgeError(
                "Diagnostic result is not structured", code=ErrorCode.INTERNAL_ERROR
            )
        if data.get("outcome") != "passed":
            raise CommandError(
                f"Diagnostic stage failed: {stage.target}",
                code=ErrorCode.COMMAND_FAILED,
                details={
                    "diagnostic_id": stage.target,
                    "failure_class": data.get("failure_class"),
                },
            )
        return data

    def _persist_receipt(
        self,
        *,
        operation_id: str,
        ordinal: int,
        plan: ExecutionPlan,
        stage: PlanStage,
        started_at: str,
        pre_identity: WorkspaceIdentity,
        status: StageReceiptStatus,
        failure_class: str | None,
        cache_status: StageCacheStatus = StageCacheStatus.NOT_CACHEABLE,
        result_reference: str | None = None,
        environment_identity: str | None = None,
        artifact_digests: tuple[ArtifactDigest, ...] | None = None,
    ) -> StageReceipt:
        post_identity = self._identity(plan.workspace_id)
        source_changed = pre_identity.workspace_fingerprint != post_identity.workspace_fingerprint
        reference = result_reference or f"stage-result-{operation_id.removeprefix('op-')}-{ordinal}"
        receipt = create_stage_receipt(
            operation_id=operation_id,
            ordinal=ordinal,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            workspace_id=plan.workspace_id,
            stage_id=stage.stage_id,
            kind=stage.kind.value,
            target=stage.target,
            boundary=stage.boundary.value,
            started_at=started_at,
            finished_at=self.ctx.clock.now_iso(),
            pre_identity=pre_identity,
            post_identity=post_identity,
            target_identity=stage.definition_hash,
            environment_identity=environment_identity,
            status=status,
            failure_class=failure_class,
            result_reference=reference,
            artifact_digests=(
                artifact_digests
                if artifact_digests is not None
                else self._artifact_digests(plan.workspace_id, stage)
            ),
            cache_status=cache_status,
            source_changed=source_changed,
        )
        return self._receipt_store().create(receipt).value

    def _run(
        self,
        operation_id: str,
        plan: ExecutionPlan,
        through: ExecutionBoundary,
        token: CancellationToken,
    ) -> WorkspaceExecutePlanResult:
        dag = compile_plan_dag(plan)
        dag_by_id = {stage.stage_id: stage for stage in dag.stages}
        selected = self._selected_stages(plan, through)
        receipts: list[StageReceipt] = []
        receipts_by_stage: dict[str, StageReceipt] = {}
        total = len(selected)
        _, _, workspace_root = self.ctx.workspace(plan.workspace_id)
        cache = self._cache_store()
        for ordinal, stage in enumerate(selected):
            self._check_cancelled(operation_id, token)
            self.plan_service.require_current(plan)
            self.operations.progress(
                operation_id,
                phase=f"stage-{ordinal + 1}",
                current=ordinal,
                total=total,
                unit="stages",
                message=f"Running accepted stage {ordinal + 1} of {total}: {stage.stage_id}",
            )
            started_at = self.ctx.clock.now_iso()
            pre_identity = self._identity(plan.workspace_id)
            dag_stage = dag_by_id[stage.stage_id]
            cacheable = dag_stage.cache_policy is CachePolicy.READ_ONLY and cache is not None
            cache_key: IterationCacheKey | None = None
            miss_reason: CacheMissReason | None = None
            environment_identity = self._environment_identity(plan, stage)
            if cacheable and cache is not None:
                cache_key = self._cache_key(
                    plan,
                    stage,
                    dag_stage,
                    pre_identity,
                    receipts_by_stage,
                )
                lookup = cache.lookup(cache_key, workspace_root=workspace_root)
                if lookup.hit and lookup.entry is not None:
                    receipt = self._persist_receipt(
                        operation_id=operation_id,
                        ordinal=ordinal,
                        plan=plan,
                        stage=stage,
                        started_at=started_at,
                        pre_identity=pre_identity,
                        status=StageReceiptStatus.SUCCEEDED,
                        failure_class=None,
                        cache_status=StageCacheStatus.HIT,
                        result_reference=(
                            f"cache-hit:{lookup.entry.entry_id}:{cache_key.cache_key[:16]}"
                        ),
                        environment_identity=environment_identity,
                        artifact_digests=lookup.entry.artifact_digests,
                    )
                    receipts.append(receipt)
                    receipts_by_stage[stage.stage_id] = receipt
                    self.operations.progress(
                        operation_id,
                        phase=f"stage-{ordinal + 1}",
                        current=ordinal + 1,
                        total=total,
                        unit="stages",
                        message=f"Reused compatible stage {ordinal + 1} of {total}: {stage.stage_id}",
                    )
                    continue
                miss_reason = lookup.reason or CacheMissReason.NOT_FOUND

            cache_status = StageCacheStatus.MISS if cacheable else StageCacheStatus.NOT_CACHEABLE
            reference = (
                f"stage-result-{operation_id.removeprefix('op-')}-{ordinal};"
                f"cache-miss:{miss_reason.value}"
                if miss_reason is not None
                else None
            )
            try:
                self._execute_stage(operation_id, plan, stage, token)
                receipt = self._persist_receipt(
                    operation_id=operation_id,
                    ordinal=ordinal,
                    plan=plan,
                    stage=stage,
                    started_at=started_at,
                    pre_identity=pre_identity,
                    status=StageReceiptStatus.SUCCEEDED,
                    failure_class=None,
                    cache_status=cache_status,
                    result_reference=reference,
                    environment_identity=environment_identity,
                )
                receipts.append(receipt)
                receipts_by_stage[stage.stage_id] = receipt
                if receipt.source_changed:
                    raise RepoForgeError(
                        "Plan stage changed the accepted workspace snapshot unexpectedly",
                        code=ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION,
                        details={
                            "stage_id": stage.stage_id,
                            "receipt_id": receipt.receipt_id,
                            "paths": list(
                                self.ctx.git.changed_paths(
                                    workspace_root, self.ctx.workspace(plan.workspace_id)[1]
                                )
                            ),
                        },
                    )
                if cacheable and cache is not None and cache_key is not None:
                    cache.put(
                        create_iteration_cache_entry(
                            key=cache_key,
                            source_receipt_id=receipt.receipt_id,
                            artifact_digests=receipt.artifact_digests,
                            created_at=receipt.finished_at,
                        )
                    )
            except Exception as exc:
                _, locked_repo, _ = self.ctx.workspace(plan.workspace_id)
                post_identity = self._identity(plan.workspace_id)
                prior_page = self._receipt_store().list_for_plan(plan.plan_id)
                prior_receipts = tuple(item.value for item in prior_page.records)
                evidence = self.failure_intelligence.build(
                    operation_id=operation_id,
                    plan=plan,
                    stage=stage,
                    exc=exc,
                    pre_identity=pre_identity,
                    post_identity=post_identity,
                    environment_identity=environment_identity,
                    changed_paths=tuple(self.ctx.git.changed_paths(workspace_root, locked_repo)),
                    prior_receipts=prior_receipts,
                )
                failed_receipt = self._persist_receipt(
                    operation_id=operation_id,
                    ordinal=ordinal,
                    plan=plan,
                    stage=stage,
                    started_at=started_at,
                    pre_identity=pre_identity,
                    status=(
                        StageReceiptStatus.CANCELLED
                        if token.is_cancelled()
                        else StageReceiptStatus.FAILED
                    ),
                    failure_class=evidence.failure_class.value,
                    cache_status=cache_status,
                    result_reference=f"failure:{evidence.failure_id}",
                    environment_identity=environment_identity,
                )
                stored_evidence = self.failure_intelligence.persist_for_workspace(
                    evidence,
                    receipt_id=failed_receipt.receipt_id,
                    workspace_id=plan.workspace_id,
                )
                receipts.append(failed_receipt)
                receipts_by_stage[stage.stage_id] = failed_receipt
                if isinstance(exc, RepoForgeError):
                    exc.details["failure_id"] = stored_evidence.failure_id
                    exc.details["failure_class"] = stored_evidence.failure_class.value
                    failure_to_raise: Exception = exc
                else:
                    failure_to_raise = RepoForgeError(
                        str(exc) or type(exc).__name__,
                        code=ErrorCode.COMMAND_FAILED,
                        details={
                            "failure_id": stored_evidence.failure_id,
                            "failure_class": stored_evidence.failure_class.value,
                        },
                    )
                if stage.failure_policy is StageFailurePolicy.OPTIONAL and not token.is_cancelled():
                    continue
                raise failure_to_raise from exc
            self.operations.progress(
                operation_id,
                phase=f"stage-{ordinal + 1}",
                current=ordinal + 1,
                total=total,
                unit="stages",
                message=f"Completed accepted stage {ordinal + 1} of {total}: {stage.stage_id}",
            )

        final_identity = self._identity(plan.workspace_id)
        satisfies = False
        if through == "full":
            record = self.ctx.store.load(plan.workspace_id)
            verification = record.last_verification
            satisfies = bool(
                verification is not None
                and verification.profile == plan.final_profile
                and verification.fingerprint == final_identity.workspace_fingerprint
            )
            if not satisfies:
                raise RepoForgeError(
                    "Full plan execution did not produce an exact current verification receipt",
                    code=ErrorCode.CHECK_EVIDENCE_UNAVAILABLE,
                )
        return WorkspaceExecutePlanResult(
            operation_id=operation_id,
            workspace_id=plan.workspace_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            through=through,
            stage_receipts=tuple(receipt_payload(receipt) for receipt in receipts),
            satisfies_commit_gate=satisfies,
            head_sha=final_identity.head_sha,
            workspace_fingerprint=final_identity.workspace_fingerprint,
        )

    def execute(self, command: WorkspaceExecutePlanCommand) -> WorkspaceExecutePlanAdmission:
        through = self._boundary(command.through)
        plan = self.plan_service.read_accepted(command.workspace_id, command.plan_id)
        self.plan_service.require_current(plan)
        selected = self._selected_stages(plan, through)
        task = self.operations.create(
            kind=_KIND,
            phase="queued",
            cancel_supported=True,
            task_id=plan.task_id,
            workspace_id=command.workspace_id,
            now=self.ctx.clock.now_iso(),
        )
        task = self.operations.start(task.operation_id, now=self.ctx.clock.now_iso())
        operation_id = task.operation_id
        token = CancellationToken()
        self._register_token(operation_id, token)

        def run() -> None:
            failure: Exception | None = None
            result: WorkspaceExecutePlanResult | None = None
            try:
                try:
                    result = self.ctx.audited(
                        _KIND,
                        {
                            "workspace_id": command.workspace_id,
                            "plan_id": command.plan_id,
                            "through": through,
                            "stage_count": len(selected),
                        },
                        lambda: self._run(operation_id, plan, through, token),
                        mutating=True,
                    )
                except Exception as exc:
                    failure = exc
            finally:
                self._unregister_token(operation_id)

            now = self.ctx.clock.now_iso()
            if failure is None and result is not None:
                result_store = self.ctx.operation_result_store
                if result_store is None:
                    self.operations.fail(
                        operation_id,
                        error_code=ErrorCode.STATE_PERSISTENCE_FAILED.value,
                        error_message="Operation result store is unavailable",
                        retryability=OperationRetryability.MANUAL,
                        now=now,
                    )
                    return
                try:
                    result_store.save(operation_id, to_data(result))
                    self.operations.succeed(
                        operation_id,
                        result_reference=f"{_KIND}:{operation_id}",
                        now=now,
                    )
                except Exception as persist_exc:
                    with contextlib.suppress(Exception):
                        result_store.delete(operation_id)
                    self.operations.fail(
                        operation_id,
                        error_code=ErrorCode.STATE_PERSISTENCE_FAILED.value,
                        error_message=_safe_error_message(persist_exc),
                        retryability=OperationRetryability.MANUAL,
                        now=now,
                    )
                return

            current = self.operations.status(operation_id)
            if current.cancellation_requested_at is not None or token.is_cancelled():
                self.operations.cancelled(operation_id, now=now)
                return
            final_error = failure or RepoForgeError(
                "Plan execution completed without a result",
                code=ErrorCode.INTERNAL_ERROR,
            )
            details = getattr(final_error, "details", None)
            failure_id = details.get("failure_id") if isinstance(details, dict) else None
            result_store = self.ctx.operation_result_store
            if isinstance(failure_id, str) and result_store is not None:
                result_store.save(
                    operation_id,
                    {
                        "failure_id": failure_id,
                        "failure_evidence_reference": f"failure:{failure_id}",
                    },
                )
            raw_code = getattr(getattr(final_error, "code", None), "value", None)
            error_code = str(raw_code or ErrorCode.INTERNAL_ERROR.value)
            self.operations.fail(
                operation_id,
                error_code=error_code,
                error_message=_safe_error_message(final_error),
                retryability=(
                    OperationRetryability.AUTOMATIC
                    if bool(getattr(final_error, "retryable", False))
                    else OperationRetryability.MANUAL
                ),
                now=now,
            )

        try:
            scheduled = self.background_tasks.submit(operation_id, run)
        except Exception as exc:
            self._unregister_token(operation_id)
            self.operations.fail(
                operation_id,
                error_code=ErrorCode.INTERNAL_ERROR.value,
                error_message=_safe_error_message(exc),
                now=self.ctx.clock.now_iso(),
            )
            raise
        if not scheduled:
            self._unregister_token(operation_id)
            self.operations.fail(
                operation_id,
                error_code=ErrorCode.INTERNAL_ERROR.value,
                error_message="Background task runner rejected plan execution",
                now=self.ctx.clock.now_iso(),
            )
            raise RepoForgeError(
                "Background task runner rejected plan execution",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return WorkspaceExecutePlanAdmission(
            operation_id=operation_id,
            phase="running",
            safe_next_action="Poll operation_status; cancellation is available while a stage owns a subprocess.",
        )

    def receipts(self, plan_id: str) -> WorkspaceExecutionReceiptsResult:
        page = self._receipt_store().list_for_plan(plan_id)
        return WorkspaceExecutionReceiptsResult(
            plan_id=plan_id,
            stage_receipts=tuple(receipt_payload(item.value) for item in page.records),
            truncated=page.scan_truncated,
        )
