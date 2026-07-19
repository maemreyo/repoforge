"""Read baseline-aware hygiene evidence for one exact workspace snapshot."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.errors import ConfigError
from ...domain.execution_environment import ExecutionEvidence
from ...domain.hygiene import compare_hygiene_findings
from ...ports.hygiene import HygieneCacheKey
from ..context import ApplicationContext
from ..fingerprint_cache import read_fingerprint
from .hygiene_common import (
    base_policy_paths,
    config_identity,
    finding_data,
    select_formatter,
    workspace_base_sha,
    workspace_policy_paths,
)


@dataclass(frozen=True, slots=True)
class WorkspaceHygieneStatusCommand:
    workspace_id: str
    formatter_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceHygieneStatusResult:
    workspace_id: str
    status: str
    formatter_id: str | None
    available_formatters: list[str]
    reason: str | None
    base_sha: str | None
    head_sha: str
    workspace_fingerprint: str
    formatter_contract_hash: str | None
    environment_identity: str | None
    base_cache_hit: bool
    preexisting: list[dict[str, str]]
    introduced: list[dict[str, str]]
    resolved: list[dict[str, str]]
    changed_path_findings: list[dict[str, str]]
    output_truncated: bool
    next_safe_actions: list[dict[str, object]]
    execution_evidence: ExecutionEvidence | None = None


class WorkspaceHygieneStatusReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceHygieneStatusCommand) -> WorkspaceHygieneStatusResult:
        details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "formatter_id": command.formatter_id,
        }

        def operation() -> WorkspaceHygieneStatusResult:
            result = self._read(command)
            details.update(
                {
                    "status": result.status,
                    "base_cache_hit": result.base_cache_hit,
                    "preexisting_count": len(result.preexisting),
                    "introduced_count": len(result.introduced),
                    "resolved_count": len(result.resolved),
                    "changed_path_finding_count": len(result.changed_path_findings),
                    "formatter_contract_hash": result.formatter_contract_hash,
                }
            )
            return result

        return self.ctx.audited("workspace_hygiene_status", details, operation)

    def compute(self, command: WorkspaceHygieneStatusCommand) -> WorkspaceHygieneStatusResult:
        """Read hygiene evidence without a nested audit event."""
        return self._read(command)

    def _read(self, command: WorkspaceHygieneStatusCommand) -> WorkspaceHygieneStatusResult:
        record, repo, workspace = self.ctx.workspace(command.workspace_id)
        policy = select_formatter(repo, command.formatter_id, allow_unavailable=True)
        fingerprint = read_fingerprint(
            self.ctx.fingerprint_cache,
            command.workspace_id,
            self.ctx.git,
            workspace,
        ).fingerprint
        head_sha = self.ctx.git.head_sha(workspace)
        if policy is None:
            reason = (
                "no_reviewed_formatter" if not repo.formatters else "formatter_selection_required"
            )
            return WorkspaceHygieneStatusResult(
                workspace_id=command.workspace_id,
                status="unavailable",
                formatter_id=None,
                available_formatters=sorted(repo.formatters),
                reason=reason,
                base_sha=None,
                head_sha=head_sha,
                workspace_fingerprint=fingerprint,
                formatter_contract_hash=None,
                environment_identity=None,
                base_cache_hit=False,
                preexisting=[],
                introduced=[],
                resolved=[],
                changed_path_findings=[],
                output_truncated=False,
                next_safe_actions=[
                    {
                        "action": "propose_formatter_policy"
                        if not repo.formatters
                        else "select_formatter",
                        "reason": (
                            "Add a reviewed formatter with fixed check/fix argv and explicit bounds."
                            if not repo.formatters
                            else "Choose one formatter_id from available_formatters."
                        ),
                        "required": True,
                    }
                ],
            )
        if self.ctx.hygiene is None or self.ctx.hygiene_cache is None:
            raise ConfigError("Hygiene adapters are unavailable in the active application")

        base_sha = workspace_base_sha(self.ctx, workspace, repo, record.base)
        workspace_paths = workspace_policy_paths(self.ctx, workspace, repo, policy)
        base_paths = base_policy_paths(self.ctx, workspace, repo, base_sha, policy)
        workspace_inspection = self.ctx.hygiene.inspect_workspace(
            workspace,
            policy,
            workspace_paths,
        )
        key = HygieneCacheKey(
            repo_id=repo.repo_id,
            base_sha=base_sha,
            config_identity=config_identity(self.ctx),
            environment_identity=workspace_inspection.environment_identity,
            formatter_contract_hash=policy.contract_hash,
            ttl_seconds=policy.baseline_cache_ttl_seconds,
        )
        cached = self.ctx.hygiene_cache.get(key, now_epoch=self.ctx.now_epoch())
        cache_hit = cached is not None
        output_truncated = workspace_inspection.output_truncated
        if cached is None:
            base_inspection = self.ctx.hygiene.inspect_base(
                workspace,
                base_sha,
                policy,
                base_paths,
                max_archive_bytes=self.ctx.config.server.max_fingerprint_bytes,
            )
            base_findings = base_inspection.findings
            output_truncated = output_truncated or base_inspection.output_truncated
            actual_key = HygieneCacheKey(
                repo_id=repo.repo_id,
                base_sha=base_sha,
                config_identity=key.config_identity,
                environment_identity=base_inspection.environment_identity,
                formatter_contract_hash=policy.contract_hash,
                ttl_seconds=policy.baseline_cache_ttl_seconds,
            )
            self.ctx.hygiene_cache.put(
                actual_key,
                base_findings,
                now_epoch=self.ctx.now_epoch(),
            )
        else:
            base_findings = cached
        changed_paths = tuple(self.ctx.git.changed_paths(workspace, repo))
        comparison = compare_hygiene_findings(
            base=base_findings,
            workspace=workspace_inspection.findings,
            changed_paths=changed_paths,
        )
        next_actions: list[dict[str, object]] = []
        if comparison.changed_path_findings:
            next_actions.append(
                {
                    "action": "workspace_format_changed",
                    "reason": "Changed paths have formatter findings eligible for constrained remediation.",
                    "required": False,
                }
            )
        if comparison.introduced and not comparison.changed_path_findings:
            next_actions.append(
                {
                    "action": "review_hygiene_scope",
                    "reason": "Introduced findings are outside the current changed-path remediation scope.",
                    "required": True,
                }
            )
        return WorkspaceHygieneStatusResult(
            workspace_id=command.workspace_id,
            status="available",
            formatter_id=policy.formatter_id,
            available_formatters=sorted(repo.formatters),
            reason=None,
            base_sha=base_sha,
            head_sha=head_sha,
            workspace_fingerprint=fingerprint,
            formatter_contract_hash=policy.contract_hash,
            environment_identity=workspace_inspection.environment_identity,
            base_cache_hit=cache_hit,
            preexisting=[finding_data(item) for item in comparison.preexisting],
            introduced=[finding_data(item) for item in comparison.introduced],
            resolved=[finding_data(item) for item in comparison.resolved],
            changed_path_findings=[finding_data(item) for item in comparison.changed_path_findings],
            output_truncated=output_truncated,
            next_safe_actions=next_actions,
            execution_evidence=workspace_inspection.execution_evidence,
        )
