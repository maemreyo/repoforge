from __future__ import annotations

from dataclasses import replace

import pytest

from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.github_capability_preflight import (
    GitHubCapabilityEvidenceState,
    GitHubCapabilityPreflightReport,
    GitHubCapabilityPreflightRequest,
    GitHubCapabilityResult,
    GitHubOperationCapability,
    authorize_github_capabilities,
    github_capability_requirements,
)

_CONFIG = "a" * 64
_POLICY = "b" * 64
_OBSERVED_AT = "2026-07-29T13:30:00+00:00"


@pytest.mark.parametrize(
    ("capability", "permission_id"),
    [
        (GitHubOperationCapability.CONTENTS_READ, "contents:read"),
        (GitHubOperationCapability.CONTENTS_WRITE, "contents:write"),
        (GitHubOperationCapability.ISSUES_READ, "issues:read"),
        (GitHubOperationCapability.ISSUES_WRITE, "issues:write"),
        (GitHubOperationCapability.PULL_REQUESTS_READ, "pull_requests:read"),
        (GitHubOperationCapability.PULL_REQUESTS_WRITE, "pull_requests:write"),
        (GitHubOperationCapability.WORKFLOWS_READ, "actions:read"),
        (GitHubOperationCapability.WORKFLOWS_WRITE, "workflows:write"),
        (GitHubOperationCapability.RELEASES_READ, "contents:read"),
        (GitHubOperationCapability.RELEASES_WRITE, "contents:write"),
        (GitHubOperationCapability.PROJECTS_READ, "organization_projects:read"),
        (GitHubOperationCapability.PROJECTS_WRITE, "organization_projects:write"),
        (GitHubOperationCapability.PACKAGES_READ, "organization_packages:read"),
        (GitHubOperationCapability.PACKAGES_WRITE, "organization_packages:write"),
    ],
)
def test_capability_matrix_is_exact(
    capability: GitHubOperationCapability,
    permission_id: str,
) -> None:
    requirements = github_capability_requirements()

    assert set(requirements) == set(GitHubOperationCapability)
    assert requirements[capability].permission_id == permission_id
    assert requirements[capability].write is capability.value.endswith(".write")


def _request(
    capabilities: tuple[GitHubOperationCapability, ...] = (
        GitHubOperationCapability.PULL_REQUESTS_WRITE,
        GitHubOperationCapability.CONTENTS_READ,
    ),
    permissions: tuple[str, ...] = ("pull_requests:write", "contents:read"),
) -> GitHubCapabilityPreflightRequest:
    return GitHubCapabilityPreflightRequest(
        host="github.com",
        actor_id="installation:84",
        repository_id="123456",
        installation_id="installation-84",
        capability_ids=capabilities,
        permission_ids=permissions,
        config_revision=_CONFIG,
        policy_revision=_POLICY,
        observed_at=_OBSERVED_AT,
    )


def _available(
    capability: GitHubOperationCapability,
    *,
    detail_digest: str | None = None,
) -> GitHubCapabilityResult:
    return GitHubCapabilityResult(
        capability=capability,
        state=GitHubCapabilityEvidenceState.PROVEN_AVAILABLE,
        reason_code="bounded_probe_succeeded",
        detail_digest=detail_digest or "c" * 64,
    )


def _report(
    request: GitHubCapabilityPreflightRequest | None = None,
    results: tuple[GitHubCapabilityResult, ...] | None = None,
) -> GitHubCapabilityPreflightReport:
    selected = request or _request()
    return GitHubCapabilityPreflightReport.build(
        selected,
        results or tuple(_available(capability) for capability in selected.capability_ids),
    )


def test_report_digests_are_deterministic_order_independent_and_secret_free() -> None:
    request = _request()
    forward = _report(request)
    reverse = GitHubCapabilityPreflightReport.build(
        replace(
            request,
            capability_ids=tuple(reversed(request.capability_ids)),
            permission_ids=tuple(reversed(request.permission_ids)),
        ),
        tuple(reversed(tuple(_available(capability) for capability in request.capability_ids))),
    )

    assert forward.capability_digest == reverse.capability_digest
    assert forward.permission_digest == reverse.permission_digest
    assert forward.evidence_digest == reverse.evidence_digest
    assert len(forward.capability_digest) == 64
    assert len(forward.permission_digest) == 64
    assert len(forward.evidence_digest) == 64
    rendered = str(forward.safe_payload())
    assert "token" not in rendered.lower()
    assert "authorization" not in rendered.lower()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda request: replace(request, capability_ids=()),
        lambda request: replace(
            request,
            capability_ids=(
                GitHubOperationCapability.CONTENTS_READ,
                GitHubOperationCapability.CONTENTS_READ,
            ),
        ),
        lambda request: replace(request, permission_ids=("contents:read", "contents:read")),
        lambda request: replace(request, repository_id="repo with spaces"),
        lambda request: replace(request, config_revision="not-a-digest"),
        lambda request: replace(request, policy_revision="not-a-digest"),
        lambda request: replace(request, observed_at="not-a-timestamp"),
    ],
)
def test_request_rejects_empty_duplicate_or_unbounded_identity_inputs(mutation) -> None:
    with pytest.raises(ValueError):
        mutation(_request())


def test_report_requires_exactly_one_result_per_requested_capability() -> None:
    request = _request()
    first = _available(request.capability_ids[0])

    with pytest.raises(ValueError, match="exactly once"):
        GitHubCapabilityPreflightReport.build(request, (first,))

    with pytest.raises(ValueError, match="exactly once"):
        GitHubCapabilityPreflightReport.build(request, (first, first))

    with pytest.raises(ValueError, match="requested"):
        GitHubCapabilityPreflightReport.build(
            request,
            (
                first,
                _available(GitHubOperationCapability.ISSUES_READ),
            ),
        )


def test_authorization_accepts_only_reports_with_proven_available_results() -> None:
    report = _report()

    assert authorize_github_capabilities(report) is report

    denied = replace(
        report.results[0],
        state=GitHubCapabilityEvidenceState.PROVEN_DENIED,
        reason_code="permission_missing",
        error_code=ErrorCode.GITHUB_API_PERMISSION_DENIED,
    )
    denied_report = GitHubCapabilityPreflightReport.build(
        _request(),
        (denied, report.results[1]),
    )

    with pytest.raises(RepoForgeError) as failure:
        authorize_github_capabilities(denied_report)

    assert failure.value.code is ErrorCode.GITHUB_API_PERMISSION_DENIED
    assert failure.value.unchanged_state == ("No GitHub external write was admitted.",)
