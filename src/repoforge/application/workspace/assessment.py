"""Read-only snapshot-consistent workspace assessment orchestration."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar

from ...config import RepositoryConfig
from ...domain.assessment import (
    AssessmentCoverage,
    AssessmentEvidence,
    AssessmentEvidenceStatus,
    AssessmentSnapshot,
    WorkspaceAssessment,
    evidence,
    new_assessment_snapshot,
    validate_workspace_assessment,
)
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.policy import assert_path_allowed
from ..context import ApplicationContext, repository_policy_snapshot
from .base_status import WorkspaceBaseStatusCommand, WorkspaceBaseStatusReader
from .diff import WorkspaceDiffCommand, WorkspaceDiffReader
from .pr_checks import WorkspacePrChecksCommand, WorkspacePrChecksReader
from .pr_status import WorkspacePrStatusCommand, WorkspacePrStatusReader
from .status import WorkspaceStatusCommand, WorkspaceStatusReader

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WorkspaceAssessmentCommand:
    workspace_id: str


class WorkspaceAssessmentReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx
        self._status = WorkspaceStatusReader(ctx)
        self._diff = WorkspaceDiffReader(ctx)
        self._base = WorkspaceBaseStatusReader(ctx)
        self._pr = WorkspacePrStatusReader(ctx)
        self._checks = WorkspacePrChecksReader(ctx)

    def _config_generation(self) -> str:
        try:
            data = self.ctx.config.source_path.read_bytes()
        except OSError as exc:
            raise RepoForgeError(
                "Active configuration generation cannot be read",
                code=ErrorCode.ASSESSMENT_COMPONENT_UNAVAILABLE,
                retryable=True,
                safe_next_action="Restore the active configuration file and retry the assessment.",
            ) from exc
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _policy_hash(repo: RepositoryConfig) -> str:
        value = repository_policy_snapshot(repo).get("sha256")
        if not isinstance(value, str) or len(value) != 64:
            raise RepoForgeError(
                "Repository policy hash is unavailable",
                code=ErrorCode.ASSESSMENT_COMPONENT_UNAVAILABLE,
            )
        return value

    def _capture(
        self,
        workspace_id: str,
        repo: RepositoryConfig,
        path: Path,
    ) -> AssessmentSnapshot:
        created_at = self.ctx.clock.now_iso()
        return new_assessment_snapshot(
            workspace_id=workspace_id,
            head_sha=self.ctx.git.head_sha(path).lower(),
            workspace_fingerprint=self.ctx.git.fingerprint(path),
            config_generation=self._config_generation(),
            policy_hash=self._policy_hash(repo),
            created_at=created_at,
        )

    def _assert_current(
        self,
        snapshot: AssessmentSnapshot,
        repo: RepositoryConfig,
        path: Path,
    ) -> None:
        current = (
            self.ctx.git.head_sha(path).lower(),
            self.ctx.git.fingerprint(path),
            self._config_generation(),
            self._policy_hash(repo),
        )
        expected = (
            snapshot.head_sha,
            snapshot.workspace_fingerprint,
            snapshot.config_generation,
            snapshot.policy_hash,
        )
        if current != expected:
            raise RepoForgeError(
                "Workspace assessment identity changed during evidence collection",
                code=ErrorCode.STALE_ASSESSMENT_SNAPSHOT,
                retryable=True,
                safe_next_action="Discard the partial result and start a new assessment from current state.",
            )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        code = getattr(exc, "code", ErrorCode.ASSESSMENT_COMPONENT_UNAVAILABLE)
        value = getattr(code, "value", code)
        return str(value)[:128]

    def _collect(
        self,
        snapshot: AssessmentSnapshot,
        repo: RepositoryConfig,
        path: Path,
        provider: Callable[[], T],
        converter: Callable[[T], dict[str, Any]],
        *,
        fallback: str,
        not_applicable: bool = False,
    ) -> AssessmentEvidence:
        try:
            result = provider()
            component = evidence(
                snapshot,
                status=AssessmentEvidenceStatus.CURRENT,
                coverage=AssessmentCoverage.COMPLETE,
                value=converter(result),
            )
        except RepoForgeError as exc:
            component = evidence(
                snapshot,
                status=(
                    AssessmentEvidenceStatus.NOT_APPLICABLE
                    if not_applicable
                    else AssessmentEvidenceStatus.UNAVAILABLE
                ),
                coverage=AssessmentCoverage.NONE,
                error_code=self._error_code(exc),
                safe_fallback=fallback,
            )
        except Exception:
            component = evidence(
                snapshot,
                status=AssessmentEvidenceStatus.UNAVAILABLE,
                coverage=AssessmentCoverage.NONE,
                error_code=ErrorCode.ASSESSMENT_COMPONENT_UNAVAILABLE.value,
                safe_fallback=fallback,
            )
        self._assert_current(snapshot, repo, path)
        return component

    @staticmethod
    def _pr_value(result: Any) -> dict[str, Any]:
        payload = result.payload
        allowed = (
            "number",
            "url",
            "state",
            "isDraft",
            "mergeable",
            "reviewDecision",
            "headRefOid",
        )
        return {key: payload[key] for key in allowed if key in payload}

    @staticmethod
    def _ci_value(result: Any) -> dict[str, Any]:
        return {
            "summary": dict(sorted(result.summary.items())),
            "all_passed": result.all_passed,
            "pending": result.pending,
            "stale": result.stale,
            "head_sha": result.head_sha,
            "pushed_sha": result.pushed_sha,
        }

    @staticmethod
    def _failure_refs(result: Any) -> dict[str, Any]:
        selectors = sorted(
            {
                str(item["selector"])
                for item in result.checks
                if item.get("bucket") == "fail"
                and isinstance(item.get("selector"), str)
                and str(item["selector"]).startswith("check-run:")
            }
        )[:20]
        return {"selectors": selectors, "truncated": len(selectors) >= 20}

    def execute(self, command: WorkspaceAssessmentCommand) -> WorkspaceAssessment:
        record, repo, path = self.ctx.workspace(command.workspace_id)

        def operation() -> WorkspaceAssessment:
            snapshot = self._capture(command.workspace_id, repo, path)
            self._assert_current(snapshot, repo, path)

            status_result: Any | None = None
            try:
                status_result = self._status.execute(WorkspaceStatusCommand(command.workspace_id))
                changed_paths = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.CURRENT,
                    coverage=AssessmentCoverage.COMPLETE,
                    value={"paths": sorted(status_result.changed_paths)},
                )
                change_budget = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.CURRENT,
                    coverage=AssessmentCoverage.COMPLETE,
                    value=dict(status_result.change_metrics),
                )
                allowed_paths = [
                    assert_path_allowed(item, repo) for item in sorted(status_result.changed_paths)
                ]
                path_policy = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.CURRENT,
                    coverage=AssessmentCoverage.COMPLETE,
                    value={"allowed_paths": allowed_paths, "violations": []},
                )
                receipt_freshness = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.CURRENT,
                    coverage=AssessmentCoverage.COMPLETE,
                    value={"last_verification": status_result.last_verification},
                )
            except RepoForgeError as exc:
                status_failure = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.UNAVAILABLE,
                    coverage=AssessmentCoverage.NONE,
                    error_code=self._error_code(exc),
                    safe_fallback="Workspace-local status evidence is unavailable.",
                )
                changed_paths = status_failure
                change_budget = status_failure
                path_policy = status_failure
                receipt_freshness = status_failure
            self._assert_current(snapshot, repo, path)

            diff_summary = self._collect(
                snapshot,
                repo,
                path,
                lambda: self._diff.execute(WorkspaceDiffCommand(command.workspace_id)),
                lambda result: {
                    "stat": result.stat,
                    "truncated": result.truncated,
                    "untracked_paths": sorted(result.untracked_paths),
                },
                fallback="Bounded diff summary is unavailable.",
            )
            base_freshness = self._collect(
                snapshot,
                repo,
                path,
                lambda: self._base.execute(WorkspaceBaseStatusCommand(command.workspace_id)),
                lambda result: asdict(result),
                fallback="Base freshness is unknown; do not assume the workspace is current.",
            )
            pr_state = self._collect(
                snapshot,
                repo,
                path,
                lambda: self._pr.execute(WorkspacePrStatusCommand(command.workspace_id)),
                self._pr_value,
                fallback="No pull-request state is available; the branch may be unpublished or GitHub unavailable.",
                not_applicable=True,
            )
            ci_result: Any | None = None
            try:
                ci_result = self._checks.execute(
                    WorkspacePrChecksCommand(command.workspace_id, required_only=False)
                )
                ci_summary = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.CURRENT,
                    coverage=AssessmentCoverage.COMPLETE,
                    value=self._ci_value(ci_result),
                )
                failure_evidence_refs = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.CURRENT,
                    coverage=AssessmentCoverage.COMPLETE,
                    value=self._failure_refs(ci_result),
                )
            except RepoForgeError as exc:
                ci_summary = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.UNAVAILABLE,
                    coverage=AssessmentCoverage.NONE,
                    error_code=self._error_code(exc),
                    safe_fallback="CI state is unavailable; do not infer success.",
                )
                failure_evidence_refs = evidence(
                    snapshot,
                    status=AssessmentEvidenceStatus.UNAVAILABLE,
                    coverage=AssessmentCoverage.NONE,
                    error_code=self._error_code(exc),
                    safe_fallback="No failure-evidence references can be trusted.",
                )
            self._assert_current(snapshot, repo, path)

            components = {
                "changed_paths": changed_paths,
                "diff_summary": diff_summary,
                "change_budget": change_budget,
                "path_policy": path_policy,
                "base_freshness": base_freshness,
                "pr_state": pr_state,
                "ci_summary": ci_summary,
                "failure_evidence_refs": failure_evidence_refs,
                "receipt_freshness": receipt_freshness,
            }
            coverage = {name: item.coverage.value for name, item in sorted(components.items())}
            uncertainties = tuple(
                sorted(
                    f"{name}:{item.error_code or item.status.value}"
                    for name, item in components.items()
                    if item.status is not AssessmentEvidenceStatus.CURRENT
                )
            )
            self._assert_current(snapshot, repo, path)
            return validate_workspace_assessment(
                WorkspaceAssessment(
                    snapshot=snapshot,
                    changed_paths=changed_paths,
                    diff_summary=diff_summary,
                    change_budget=change_budget,
                    path_policy=path_policy,
                    base_freshness=base_freshness,
                    pr_state=pr_state,
                    ci_summary=ci_summary,
                    failure_evidence_refs=failure_evidence_refs,
                    receipt_freshness=receipt_freshness,
                    evidence_coverage=coverage,
                    uncertainties=uncertainties,
                )
            )

        return self.ctx.audited(
            "workspace_assessment",
            {"workspace_id": record.workspace_id},
            operation,
            mutating=False,
        )
