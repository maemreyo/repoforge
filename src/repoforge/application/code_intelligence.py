"""Snapshot-bound orchestration for provider-neutral code-intelligence evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceResult,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
    new_code_intelligence_result,
    unavailable_code_intelligence,
)
from ..domain.errors import ErrorCode, RepoForgeError, SecurityError
from ..domain.evidence import EvidenceItem, new_code_intelligence_evidence
from ..domain.policy import assert_path_allowed
from .context import ApplicationContext

_MAX_PROVIDER_PATHS = 2_000


@dataclass(frozen=True, slots=True)
class CodeIntelligenceCommand:
    workspace_id: str
    expected_head_sha: str | None = None
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class CodeIntelligenceAnalysis:
    result: CodeIntelligenceResult
    evidence: EvidenceItem | None


def _provider_identity(provider: object | None) -> tuple[str, str]:
    provider_id = getattr(provider, "provider_id", "unavailable")
    provider_version = getattr(provider, "provider_version", "0")
    return (
        provider_id if isinstance(provider_id, str) and provider_id else "unavailable",
        provider_version if isinstance(provider_version, str) and provider_version else "0",
    )


def _with_listing_limitation(
    result: CodeIntelligenceResult,
    *,
    listing_truncated: bool,
) -> CodeIntelligenceResult:
    if not listing_truncated or result.status is CodeIntelligenceStatus.UNAVAILABLE:
        return result
    limitation = (
        "Repository file listing reached its reviewed bound before every path was considered."
    )
    return new_code_intelligence_result(
        provider_id=result.provider_id,
        provider_version=result.provider_version,
        snapshot=result.snapshot,
        status=CodeIntelligenceStatus.PARTIAL,
        coverage=CodeIntelligenceMeasure(
            min(result.coverage.value, 99),
            f"{result.coverage.reason} The repository file listing was truncated.",
        ),
        confidence=result.confidence,
        analyzed_paths=result.analyzed_paths,
        symbols=result.symbols,
        imports=result.imports,
        references=result.references,
        affected_tests=result.affected_tests,
        unsupported_paths=result.unsupported_paths,
        malformed_paths=result.malformed_paths,
        generated_paths=result.generated_paths,
        denied_paths=result.denied_paths,
        limitations=tuple((*result.limitations, limitation)),
        truncated=True,
    )


class CodeIntelligenceAnalyzer:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx

    def analyze_current(self, command: CodeIntelligenceCommand) -> CodeIntelligenceAnalysis:
        _record, repo, workspace = self.ctx.workspace(command.workspace_id)
        head_sha = self.ctx.git.head_sha(workspace).lower()
        fingerprint = self.ctx.git.fingerprint(workspace)
        if command.expected_head_sha is not None and command.expected_head_sha != head_sha:
            raise RepoForgeError(
                "Code-intelligence HEAD no longer matches the reviewed snapshot",
                code=ErrorCode.CODE_INTELLIGENCE_STALE,
                retryable=True,
            )
        if command.expected_fingerprint is not None and command.expected_fingerprint != fingerprint:
            raise RepoForgeError(
                "Code-intelligence workspace fingerprint no longer matches the reviewed snapshot",
                code=ErrorCode.CODE_INTELLIGENCE_STALE,
                retryable=True,
            )
        snapshot = CodeIntelligenceSnapshot(
            repo_id=repo.repo_id,
            workspace_id=command.workspace_id,
            head_sha=head_sha,
            workspace_fingerprint=fingerprint,
        )
        paths, listing_truncated = self.ctx.git.list_files(
            workspace,
            repo,
            _MAX_PROVIDER_PATHS,
        )
        allowed_paths: list[str] = []
        denied_paths: list[str] = []
        for relative_path in paths:
            try:
                allowed_paths.append(assert_path_allowed(relative_path, repo))
            except SecurityError:
                denied_paths.append(relative_path.replace("\\", "/"))
        changed_paths = tuple(
            sorted(
                {
                    assert_path_allowed(relative_path, repo)
                    for relative_path in self.ctx.git.changed_paths(workspace, repo)
                }
            )
        )
        request = CodeIntelligenceRequest(
            workspace_root=workspace,
            snapshot=snapshot,
            paths=tuple(allowed_paths),
            changed_paths=changed_paths,
            diagnostic_ids=tuple(sorted(repo.diagnostics)),
            denied_paths=tuple(denied_paths),
        )
        provider = self.ctx.code_intelligence
        provider_id, provider_version = _provider_identity(provider)
        if provider is None:
            result = unavailable_code_intelligence(
                snapshot=snapshot,
                reason="No code-intelligence provider is configured.",
            )
        else:
            try:
                result = provider.analyze(request)
                if result.snapshot != snapshot:
                    raise RepoForgeError(
                        "Code-intelligence provider returned evidence for a different snapshot",
                        code=ErrorCode.CODE_INTELLIGENCE_INVALID,
                    )
            except Exception as exc:
                result = unavailable_code_intelligence(
                    snapshot=snapshot,
                    provider_id=provider_id,
                    provider_version=provider_version,
                    reason=(
                        f"Provider {provider_id} could not return bounded evidence: "
                        f"{type(exc).__name__}."
                    ),
                )
        result = _with_listing_limitation(result, listing_truncated=listing_truncated)
        current_head = self.ctx.git.head_sha(workspace).lower()
        current_fingerprint = self.ctx.git.fingerprint(workspace)
        if current_head != head_sha or current_fingerprint != fingerprint:
            raise RepoForgeError(
                "Workspace identity changed while code-intelligence evidence was being collected",
                code=ErrorCode.CODE_INTELLIGENCE_STALE,
                retryable=True,
                safe_next_action="Discard the result and rerun analysis on the current workspace snapshot.",
            )
        evidence = new_code_intelligence_evidence(result, created_at=self.ctx.clock.now_iso())
        return CodeIntelligenceAnalysis(result, evidence)

    def execute(self, command: CodeIntelligenceCommand) -> CodeIntelligenceAnalysis:
        details: dict[str, Any] = {"workspace_id": command.workspace_id}
        return self.ctx.audited(
            "workspace_code_intelligence",
            details,
            lambda: self.analyze_current(command),
            mutating=False,
        )


__all__ = [
    "CodeIntelligenceAnalysis",
    "CodeIntelligenceAnalyzer",
    "CodeIntelligenceCommand",
]
