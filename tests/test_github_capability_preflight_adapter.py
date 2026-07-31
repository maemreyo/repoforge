from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.github.capability_preflight import (
    CommandGitHubCapabilityPreflight,
)
from repoforge.config import ServerConfig
from repoforge.domain.errors import ErrorCode
from repoforge.domain.github_capability_preflight import (
    GitHubCapabilityEvidenceState,
    GitHubCapabilityPreflightRequest,
    GitHubOperationCapability,
)
from repoforge.domain.repository_auth_broker import ProcessAuthContext
from repoforge.domain.repository_identity import AuthTargetKind
from repoforge.ports.cancellation import CancellationToken
from repoforge.ports.command import CommandResult

_ROOT = Path("/repo")
_HOST = "github.com"
_API_VERSION = "2022-11-28"
_TOKEN = "github-preflight-token-canary-293"
_CONFIG = "a" * 64
_POLICY = "b" * 64
_NOW = "2026-07-29T14:00:00+00:00"


def _api_argv(endpoint: str) -> tuple[str, ...]:
    return (
        "gh",
        "api",
        "--hostname",
        _HOST,
        "--method",
        "GET",
        endpoint,
        "--include",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {_API_VERSION}",
    )


class IsolatedProbeExecutor:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], CommandResult] = {}
        self.calls: list[dict[str, Any]] = []
        self.ambient_calls: list[tuple[str, ...]] = []

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"PATH": "/safe/bin", **dict(extra or {})}

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del cwd, input_text, timeout, check, extra_env, output_limit, cancel_token
        command = tuple(argv)
        self.ambient_calls.append(command)
        raise AssertionError(f"ambient command execution is forbidden: {command}")

    def run_isolated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text, cancel_token
        command = tuple(argv)
        self.calls.append(
            {
                "argv": command,
                "cwd": cwd,
                "environment": dict(environment),
                "secrets": tuple(secrets),
                "timeout": timeout,
                "check": check,
                "output_limit": output_limit,
            }
        )
        if command not in self.responses:
            raise AssertionError(f"unhandled isolated command: {command}")
        return self.responses[command]

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        del argv, cwd, timeout, max_bytes
        raise AssertionError("run_bytes is not used by capability preflight")

    def ok(self, endpoint: str, payload: object) -> None:
        argv = _api_argv(endpoint)
        self.responses[argv] = CommandResult(
            argv,
            str(_ROOT),
            0,
            json.dumps(payload),
            "",
        )

    def failure(
        self,
        endpoint: str,
        *,
        status: int,
        message: str,
        header: str | None = None,
    ) -> None:
        argv = _api_argv(endpoint)
        stderr = f"HTTP {status}\n"
        if header:
            stderr += header + "\n"
        stderr += json.dumps({"message": message})
        self.responses[argv] = CommandResult(argv, str(_ROOT), 1, "", stderr)


def _server() -> ServerConfig:
    return ServerConfig(workspace_root=Path("/workspaces"), state_root=Path("/state"))


def _auth() -> ProcessAuthContext:
    return ProcessAuthContext(
        profile_id="company-app",
        material_id="grant-company-app",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="123456",
        environment=(("GH_TOKEN", _TOKEN), ("GH_PROMPT_DISABLED", "1")),
        _secret_values=(_TOKEN,),
    )


def _request(
    capabilities: tuple[GitHubOperationCapability, ...],
    permissions: tuple[str, ...],
) -> GitHubCapabilityPreflightRequest:
    return GitHubCapabilityPreflightRequest(
        host=_HOST,
        actor_id="installation:84",
        repository_id="123456",
        installation_id="installation-84",
        capability_ids=capabilities,
        permission_ids=permissions,
        config_revision=_CONFIG,
        policy_revision=_POLICY,
        observed_at=_NOW,
    )


def _seed_repository(executor: IsolatedProbeExecutor) -> None:
    executor.ok(
        "repositories/123456",
        {"id": 123456, "full_name": "acme/widgets", "archived": False},
    )


def _result(report, capability: GitHubOperationCapability):
    return next(item for item in report.results if item.capability is capability)


def test_pull_request_write_uses_only_exact_isolated_capability_probes() -> None:
    executor = IsolatedProbeExecutor()
    _seed_repository(executor)
    executor.ok("repos/acme/widgets/pulls?per_page=1", [])
    executor.ok("repos/acme/widgets/rulesets?includes_parents=true", [])
    adapter = CommandGitHubCapabilityPreflight(executor, _server())

    report = adapter.preflight(
        _ROOT,
        _request(
            (GitHubOperationCapability.PULL_REQUESTS_WRITE,),
            ("pull_requests:write",),
        ),
        _auth(),
    )

    assert (
        _result(report, GitHubOperationCapability.PULL_REQUESTS_WRITE).state
        is GitHubCapabilityEvidenceState.PROVEN_AVAILABLE
    )
    assert [call["argv"] for call in executor.calls] == [
        _api_argv("repositories/123456"),
        _api_argv("repos/acme/widgets/pulls?per_page=1"),
        _api_argv("repos/acme/widgets/rulesets?includes_parents=true"),
    ]
    assert executor.ambient_calls == []
    for call in executor.calls:
        assert call["cwd"] == _ROOT
        assert call["environment"] == _auth().environment_dict()
        assert call["secrets"] == (_TOKEN,)
        assert call["check"] is False


