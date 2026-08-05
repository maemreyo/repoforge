"""Isolated, operation-scoped GitHub capability preflight evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ...config import ServerConfig
from ...domain.errors import ErrorCode
from ...domain.github_capability_preflight import (
    GitHubCapabilityEvidenceState,
    GitHubCapabilityPreflightReport,
    GitHubCapabilityPreflightRequest,
    GitHubCapabilityResult,
    GitHubOperationCapability,
    github_capability_requirements,
)
from ...domain.repository_auth_broker import ProcessAuthContext
from ...ports.command import CommandExecutor, CommandResult

_API_VERSION = "2022-11-28"
_HTTP_STATUS = re.compile(r"HTTP\s+(\d{3})", re.IGNORECASE)
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_ENVIRONMENT_KEYS = ("PATH", "LANG", "LC_ALL")


@dataclass(frozen=True, slots=True)
class _FailureClassification:
    state: GitHubCapabilityEvidenceState
    error_code: ErrorCode
    reason_code: str
    policy_category: str | None = None


_CAPABILITY_ENDPOINTS: dict[GitHubOperationCapability, str] = {
    GitHubOperationCapability.CONTENTS_READ: "contents/",
    GitHubOperationCapability.CONTENTS_WRITE: "contents/",
    GitHubOperationCapability.ISSUES_READ: "issues?per_page=1",
    GitHubOperationCapability.ISSUES_WRITE: "issues?per_page=1",
    GitHubOperationCapability.PULL_REQUESTS_READ: "pulls?per_page=1",
    GitHubOperationCapability.PULL_REQUESTS_WRITE: "pulls?per_page=1",
    GitHubOperationCapability.WORKFLOWS_READ: "actions/workflows?per_page=1",
    GitHubOperationCapability.WORKFLOWS_WRITE: "actions/workflows?per_page=1",
    GitHubOperationCapability.RELEASES_READ: "releases?per_page=1",
    GitHubOperationCapability.RELEASES_WRITE: "releases?per_page=1",
}

_RULESET_CAPABILITIES = frozenset(
    {
        GitHubOperationCapability.CONTENTS_WRITE,
        GitHubOperationCapability.PULL_REQUESTS_WRITE,
        GitHubOperationCapability.WORKFLOWS_WRITE,
        GitHubOperationCapability.RELEASES_WRITE,
    }
)

_UNOBSERVABLE_REASONS: dict[GitHubOperationCapability, str] = {
    GitHubOperationCapability.PROJECTS_READ: "project_target_evidence_absent",
    GitHubOperationCapability.PROJECTS_WRITE: "project_target_evidence_absent",
    GitHubOperationCapability.PACKAGES_READ: "package_target_evidence_absent",
    GitHubOperationCapability.PACKAGES_WRITE: "package_target_evidence_absent",
}


def _detail_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _response_body(stdout: str) -> str:
    """Strip the leading HTTP status/header block `_api`'s `--include` prints.

    `--include` is what makes `_http_status` able to read the status line; the
    cost is that `stdout` is the raw response with headers prepended, not bare
    JSON. Headers end at the first blank line (CRLF- or LF-terminated) per HTTP
    framing, so splitting there and keeping everything after is exact, not a
    heuristic -- a JSON body never contains a literal blank line itself.
    """
    for separator in ("\r\n\r\n", "\n\n"):
        if separator in stdout:
            return stdout.split(separator, 1)[1]
    return stdout


def _json_value(result: CommandResult) -> object | None:
    if result.returncode != 0 or result.stdout_truncated:
        return None
    try:
        return cast(object, json.loads(_response_body(result.stdout) or "null"))
    except json.JSONDecodeError:
        return None


def _http_status(result: CommandResult) -> int | None:
    match = _HTTP_STATUS.search(result.combined)
    return int(match.group(1)) if match else None


def _permission_allows(observed: tuple[str, ...], required: str) -> bool:
    required_name, required_level = required.split(":", 1)
    levels = {"read": 1, "write": 2, "admin": 3}
    for permission in observed:
        name, level = permission.split(":", 1)
        if name == required_name and levels[level] >= levels[required_level]:
            return True
    return False


def _classify_failure(
    result: CommandResult,
    *,
    ruleset_probe: bool = False,
) -> _FailureClassification:
    combined = result.combined.casefold()
    status = _http_status(result)
    if "x-github-sso" in combined or "saml" in combined:
        return _FailureClassification(
            GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
            ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
            "sso_authorization_required",
            "sso",
        )
    if "fine-grained" in combined and ("approv" in combined or "pending" in combined):
        return _FailureClassification(
            GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
            ErrorCode.GITHUB_TOKEN_APPROVAL_REQUIRED,
            "token_approval_required",
            "token_approval",
        )
    if (
        "actions is disabled" in combined
        or ("actions" in combined and "organization policy" in combined)
        or "selected actions" in combined
    ):
        return _FailureClassification(
            GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
            ErrorCode.GITHUB_WORKFLOW_POLICY_DENIED,
            "workflow_policy_denied",
            "workflow_policy",
        )
    if (
        "ip allow list" in combined
        or "ip is not permitted" in combined
        or "vpn" in combined
        or "network policy" in combined
    ):
        return _FailureClassification(
            GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
            ErrorCode.GITHUB_NETWORK_POLICY_DENIED,
            "network_policy_denied",
            "network_policy",
        )
    if (
        ruleset_probe
        or "rule violations" in combined
        or "gh013" in combined
        or "ruleset" in combined
    ):
        return _FailureClassification(
            GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
            ErrorCode.GITHUB_RULESET_POLICY_DENIED,
            "ruleset_policy_denied",
            "ruleset",
        )
    if status is None or status >= 500:
        return _FailureClassification(
            GitHubCapabilityEvidenceState.PROVIDER_UNAVAILABLE,
            ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
            "provider_unavailable",
            "provider",
        )
    return _FailureClassification(
        GitHubCapabilityEvidenceState.PROVEN_DENIED,
        ErrorCode.GITHUB_API_PERMISSION_DENIED,
        "provider_permission_denied",
    )


def _failure_result(
    capability: GitHubOperationCapability,
    command: CommandResult,
    *,
    ruleset_probe: bool = False,
) -> GitHubCapabilityResult:
    classified = _classify_failure(command, ruleset_probe=ruleset_probe)
    return GitHubCapabilityResult(
        capability=capability,
        state=classified.state,
        reason_code=classified.reason_code,
        detail_digest=_detail_digest(command.combined),
        error_code=classified.error_code,
        policy_category=classified.policy_category,
    )


def _unobservable_result(
    capability: GitHubOperationCapability,
    reason_code: str,
) -> GitHubCapabilityResult:
    return GitHubCapabilityResult(
        capability=capability,
        state=GitHubCapabilityEvidenceState.UNOBSERVABLE,
        reason_code=reason_code,
        detail_digest=_detail_digest(reason_code),
        error_code=ErrorCode.GITHUB_ENTERPRISE_EVIDENCE_UNOBSERVABLE,
        policy_category="enterprise_evidence",
    )


class CommandGitHubCapabilityPreflight:
    """Collect bounded GitHub evidence using only the exact operation auth context."""

    def __init__(self, executor: CommandExecutor, server: ServerConfig) -> None:
        self._executor = executor
        self._server = server
        self._output_limit = min(max(server.max_tool_output_chars, 500_000), 5_000_000)

    def _isolated_environment(self, auth_context: ProcessAuthContext) -> dict[str, str]:
        # auth_context.environment_dict() carries only the broker-issued identity
        # material (e.g. GH_TOKEN) -- never PATH -- so calling `gh` with it verbatim
        # fails closed with "Executable not found: gh" the moment PATH isn't already
        # ambient in the parent process. Seed from the executor's own resolved PATH
        # (same allowlist repository_observation.py uses) before layering the
        # reviewed identity on top.
        environment = {
            key: value
            for key, value in self._executor.environment().items()
            if key in _SAFE_ENVIRONMENT_KEYS
        }
        environment.update(auth_context.environment_dict())
        return environment

    def _api(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
        endpoint: str,
    ) -> CommandResult:
        return self._executor.run_isolated(
            [
                "gh",
                "api",
                "--hostname",
                request.host,
                "--method",
                "GET",
                endpoint,
                "--include",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                f"X-GitHub-Api-Version: {_API_VERSION}",
            ],
            cwd=cwd,
            environment=self._isolated_environment(auth_context),
            secrets=auth_context.secret_values,
            check=False,
            timeout=self._server.default_command_timeout_seconds,
            output_limit=self._output_limit,
        )

    @staticmethod
    def _all_failed(
        request: GitHubCapabilityPreflightRequest,
        result: CommandResult,
        *,
        repository_mismatch: bool = False,
    ) -> tuple[GitHubCapabilityResult, ...]:
        if repository_mismatch:
            return tuple(
                GitHubCapabilityResult(
                    capability=capability,
                    state=GitHubCapabilityEvidenceState.PROVEN_DENIED,
                    reason_code="repository_identity_mismatch",
                    detail_digest=_detail_digest(result.combined),
                    error_code=ErrorCode.GITHUB_API_REPOSITORY_MISMATCH,
                )
                for capability in request.capability_ids
            )
        return tuple(_failure_result(capability, result) for capability in request.capability_ids)

    def _repository(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
    ) -> tuple[str | None, bool, tuple[GitHubCapabilityResult, ...] | None]:
        result = self._api(
            cwd,
            request,
            auth_context,
            f"repositories/{request.repository_id}",
        )
        if result.returncode != 0:
            return None, False, self._all_failed(request, result)
        payload = _json_value(result)
        if not isinstance(payload, dict):
            return (
                None,
                False,
                tuple(
                    GitHubCapabilityResult(
                        capability=capability,
                        state=GitHubCapabilityEvidenceState.PROVIDER_UNAVAILABLE,
                        reason_code="repository_payload_invalid",
                        detail_digest=_detail_digest(result.stdout),
                        error_code=ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                        policy_category="provider",
                    )
                    for capability in request.capability_ids
                ),
            )
        repository_id = str(payload.get("id", ""))
        slug = payload.get("full_name")
        if (
            repository_id != request.repository_id
            or not isinstance(slug, str)
            or _REPOSITORY.fullmatch(slug) is None
        ):
            return None, False, self._all_failed(request, result, repository_mismatch=True)
        if payload.get("archived") is True:
            return (
                None,
                False,
                tuple(
                    GitHubCapabilityResult(
                        capability=capability,
                        state=GitHubCapabilityEvidenceState.PROVEN_DENIED,
                        reason_code="repository_archived",
                        detail_digest=_detail_digest(result.stdout),
                        error_code=ErrorCode.GITHUB_API_PERMISSION_DENIED,
                    )
                    for capability in request.capability_ids
                ),
            )
        # `permissions.push` is GitHub's own verdict on whether *this exact*
        # authenticated caller can write to *this exact* repository -- scoped
        # evidence a bare OAuth/PAT scope list can't give (a "repo"-scoped token
        # says nothing about collaborator-level access to one specific repo).
        # Stored-account grants never populate permission_ids (see
        # GhCliStoredAccountTokenSource.issue), so this is the only real write
        # evidence available for that credential kind; treat it as authoritative.
        permissions = payload.get("permissions")
        push_permission = isinstance(permissions, dict) and permissions.get("push") is True
        return slug, push_permission, None

    def _probe_capability(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
        slug: str,
        repository_push_permission: bool,
        capability: GitHubOperationCapability,
    ) -> GitHubCapabilityResult:
        unobservable = _UNOBSERVABLE_REASONS.get(capability)
        if unobservable is not None:
            return _unobservable_result(capability, unobservable)

        requirement = github_capability_requirements()[capability]
        has_explicit_permissions = bool(request.permission_ids)
        permission_allowed = _permission_allows(request.permission_ids, requirement.permission_id)
        if has_explicit_permissions and not permission_allowed:
            return GitHubCapabilityResult(
                capability=capability,
                state=GitHubCapabilityEvidenceState.PROVEN_DENIED,
                reason_code="required_permission_missing",
                detail_digest=_detail_digest(requirement.permission_id),
                error_code=ErrorCode.GITHUB_API_PERMISSION_DENIED,
            )

        endpoint = _CAPABILITY_ENDPOINTS[capability]
        result = self._api(cwd, request, auth_context, f"repos/{slug}/{endpoint}")
        if result.returncode != 0:
            return _failure_result(capability, result)
        if _json_value(result) is None:
            return GitHubCapabilityResult(
                capability=capability,
                state=GitHubCapabilityEvidenceState.PROVIDER_UNAVAILABLE,
                reason_code="capability_payload_invalid",
                detail_digest=_detail_digest(result.stdout),
                error_code=ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                policy_category="provider",
            )
        if requirement.write and not has_explicit_permissions and not repository_push_permission:
            return _unobservable_result(capability, "write_permission_evidence_absent")

        if capability in _RULESET_CAPABILITIES:
            ruleset = self._api(
                cwd,
                request,
                auth_context,
                f"repos/{slug}/rulesets?includes_parents=true",
            )
            if ruleset.returncode != 0:
                return _failure_result(capability, ruleset, ruleset_probe=True)
            if _json_value(ruleset) is None:
                return GitHubCapabilityResult(
                    capability=capability,
                    state=GitHubCapabilityEvidenceState.PROVIDER_UNAVAILABLE,
                    reason_code="ruleset_payload_invalid",
                    detail_digest=_detail_digest(ruleset.stdout),
                    error_code=ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
                    policy_category="provider",
                )

        return GitHubCapabilityResult(
            capability=capability,
            state=GitHubCapabilityEvidenceState.PROVEN_AVAILABLE,
            reason_code="bounded_probe_succeeded",
            detail_digest=_detail_digest(result.stdout),
        )

    def preflight(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
    ) -> GitHubCapabilityPreflightReport:
        if not isinstance(cwd, Path) or not cwd.is_absolute():
            raise ValueError("cwd must be an absolute Path")
        if not isinstance(request, GitHubCapabilityPreflightRequest):
            raise ValueError("request must be a GitHubCapabilityPreflightRequest")
        if not isinstance(auth_context, ProcessAuthContext):
            raise ValueError("auth_context must be a ProcessAuthContext")
        if auth_context.target_id != request.repository_id:
            results = tuple(
                GitHubCapabilityResult(
                    capability=capability,
                    state=GitHubCapabilityEvidenceState.PROVEN_DENIED,
                    reason_code="auth_target_mismatch",
                    detail_digest=_detail_digest(auth_context.target_id),
                    error_code=ErrorCode.GITHUB_API_REPOSITORY_MISMATCH,
                )
                for capability in request.capability_ids
            )
            return GitHubCapabilityPreflightReport.build(request, results)

        slug, repository_push_permission, repository_failure = self._repository(
            cwd, request, auth_context
        )
        if repository_failure is not None:
            return GitHubCapabilityPreflightReport.build(request, repository_failure)
        assert slug is not None
        results = tuple(
            self._probe_capability(
                cwd, request, auth_context, slug, repository_push_permission, capability
            )
            for capability in request.capability_ids
        )
        return GitHubCapabilityPreflightReport.build(request, results)


__all__ = ["CommandGitHubCapabilityPreflight"]
