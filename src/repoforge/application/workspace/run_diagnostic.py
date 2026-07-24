"""Run one repository-reviewed diagnostic against an exact workspace state."""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from ...domain.command_source import derive_command_source_paths
from ...domain.diagnostics import (
    DiagnosticExpectation,
    DiagnosticFailureClass,
    DiagnosticMutability,
    DiagnosticProfileConfig,
    validate_diagnostic_expectation,
)
from ...domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.execution_environment import build_execution_evidence
from ...domain.failed_selectors import (
    FailedSelectorRecord,
    clear_failed_selectors,
    read_failed_selectors,
    record_failed_selectors,
)
from ...domain.policy import normalize_relative_path
from ...domain.retry_guidance import (
    clear_reusable_failure,
    record_reusable_failure,
    reusable_failure,
)
from ...domain.verification import VerificationIntent
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from ..context import ApplicationContext
from ..dto import to_data
from ..execution.requests import diagnostic_execution_request
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from ..verification_reuse import (
    command_source_identity,
    config_identity,
    diagnostic_rerun_target_identity,
    diagnostic_target_identity,
    failure_reuse_binding,
)
from .diagnostic_parser import evaluate_diagnostic_expectation, parse_diagnostic
from .diagnostic_selector import SelectorInput, resolve_diagnostic_selector

_REUSABLE_DIAGNOSTIC_FAILURES = frozenset(
    {
        DiagnosticFailureClass.COLLECTION_ERROR.value,
        DiagnosticFailureClass.SYNTAX_ERROR.value,
        DiagnosticFailureClass.IMPORT_ERROR.value,
        DiagnosticFailureClass.DEPENDENCY_MISSING.value,
        DiagnosticFailureClass.CONTRACT_DRIFT.value,
        DiagnosticFailureClass.DIAGNOSTIC_FAILURE.value,
    }
)


@dataclass(frozen=True, slots=True)
class WorkspaceRunDiagnosticCommand:
    workspace_id: str
    diagnostic_id: str
    selector: SelectorInput = None
    expected_fingerprint: str | None = None
    intent: VerificationIntent | str | None = None
    expectation: DiagnosticExpectation | str | None = None
    expected_failure_class: DiagnosticFailureClass | str | None = None
    selector2: SelectorInput = None
    force_rerun: bool = False
    rerun_failed: bool = False
    cancellation_token: CancellationToken | None = None
    before_command: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRunDiagnosticResult:
    workspace_id: str
    diagnostic_id: str
    summary: str
    selector_kind: str
    resolved_selector: str | None
    resolved_selectors: dict[str, list[str]]
    argv: list[str]
    working_directory: str
    network_policy: str
    mutability: str
    parser: str
    intent: str
    expectation: str
    expected_failure_class: str | None
    returncode: int
    outcome: str
    failure_class: str | None
    expectation_met: bool | None
    business_tests_ran: bool
    valid_tdd_red_evidence: bool
    parsed: dict[str, int | str]
    excerpt: str
    output_truncated: bool
    fingerprint_before: str
    fingerprint_after: str
    fingerprint_changed: bool
    head_sha: str
    changed_paths: list[str]
    unexpected_paths: list[str]
    change_metrics: dict[str, Any]
    verification_invalidated: bool
    satisfies_commit_gate: bool
    next_safe_actions: list[dict[str, object]]
    failure_reused: bool = False
    reuse_binding: str | None = None
    execution_evidence: dict[str, object] = field(default_factory=dict)
    failed_selectors: list[str] = field(default_factory=list)
    output_artifact_reference: str | None = None
    failure_provider: str | None = None
    selector_coverage: str = "not_applicable"
    selectors_unavailable_reason: str | None = None
    failure_locations: list[dict[str, object]] = field(default_factory=list)
    output_artifact_status: str = "not_applicable"
    failure_expectation: str | None = None
    failure_chain_id: str | None = None
    rerun_of_selectors: list[str] = field(default_factory=list)


def _diagnostic_error(
    message: str,
    code: ErrorCode,
    *,
    retryable: bool = False,
    mutation_possible: bool = False,
) -> RepoForgeError:
    unchanged = (
        (
            "No configuration generation, commit, or remote state changed; workspace paths named in the error may have changed.",
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
            else "Refresh workspace status and retry the same reviewed diagnostic after correcting the reported condition."
        ),
    )


