"""Exact GitHub capability requirements and secret-free preflight evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .errors import ErrorCode, RepoForgeError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_PERMISSION = re.compile(r"^[a-z][a-z0-9_]{0,63}:(?:read|write|admin)$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class GitHubOperationCapability(str, Enum):
    CONTENTS_READ = "github.contents.read"
    CONTENTS_WRITE = "github.contents.write"
    ISSUES_READ = "github.issues.read"
    ISSUES_WRITE = "github.issues.write"
    PULL_REQUESTS_READ = "github.pull_requests.read"
    PULL_REQUESTS_WRITE = "github.pull_requests.write"
    WORKFLOWS_READ = "github.workflows.read"
    WORKFLOWS_WRITE = "github.workflows.write"
    RELEASES_READ = "github.releases.read"
    RELEASES_WRITE = "github.releases.write"
    PROJECTS_READ = "github.projects.read"
    PROJECTS_WRITE = "github.projects.write"
    PACKAGES_READ = "github.packages.read"
    PACKAGES_WRITE = "github.packages.write"


class GitHubCapabilityEvidenceState(str, Enum):
    PROVEN_AVAILABLE = "proven_available"
    PROVEN_DENIED = "proven_denied"
    LIKELY_POLICY_DENIED = "likely_policy_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNOBSERVABLE = "unobservable"


@dataclass(frozen=True, slots=True)
class GitHubPermissionRequirement:
    capability: GitHubOperationCapability
    permission_id: str
    write: bool

    def __post_init__(self) -> None:
        if not isinstance(self.capability, GitHubOperationCapability):
            raise ValueError("capability must be a GitHubOperationCapability")
        if (
            not isinstance(self.permission_id, str)
            or _PERMISSION.fullmatch(self.permission_id) is None
        ):
            raise ValueError("permission ID must use name:read|write|admin")
        if not isinstance(self.write, bool):
            raise ValueError("write must be boolean")


_REQUIREMENTS: dict[GitHubOperationCapability, GitHubPermissionRequirement] = {
    GitHubOperationCapability.CONTENTS_READ: GitHubPermissionRequirement(
        GitHubOperationCapability.CONTENTS_READ, "contents:read", False
    ),
    GitHubOperationCapability.CONTENTS_WRITE: GitHubPermissionRequirement(
        GitHubOperationCapability.CONTENTS_WRITE, "contents:write", True
    ),
    GitHubOperationCapability.ISSUES_READ: GitHubPermissionRequirement(
        GitHubOperationCapability.ISSUES_READ, "issues:read", False
    ),
    GitHubOperationCapability.ISSUES_WRITE: GitHubPermissionRequirement(
        GitHubOperationCapability.ISSUES_WRITE, "issues:write", True
    ),
    GitHubOperationCapability.PULL_REQUESTS_READ: GitHubPermissionRequirement(
        GitHubOperationCapability.PULL_REQUESTS_READ, "pull_requests:read", False
    ),
    GitHubOperationCapability.PULL_REQUESTS_WRITE: GitHubPermissionRequirement(
        GitHubOperationCapability.PULL_REQUESTS_WRITE, "pull_requests:write", True
    ),
    GitHubOperationCapability.WORKFLOWS_READ: GitHubPermissionRequirement(
        GitHubOperationCapability.WORKFLOWS_READ, "actions:read", False
    ),
    GitHubOperationCapability.WORKFLOWS_WRITE: GitHubPermissionRequirement(
        GitHubOperationCapability.WORKFLOWS_WRITE, "workflows:write", True
    ),
    GitHubOperationCapability.RELEASES_READ: GitHubPermissionRequirement(
        GitHubOperationCapability.RELEASES_READ, "contents:read", False
    ),
    GitHubOperationCapability.RELEASES_WRITE: GitHubPermissionRequirement(
        GitHubOperationCapability.RELEASES_WRITE, "contents:write", True
    ),
    GitHubOperationCapability.PROJECTS_READ: GitHubPermissionRequirement(
        GitHubOperationCapability.PROJECTS_READ, "organization_projects:read", False
    ),
    GitHubOperationCapability.PROJECTS_WRITE: GitHubPermissionRequirement(
        GitHubOperationCapability.PROJECTS_WRITE, "organization_projects:write", True
    ),
    GitHubOperationCapability.PACKAGES_READ: GitHubPermissionRequirement(
        GitHubOperationCapability.PACKAGES_READ, "organization_packages:read", False
    ),
    GitHubOperationCapability.PACKAGES_WRITE: GitHubPermissionRequirement(
        GitHubOperationCapability.PACKAGES_WRITE, "organization_packages:write", True
    ),
}


def github_capability_requirements() -> Mapping[
    GitHubOperationCapability, GitHubPermissionRequirement
]:
    return _REQUIREMENTS


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _permission(value: str) -> str:
    if not isinstance(value, str) or _PERMISSION.fullmatch(value) is None:
        raise ValueError("permission ID must use name:read|write|admin")
    return value


def _digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _timestamp(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError(f"{field_name} must be a bounded timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return value


def _sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class GitHubCapabilityPreflightRequest:
    host: str
    actor_id: str
    repository_id: str
    installation_id: str | None
    capability_ids: tuple[GitHubOperationCapability, ...]
    permission_ids: tuple[str, ...]
    config_revision: str
    policy_revision: str
    observed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or _HOST.fullmatch(self.host) is None:
            raise ValueError("host must be a bounded lowercase host")
        _safe_id(self.actor_id, "actor_id")
        _safe_id(self.repository_id, "repository_id")
        if self.installation_id is not None:
            _safe_id(self.installation_id, "installation_id")
        if not isinstance(self.capability_ids, tuple) or not self.capability_ids:
            raise ValueError("capability_ids must be a non-empty tuple")
        if any(not isinstance(item, GitHubOperationCapability) for item in self.capability_ids):
            raise ValueError("capability_ids must contain exact GitHub operation capabilities")
        if len(set(self.capability_ids)) != len(self.capability_ids):
            raise ValueError("capability_ids must be unique")
        if not isinstance(self.permission_ids, tuple):
            raise ValueError("permission_ids must be a tuple")
        normalized_permissions = tuple(_permission(item) for item in self.permission_ids)
        if len(set(normalized_permissions)) != len(normalized_permissions):
            raise ValueError("permission_ids must be unique")
        _digest(self.config_revision, "config_revision")
        _digest(self.policy_revision, "policy_revision")
        _timestamp(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class GitHubCapabilityResult:
    capability: GitHubOperationCapability
    state: GitHubCapabilityEvidenceState
    reason_code: str
    detail_digest: str
    error_code: ErrorCode | None = None
    policy_category: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, GitHubOperationCapability):
            raise ValueError("capability must be a GitHubOperationCapability")
        if not isinstance(self.state, GitHubCapabilityEvidenceState):
            raise ValueError("state must be a GitHubCapabilityEvidenceState")
        _safe_id(self.reason_code, "reason_code")
        _digest(self.detail_digest, "detail_digest")
        if self.state is GitHubCapabilityEvidenceState.PROVEN_AVAILABLE:
            if self.error_code is not None or self.policy_category is not None:
                raise ValueError("available capability evidence cannot carry an error")
        elif not isinstance(self.error_code, ErrorCode):
            raise ValueError("non-available capability evidence requires an error code")
        if self.policy_category is not None:
            _safe_id(self.policy_category, "policy_category")

    def safe_payload(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "detail_digest": self.detail_digest,
            "error_code": self.error_code.value if self.error_code is not None else None,
            "policy_category": self.policy_category,
        }


@dataclass(frozen=True, slots=True)
class GitHubCapabilityPreflightReport:
    host: str
    actor_id: str
    repository_id: str
    installation_id: str | None
    capability_ids: tuple[GitHubOperationCapability, ...]
    permission_ids: tuple[str, ...]
    results: tuple[GitHubCapabilityResult, ...]
    config_revision: str
    policy_revision: str
    observed_at: str
    capability_digest: str
    permission_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        request = GitHubCapabilityPreflightRequest(
            host=self.host,
            actor_id=self.actor_id,
            repository_id=self.repository_id,
            installation_id=self.installation_id,
            capability_ids=self.capability_ids,
            permission_ids=self.permission_ids,
            config_revision=self.config_revision,
            policy_revision=self.policy_revision,
            observed_at=self.observed_at,
        )
        del request
        _validate_results(self.capability_ids, self.results)
        _digest(self.capability_digest, "capability_digest")
        _digest(self.permission_digest, "permission_digest")
        _digest(self.evidence_digest, "evidence_digest")

    @classmethod
    def build(
        cls,
        request: GitHubCapabilityPreflightRequest,
        results: tuple[GitHubCapabilityResult, ...],
    ) -> GitHubCapabilityPreflightReport:
        if not isinstance(request, GitHubCapabilityPreflightRequest):
            raise ValueError("request must be a GitHubCapabilityPreflightRequest")
        capabilities = tuple(sorted(request.capability_ids, key=lambda item: item.value))
        permissions = tuple(sorted(request.permission_ids))
        normalized_results = tuple(sorted(results, key=lambda item: item.capability.value))
        _validate_results(capabilities, normalized_results)
        capability_digest = _sha256([item.value for item in capabilities])
        permission_digest = _sha256(list(permissions))
        evidence_projection = {
            "host": request.host,
            "actor_id": request.actor_id,
            "repository_id": request.repository_id,
            "installation_id": request.installation_id,
            "capability_ids": [item.value for item in capabilities],
            "permission_ids": list(permissions),
            "results": [item.safe_payload() for item in normalized_results],
            "config_revision": request.config_revision,
            "policy_revision": request.policy_revision,
        }
        return cls(
            host=request.host,
            actor_id=request.actor_id,
            repository_id=request.repository_id,
            installation_id=request.installation_id,
            capability_ids=capabilities,
            permission_ids=permissions,
            results=normalized_results,
            config_revision=request.config_revision,
            policy_revision=request.policy_revision,
            observed_at=request.observed_at,
            capability_digest=capability_digest,
            permission_digest=permission_digest,
            evidence_digest=_sha256(evidence_projection),
        )

    def safe_payload(self) -> dict[str, object]:
        return {
            "host": self.host,
            "actor_id": self.actor_id,
            "repository_id": self.repository_id,
            "installation_id": self.installation_id,
            "capability_ids": [item.value for item in self.capability_ids],
            "permission_ids": list(self.permission_ids),
            "results": [item.safe_payload() for item in self.results],
            "config_revision": self.config_revision,
            "policy_revision": self.policy_revision,
            "observed_at": self.observed_at,
            "capability_digest": self.capability_digest,
            "permission_digest": self.permission_digest,
            "evidence_digest": self.evidence_digest,
        }


def _validate_results(
    capability_ids: tuple[GitHubOperationCapability, ...],
    results: tuple[GitHubCapabilityResult, ...],
) -> None:
    if not isinstance(results, tuple) or any(
        not isinstance(item, GitHubCapabilityResult) for item in results
    ):
        raise ValueError("results must be a tuple of GitHubCapabilityResult values")
    requested = tuple(item.value for item in capability_ids)
    observed = tuple(item.capability.value for item in results)
    if len(set(observed)) != len(observed):
        raise ValueError("each requested capability must appear exactly once")
    if set(observed) != set(requested):
        if set(observed).difference(requested):
            raise ValueError("results contain a capability that was not requested")
        raise ValueError("each requested capability must appear exactly once")


def authorize_github_capabilities(
    report: GitHubCapabilityPreflightReport,
) -> GitHubCapabilityPreflightReport:
    if not isinstance(report, GitHubCapabilityPreflightReport):
        raise ValueError("report must be a GitHubCapabilityPreflightReport")
    denied = next(
        (
            item
            for item in report.results
            if item.state is not GitHubCapabilityEvidenceState.PROVEN_AVAILABLE
        ),
        None,
    )
    if denied is None:
        return report
    code = denied.error_code or ErrorCode.GITHUB_API_PERMISSION_DENIED
    raise RepoForgeError(
        "GitHub capability preflight did not prove the exact requested capability.",
        code=code,
        retryable=code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
        safe_next_action=(
            "Correct the reported condition for this exact GitHub capability and rerun "
            "preflight with the same reviewed profile."
        ),
        unchanged_state=("No GitHub external write was admitted.",),
        details={
            "capability_id": denied.capability.value,
            "repository_id": report.repository_id,
            "installation_id": report.installation_id,
            "evidence_state": denied.state.value,
            "policy_category": denied.policy_category,
        },
    )


__all__ = [
    "GitHubCapabilityEvidenceState",
    "GitHubCapabilityPreflightReport",
    "GitHubCapabilityPreflightRequest",
    "GitHubCapabilityResult",
    "GitHubOperationCapability",
    "GitHubPermissionRequirement",
    "authorize_github_capabilities",
    "github_capability_requirements",
]