def test_issue_preflight_does_not_probe_unrequested_families_or_rulesets() -> None:
    executor = IsolatedProbeExecutor()
    _seed_repository(executor)
    executor.ok("repos/acme/widgets/issues?per_page=1", [])
    adapter = CommandGitHubCapabilityPreflight(executor, _server())

    report = adapter.preflight(
        _ROOT,
        _request(
            (GitHubOperationCapability.ISSUES_READ,),
            ("issues:read",),
        ),
        _auth(),
    )

    assert (
        _result(report, GitHubOperationCapability.ISSUES_READ).state
        is GitHubCapabilityEvidenceState.PROVEN_AVAILABLE
    )
    invoked = [" ".join(call["argv"]) for call in executor.calls]
    assert all("pulls" not in call for call in invoked)
    assert all("actions" not in call for call in invoked)
    assert all("releases" not in call for call in invoked)
    assert all("rulesets" not in call for call in invoked)


@pytest.mark.parametrize(
    ("message", "header", "code", "category"),
    [
        (
            "Resource protected by organization SAML enforcement",
            "X-GitHub-SSO: required; url=https://github.com/orgs/acme/sso",
            ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
            "sso",
        ),
        (
            "Fine-grained personal access token must be approved by this organization",
            None,
            ErrorCode.GITHUB_TOKEN_APPROVAL_REQUIRED,
            "token_approval",
        ),
        (
            "GitHub Actions is disabled for this repository by organization policy",
            None,
            ErrorCode.GITHUB_WORKFLOW_POLICY_DENIED,
            "workflow_policy",
        ),
        (
            "Your IP is not permitted by the organization IP allow list",
            None,
            ErrorCode.GITHUB_NETWORK_POLICY_DENIED,
            "network_policy",
        ),
    ],
)
def test_enterprise_policy_failures_are_classified_without_leaking_response_text(
    message: str,
    header: str | None,
    code: ErrorCode,
    category: str,
) -> None:
    executor = IsolatedProbeExecutor()
    _seed_repository(executor)
    executor.failure(
        "repos/acme/widgets/issues?per_page=1",
        status=403,
        message=message + " " + _TOKEN,
        header=header,
    )
    adapter = CommandGitHubCapabilityPreflight(executor, _server())

    report = adapter.preflight(
        _ROOT,
        _request(
            (GitHubOperationCapability.ISSUES_WRITE,),
            ("issues:write",),
        ),
        _auth(),
    )

    result = _result(report, GitHubOperationCapability.ISSUES_WRITE)
    assert result.state is GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED
    assert result.error_code is code
    assert result.policy_category == category
    assert _TOKEN not in json.dumps(report.safe_payload(), sort_keys=True)


def test_ruleset_denial_is_typed_after_capability_probe_succeeds() -> None:
    executor = IsolatedProbeExecutor()
    _seed_repository(executor)
    executor.ok("repos/acme/widgets/pulls?per_page=1", [])
    executor.failure(
        "repos/acme/widgets/rulesets?includes_parents=true",
        status=403,
        message="Repository rule violations found (GH013)",
    )
    adapter = CommandGitHubCapabilityPreflight(executor, _server())

    report = adapter.preflight(
        _ROOT,
        _request(
            (GitHubOperationCapability.PULL_REQUESTS_WRITE,),
            ("pull_requests:write",),
        ),
        _auth(),
    )

    result = _result(report, GitHubOperationCapability.PULL_REQUESTS_WRITE)
    assert result.state is GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED
    assert result.error_code is ErrorCode.GITHUB_RULESET_POLICY_DENIED
    assert result.policy_category == "ruleset"


def test_provider_outage_and_unobservable_write_fail_closed() -> None:
    outage = IsolatedProbeExecutor()
    outage.failure(
        "repositories/123456",
        status=500,
        message="temporary provider failure " + _TOKEN,
    )
    outage_report = CommandGitHubCapabilityPreflight(outage, _server()).preflight(
        _ROOT,
        _request(
            (GitHubOperationCapability.CONTENTS_READ,),
            ("contents:read",),
        ),
        _auth(),
    )
    outage_result = _result(outage_report, GitHubOperationCapability.CONTENTS_READ)
    assert outage_result.state is GitHubCapabilityEvidenceState.PROVIDER_UNAVAILABLE
    assert outage_result.error_code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE

    unobservable = IsolatedProbeExecutor()
    _seed_repository(unobservable)
    unobservable.ok("repos/acme/widgets/issues?per_page=1", [])
    report = CommandGitHubCapabilityPreflight(unobservable, _server()).preflight(
        _ROOT,
        _request((GitHubOperationCapability.ISSUES_WRITE,), ()),
        _auth(),
    )
    result = _result(report, GitHubOperationCapability.ISSUES_WRITE)
    assert result.state is GitHubCapabilityEvidenceState.UNOBSERVABLE
    assert result.error_code is ErrorCode.GITHUB_ENTERPRISE_EVIDENCE_UNOBSERVABLE


def test_project_and_package_capabilities_remain_distinct_when_target_evidence_is_absent() -> None:
    executor = IsolatedProbeExecutor()
    _seed_repository(executor)
    adapter = CommandGitHubCapabilityPreflight(executor, _server())

    report = adapter.preflight(
        _ROOT,
        _request(
            (
                GitHubOperationCapability.PROJECTS_WRITE,
                GitHubOperationCapability.PACKAGES_READ,
            ),
            ("organization_projects:write", "organization_packages:read"),
        ),
        _auth(),
    )

    projects = _result(report, GitHubOperationCapability.PROJECTS_WRITE)
    packages = _result(report, GitHubOperationCapability.PACKAGES_READ)
    assert projects.state is GitHubCapabilityEvidenceState.UNOBSERVABLE
    assert packages.state is GitHubCapabilityEvidenceState.UNOBSERVABLE
    assert projects.reason_code != packages.reason_code
    assert [call["argv"] for call in executor.calls] == [_api_argv("repositories/123456")]