def _profile(repo: object, diagnostic_id: str) -> DiagnosticProfileConfig:
    diagnostics = getattr(repo, "diagnostics", None)
    if not isinstance(diagnostics, dict) or diagnostic_id not in diagnostics:
        raise _diagnostic_error(
            f"Unknown reviewed diagnostic: {diagnostic_id}",
            ErrorCode.DIAGNOSTIC_NOT_FOUND,
        )
    profile = diagnostics[diagnostic_id]
    if not isinstance(profile, DiagnosticProfileConfig):
        raise _diagnostic_error(
            f"Diagnostic profile is not typed: {diagnostic_id}",
            ErrorCode.DIAGNOSTIC_OUTPUT_INVALID,
        )
    return profile


def _command_cwd(workspace: Path, profile: DiagnosticProfileConfig) -> Path:
    if profile.working_directory is None:
        return workspace
    relative = normalize_relative_path(profile.working_directory)
    unresolved = workspace / relative
    if unresolved.is_symlink():
        raise SecurityError("Diagnostic working_directory cannot be a symlink")
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(workspace.resolve(strict=True))
    except ValueError as exc:
        raise SecurityError("Diagnostic working_directory escapes workspace") from exc
    if not candidate.is_dir():
        raise WorkspaceError(
            f"Diagnostic working_directory does not exist: {profile.working_directory}"
        )
    return candidate


def _file_digest(workspace: Path, relative_path: str) -> str:
    candidate = workspace / relative_path
    if not candidate.exists() and not candidate.is_symlink():
        return "<missing>"
    if candidate.is_symlink():
        return "<symlink>"
    if not candidate.is_file():
        return "<non-regular>"
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_state(workspace: Path, paths: list[str]) -> dict[str, str]:
    return {path: _file_digest(workspace, path) for path in paths}


def _matches_artifact(path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)
        for pattern in patterns
    )


class WorkspaceDiagnosticRunner:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceRunDiagnosticCommand) -> WorkspaceRunDiagnosticResult:
        _, repo, _ = self.ctx.workspace(command.workspace_id)
        profile = _profile(repo, command.diagnostic_id)
        intent = VerificationIntent.parse(command.intent)
        expectation, expected_failure_class = validate_diagnostic_expectation(
            command.expectation,
            command.expected_failure_class,
        )

        audit_details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "diagnostic_id": command.diagnostic_id,
            "selector_kind": profile.selector.kind.value,
            "mutability": profile.mutability.value,
            "parser": profile.parser.value,
            "intent": intent.value,
            "expectation": expectation.value,
            "expected_failure_class": (
                expected_failure_class.value if expected_failure_class is not None else None
            ),
            "force_rerun": command.force_rerun,
            "rerun_failed": command.rerun_failed,
        }

        def operation() -> WorkspaceRunDiagnosticResult:
            with self.ctx.locks.lock(command.workspace_id):
                fresh, locked_repo, locked_workspace = self.ctx.workspace(command.workspace_id)
                locked_profile = _profile(locked_repo, command.diagnostic_id)
                command_cwd = _command_cwd(locked_workspace, locked_profile)
                before_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                before_states = _path_state(locked_workspace, before_paths)
                before = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    locked_workspace,
                )
                before_fingerprint = before.fingerprint
                audit_details.update(
                    {
                        "fingerprint_source": before.source,
                        "fingerprint_duration_ms": before.duration_ms,
                    }
                )
                if (
                    command.expected_fingerprint is not None
                    and command.expected_fingerprint != before_fingerprint
                ):
                    raise _diagnostic_error(
                        "Workspace changed since the diagnostic request was reviewed: "
                        f"expected {command.expected_fingerprint}, current {before_fingerprint}",
                        ErrorCode.DIAGNOSTIC_STALE_WORKSPACE,
                        retryable=True,
                    )
                diagnostic_target = f"diagnostic:{locked_profile.diagnostic_id}"
                rerun_record: FailedSelectorRecord | None = None
                effective_selector = command.selector
                effective_selector2 = command.selector2
                if command.rerun_failed:
                    rerun_record = read_failed_selectors(
                        fresh.metadata,
                        target=diagnostic_target,
                    )
                    if rerun_record is None:
                        raise _diagnostic_error(
                            "No complete failed-selector set is available for this diagnostic",
                            ErrorCode.STATE_NOT_FOUND,
                        )
                    if rerun_record.fingerprint != before_fingerprint:
                        raise _diagnostic_error(
                            "Workspace changed since the failed selector set was recorded: "
                            f"expected {rerun_record.fingerprint}, current {before_fingerprint}",
                            ErrorCode.DIAGNOSTIC_STALE_WORKSPACE,
                            retryable=True,
                        )
                    effective_selector = list(rerun_record.selectors)
                    effective_selector2 = None
                    audit_details.update(
                        {
                            "failure_chain_id": rerun_record.chain_id,
                            "rerun_selector_count": len(rerun_record.selectors),
                        }
                    )
                resolved = resolve_diagnostic_selector(
                    locked_profile,
                    effective_selector,
                    effective_selector2,
                    workspace=locked_workspace,
                    repo=locked_repo,
                    git=self.ctx.git,
                )
                expected_failure_value = (
                    expected_failure_class.value if expected_failure_class is not None else None
                )
                target_identity_value = diagnostic_target_identity(
                    locked_profile,
                    argv=resolved.argv,
                    resolved_values=resolved.values,
                    intent=intent.value,
                    expectation=expectation.value,
                    expected_failure_class=expected_failure_value,
                )
                execution_request = diagnostic_execution_request(
                    workspace_id=command.workspace_id,
                    workspace_root=locked_workspace,
                    command_cwd=command_cwd,
                    argv=resolved.argv,
                    working_directory_policy=locked_profile.working_directory or ".",
                    timeout_seconds=locked_profile.timeout_seconds,
                    output_limit=locked_profile.output_limit,
                    read_only=locked_profile.mutability is DiagnosticMutability.READ_ONLY,
                    artifact_paths=locked_profile.artifact_paths,
                    cancel_token=command.cancellation_token,
                )
                environment_identity_value: str | None = None
                try:
                    environment_identity_value = self.ctx.execution.inspect(
                        execution_request
                    ).identity.identity_hash
                except Exception:
                    audit_details["failure_reuse_unavailable"] = "environment_identity"
                config_identity_value = config_identity(self.ctx.config.source_path)
                reuse_binding = failure_reuse_binding(
                    fingerprint=before_fingerprint,
                    target_identity=target_identity_value,
                    command_source_identity_value=command_source_identity(
                        locked_workspace,
                        derive_command_source_paths((resolved.argv,)),
                    ),
                    config_identity_value=config_identity_value,
                    environment_identity=environment_identity_value,
                )
                rerun_binding = failure_reuse_binding(
                    fingerprint=before_fingerprint,
                    target_identity=diagnostic_rerun_target_identity(
                        locked_profile,
                        intent=intent.value,
                        expectation=expectation.value,
                        expected_failure_class=expected_failure_value,
                    ),
                    command_source_identity_value=command_source_identity(
                        locked_workspace,
                        derive_command_source_paths((locked_profile.argv_template,)),
                    ),
                    config_identity_value=config_identity_value,
                    environment_identity=environment_identity_value,
                )
                if rerun_record is not None:
                    if rerun_record.binding is None:
                        raise _diagnostic_error(
                            "The failed-selector record predates exact compatibility binding",
                            ErrorCode.DIAGNOSTIC_STALE_WORKSPACE,
                            retryable=True,
                        )
                    if rerun_binding is None or rerun_record.binding != rerun_binding:
                        raise _diagnostic_error(
                            "Failed-selector compatibility binding changed since the original failure",
                            ErrorCode.DIAGNOSTIC_STALE_WORKSPACE,
                            retryable=True,
                        )
                if (
                    not command.force_rerun
                    and not command.rerun_failed
                    and reuse_binding is not None
                ):
                    cached = reusable_failure(
                        fresh.metadata,
                        target=f"diagnostic:{locked_profile.diagnostic_id}",
                        binding=reuse_binding,
                    )
                    raw_result = cached.get("result") if cached is not None else None
                    if isinstance(raw_result, dict):
                        try:
                            replayed = WorkspaceRunDiagnosticResult(**raw_result)
                        except (TypeError, ValueError):
                            clear_reusable_failure(
                                fresh.metadata,
                                target=f"diagnostic:{locked_profile.diagnostic_id}",
                            )
                            self.ctx.store.save(fresh)
                        else:
                            audit_details.update(
                                {
                                    "failure_reused": True,
                                    "reuse_binding": reuse_binding.digest,
                                }
                            )
                            return replace(
                                replayed,
                                failure_reused=True,
                                reuse_binding=reuse_binding.digest,
                            )

                result: CommandResult | None = None
                command_error: CommandError | None = None
                execution_evidence_data: dict[str, object] = {}
                try:
                    if command.before_command is not None:
                        command.before_command()
                    with self.ctx.execution.prepare(execution_request) as session:
                        result = session.execute(resolved.argv).result
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

                try:
                    after_paths = self.ctx.git.changed_paths(locked_workspace, locked_repo)
                except SecurityError as exc:
                    after = prime_fingerprint(
                        self.ctx.fingerprint_cache,
                        command.workspace_id,
                        self.ctx.git,
                        locked_workspace,
                    )
                    after_fingerprint = after.fingerprint
                    audit_details["post_mutation_fingerprint_duration_ms"] = after.duration_ms
                    if (
                        fresh.last_verification is not None
                        and after_fingerprint != before_fingerprint
                    ):
                        fresh.last_verification = None
                        self.ctx.store.save(fresh)
                    raise _diagnostic_error(
                        f"Diagnostic changed a path rejected by repository policy: {exc}",
                        ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION,
                        mutation_possible=True,
                    ) from exc
                after_states = _path_state(locked_workspace, after_paths)
                after = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    locked_workspace,
                )
                after_fingerprint = after.fingerprint
                audit_details["post_mutation_fingerprint_duration_ms"] = after.duration_ms
                fingerprint_changed = after_fingerprint != before_fingerprint
                touched_paths = sorted(
                    path
                    for path in set(before_states) | set(after_states)
                    if before_states.get(path) != after_states.get(path)
                )
                verification_invalidated = False
                if fingerprint_changed and fresh.last_verification is not None:
                    fresh.last_verification = None
                    self.ctx.store.save(fresh)
                    verification_invalidated = True

                unexpected_paths: list[str] = []
                if fingerprint_changed:
                    if locked_profile.mutability is DiagnosticMutability.READ_ONLY:
                        unexpected_paths = touched_paths or sorted(after_paths)
                    else:
                        unexpected_paths = [
                            path
                            for path in touched_paths
                            if not _matches_artifact(path, locked_profile.artifact_paths)
                        ]
                if unexpected_paths:
                    raise _diagnostic_error(
                        "Diagnostic changed paths outside its reviewed mutability contract: "
                        + ", ".join(unexpected_paths),
                        ErrorCode.DIAGNOSTIC_UNEXPECTED_MUTATION,
                        mutation_possible=True,
                    )

                if command_error is not None:
                    rendered = str(command_error)
                    lowered = rendered.lower()
                    if "executable not found" in lowered:
                        code = ErrorCode.DIAGNOSTIC_TOOL_MISSING
                    elif "timed out" in lowered or "timeout" in lowered:
                        code = ErrorCode.DIAGNOSTIC_TIMEOUT
                    else:
                        code = ErrorCode.DIAGNOSTIC_OUTPUT_INVALID
                    raise _diagnostic_error(
                        rendered, code, retryable=code is ErrorCode.DIAGNOSTIC_TIMEOUT
                    )
                assert result is not None
                parsed = parse_diagnostic(locked_profile, result)
                evaluation = evaluate_diagnostic_expectation(
                    parsed,
                    intent=intent,
                    expectation=expectation,
                    expected_failure_class=expected_failure_class,
                )
                metrics = self.ctx.git.change_metrics(locked_workspace, locked_repo)
                next_actions: list[dict[str, object]] = []
                if fingerprint_changed:
                    next_actions.append(
                        {
                            "action": "workspace_status",
                            "reason": "The diagnostic changed the workspace fingerprint.",
                            "required": True,
                        }
                    )
                if evaluation.valid_tdd_red_evidence:
                    next_actions.append(
                        {
                            "action": "implement_business_logic",
                            "reason": "The selected business test produced valid expected RED evidence.",
                            "required": True,
                        }
                    )
                elif evaluation.expectation_met is False or parsed.outcome != "passed":
                    next_actions.append(
                        {
                            "action": "review_diagnostic_failure",
                            "reason": parsed.failure_class
                            or "The diagnostic result did not meet the reviewed expectation.",
                            "required": True,
                        }
                    )
                elif intent is VerificationIntent.TDD_GREEN:
                    next_actions.append(
                        {
                            "action": "continue_refactor_or_hygiene",
                            "reason": "The selected business test is GREEN on the current fingerprint.",
                            "required": False,
                        }
                    )
                failure_expectation = (
                    "expected_red"
                    if parsed.outcome == "failed" and evaluation.valid_tdd_red_evidence
                    else "unexpected"
                    if parsed.outcome == "failed"
                    else None
                )
                selector_record = rerun_record
                selector_metadata_changed = False
                if (
                    parsed.outcome == "failed"
                    and parsed.failed_selectors
                    and not fingerprint_changed
                    and rerun_binding is not None
                ):
                    selector_record = record_failed_selectors(
                        fresh.metadata,
                        target=diagnostic_target,
                        fingerprint=before_fingerprint,
                        selectors=parsed.failed_selectors,
                        binding=rerun_binding,
                        chain_id=rerun_record.chain_id if rerun_record is not None else None,
                    )
                    selector_metadata_changed = selector_record is not None
                else:
                    selector_metadata_changed = clear_failed_selectors(
                        fresh.metadata,
                        target=diagnostic_target,
                    )
                    if selector_metadata_changed:
                        selector_record = None
                evidence_chain_id = (
                    rerun_record.chain_id
                    if rerun_record is not None
                    else selector_record.chain_id
                    if selector_record is not None
                    else None
                )
                audit_details.update(
                    {
                        "failed_selector_count": len(parsed.failed_selectors),
                        "failure_expectation": failure_expectation,
                        "failure_chain_id": evidence_chain_id,
                    }
                )
                diagnostic_result = WorkspaceRunDiagnosticResult(
                    workspace_id=command.workspace_id,
                    diagnostic_id=locked_profile.diagnostic_id,
                    summary=locked_profile.summary,
                    selector_kind=locked_profile.selector.kind.value,
                    resolved_selector=resolved.value,
                    resolved_selectors={
                        name: list(values) for name, values in resolved.values.items() if values
                    },
                    argv=list(result.argv),
                    working_directory=str(command_cwd.relative_to(locked_workspace) or "."),
                    network_policy=locked_profile.network_policy.value,
                    mutability=locked_profile.mutability.value,
                    parser=locked_profile.parser.value,
                    intent=intent.value,
                    expectation=expectation.value,
                    expected_failure_class=expected_failure_value,
                    returncode=result.returncode,
                    outcome=parsed.outcome,
                    failure_class=parsed.failure_class,
                    expectation_met=evaluation.expectation_met,
                    business_tests_ran=parsed.business_tests_ran,
                    valid_tdd_red_evidence=evaluation.valid_tdd_red_evidence,
                    parsed=parsed.fields,
                    excerpt=parsed.excerpt,
                    output_truncated=parsed.output_truncated,
                    fingerprint_before=before_fingerprint,
                    fingerprint_after=after_fingerprint,
                    fingerprint_changed=fingerprint_changed,
                    head_sha=self.ctx.git.head_sha(locked_workspace),
                    changed_paths=sorted(after_paths),
                    unexpected_paths=[],
                    change_metrics=metrics,
                    verification_invalidated=verification_invalidated,
                    satisfies_commit_gate=False,
                    next_safe_actions=next_actions,
                    execution_evidence=execution_evidence_data,
                    failed_selectors=list(parsed.failed_selectors),
                    output_artifact_reference=parsed.output_artifact_reference,
                    failure_provider=parsed.failure_provider,
                    selector_coverage=parsed.selector_coverage,
                    selectors_unavailable_reason=parsed.selectors_unavailable_reason,
                    failure_locations=[
                        {
                            "path": item.path,
                            "line": item.line,
                            "column": item.column,
                            "code": item.code,
                        }
                        for item in parsed.failure_locations
                    ],
                    output_artifact_status=parsed.output_artifact_status,
                    failure_expectation=failure_expectation,
                    failure_chain_id=evidence_chain_id,
                    rerun_of_selectors=(
                        list(rerun_record.selectors) if rerun_record is not None else []
                    ),
                )
                metadata_changed = selector_metadata_changed
                if (
                    reuse_binding is not None
                    and parsed.outcome == "failed"
                    and parsed.failure_class in _REUSABLE_DIAGNOSTIC_FAILURES
                    and not parsed.output_truncated
                    and not fingerprint_changed
                ):
                    reusable_recorded = record_reusable_failure(
                        fresh.metadata,
                        target=diagnostic_target,
                        binding=reuse_binding,
                        evidence={
                            "complete": True,
                            "result": to_data(diagnostic_result),
                        },
                    )
                    metadata_changed = reusable_recorded or metadata_changed
                    if reusable_recorded:
                        audit_details["failure_reuse_recorded"] = True
                        audit_details["reuse_binding"] = reuse_binding.digest
                elif parsed.outcome == "passed" or fingerprint_changed:
                    metadata_changed = (
                        clear_reusable_failure(
                            fresh.metadata,
                            target=diagnostic_target,
                        )
                        or metadata_changed
                    )
                if metadata_changed:
                    self.ctx.store.save(fresh)
                return diagnostic_result

        return self.ctx.audited(
            "workspace_run_diagnostic",
            audit_details,
            operation,
            mutating=True,
        )
