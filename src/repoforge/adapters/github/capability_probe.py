"""Command-executed GitHub capability probes (#211): real API behavior, never scope strings."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from ...config import GitHubTicketGraphConfig, ServerConfig
from ...domain.github_capability_probe import (
    CapabilityProbeResult,
    GitHubCapability,
    GitHubCapabilityReport,
    ProbeState,
)
from ...ports.command import CommandExecutor, CommandResult

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_API_VERSION = "2022-11-28"
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")

_ISSUES_DISABLED = "Issues are disabled on this repository"
_PERMISSION_UNKNOWN = "Repository permissions were not returned by the GitHub API"
_PERMISSION_DERIVED = "Derived from the repository's push permission for the authenticated session"


class CommandGitHubCapabilityProbe:
    """Probe GitHub issue/sub-issue/dependency/project capabilities via bounded reads only."""

    def __init__(self, executor: CommandExecutor, server: ServerConfig) -> None:
        self._executor = executor
        self._server = server
        self._output_limit = min(max(server.max_tool_output_chars, 500_000), 5_000_000)

    def _run(self, argv: list[str], *, cwd: Path) -> CommandResult:
        return self._executor.run(
            argv,
            cwd=cwd,
            check=False,
            timeout=self._server.default_command_timeout_seconds,
            output_limit=self._output_limit,
        )

    def _api(self, cwd: Path, endpoint: str) -> CommandResult:
        return self._run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                endpoint,
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                f"X-GitHub-Api-Version: {_API_VERSION}",
            ],
            cwd=cwd,
        )

    def _slug(self, cwd: Path) -> str | None:
        result = self._run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=cwd,
        )
        slug = result.stdout.strip()
        if result.returncode != 0 or not _REPOSITORY.fullmatch(slug):
            return None
        return slug

    @staticmethod
    def _http_status(result: CommandResult) -> int | None:
        match = _HTTP_STATUS.search(result.combined)
        return int(match.group(1)) if match else None

    @staticmethod
    def _json_value(result: CommandResult) -> Any | None:
        if result.returncode != 0 or result.stdout_truncated:
            return None
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            return None

    def _probe_read(
        self,
        cwd: Path,
        capability: GitHubCapability,
        *,
        endpoint: str,
        expect: type,
        unavailable_detail: str,
        remediation: str,
    ) -> tuple[CapabilityProbeResult, CommandResult]:
        result = self._api(cwd, endpoint)
        if result.returncode == 0:
            payload = self._json_value(result)
            if isinstance(payload, expect):
                return (
                    CapabilityProbeResult(
                        capability, ProbeState.AVAILABLE, f"GET {endpoint} succeeded"
                    ),
                    result,
                )
            return (
                CapabilityProbeResult(
                    capability,
                    ProbeState.UNKNOWN,
                    f"GET {endpoint} succeeded but returned an unexpected payload shape",
                ),
                result,
            )
        status = self._http_status(result)
        if status in (401, 403, 404):
            return (
                CapabilityProbeResult(
                    capability, ProbeState.UNAVAILABLE, unavailable_detail, remediation=remediation
                ),
                result,
            )
        return (
            CapabilityProbeResult(
                capability,
                ProbeState.UNKNOWN,
                f"GET {endpoint} failed with an inconclusive error: {result.combined[:200]}",
            ),
            result,
        )

    @staticmethod
    def _write_result(
        capability: GitHubCapability, push_access: bool | None
    ) -> CapabilityProbeResult:
        if push_access is True:
            return CapabilityProbeResult(capability, ProbeState.AVAILABLE, _PERMISSION_DERIVED)
        if push_access is False:
            return CapabilityProbeResult(
                capability,
                ProbeState.UNAVAILABLE,
                _PERMISSION_DERIVED,
                remediation="Request write (push) access to this repository.",
            )
        return CapabilityProbeResult(capability, ProbeState.UNKNOWN, _PERMISSION_UNKNOWN)

    def _probe_issue_family(self, cwd: Path, slug: str) -> list[CapabilityProbeResult]:
        repo_meta = self._json_value(self._api(cwd, f"repos/{slug}"))
        push_access = (
            cast(bool, repo_meta["permissions"]["push"])
            if isinstance(repo_meta, dict)
            and isinstance(repo_meta.get("permissions"), dict)
            and isinstance(repo_meta["permissions"].get("push"), bool)
            else None
        )
        has_issues = repo_meta.get("has_issues") if isinstance(repo_meta, dict) else None

        if has_issues is False:
            return [
                CapabilityProbeResult(
                    GitHubCapability.ISSUE_READ,
                    ProbeState.UNAVAILABLE,
                    _ISSUES_DISABLED,
                    remediation="Enable Issues in the repository settings.",
                ),
                CapabilityProbeResult(
                    GitHubCapability.ISSUE_WRITE, ProbeState.UNAVAILABLE, _ISSUES_DISABLED
                ),
                CapabilityProbeResult(
                    GitHubCapability.SUB_ISSUES_READ, ProbeState.UNAVAILABLE, _ISSUES_DISABLED
                ),
                CapabilityProbeResult(
                    GitHubCapability.SUB_ISSUES_WRITE, ProbeState.UNAVAILABLE, _ISSUES_DISABLED
                ),
                CapabilityProbeResult(
                    GitHubCapability.DEPENDENCIES_READ, ProbeState.UNAVAILABLE, _ISSUES_DISABLED
                ),
                CapabilityProbeResult(
                    GitHubCapability.DEPENDENCIES_WRITE, ProbeState.UNAVAILABLE, _ISSUES_DISABLED
                ),
            ]

        results: list[CapabilityProbeResult] = []
        issue_read, issues_response = self._probe_read(
            cwd,
            GitHubCapability.ISSUE_READ,
            endpoint=f"repos/{slug}/issues?per_page=1",
            expect=list,
            unavailable_detail="The authenticated session cannot read issues on this repository",
            remediation="Run `gh auth login` with issue read access, or ask the operator to grant it.",
        )
        results.append(issue_read)
        results.append(self._write_result(GitHubCapability.ISSUE_WRITE, push_access))

        sample_issue: int | None = None
        if issue_read.state is ProbeState.AVAILABLE:
            issue_list = self._json_value(issues_response)
            if isinstance(issue_list, list) and issue_list:
                first = issue_list[0]
                if isinstance(first, dict) and isinstance(first.get("number"), int):
                    sample_issue = first["number"]

        if sample_issue is None:
            no_sample = "No existing issue was available to probe this capability"
            results.append(
                CapabilityProbeResult(
                    GitHubCapability.SUB_ISSUES_READ, ProbeState.UNKNOWN, no_sample
                )
            )
            results.append(
                CapabilityProbeResult(
                    GitHubCapability.DEPENDENCIES_READ, ProbeState.UNKNOWN, no_sample
                )
            )
        else:
            sub_issues_read, _ = self._probe_read(
                cwd,
                GitHubCapability.SUB_ISSUES_READ,
                endpoint=f"repos/{slug}/issues/{sample_issue}/sub_issues?per_page=1",
                expect=list,
                unavailable_detail="The authenticated session cannot read sub-issues",
                remediation="Sub-issues require the same access as issue read; verify repository access.",
            )
            results.append(sub_issues_read)
            dependencies_read, _ = self._probe_read(
                cwd,
                GitHubCapability.DEPENDENCIES_READ,
                endpoint=f"repos/{slug}/issues/{sample_issue}/dependencies/blocked_by?per_page=1",
                expect=list,
                unavailable_detail="The authenticated session cannot read issue dependencies",
                remediation="Dependencies require the same access as issue read; verify repository access.",
            )
            results.append(dependencies_read)

        results.append(self._write_result(GitHubCapability.SUB_ISSUES_WRITE, push_access))
        results.append(self._write_result(GitHubCapability.DEPENDENCIES_WRITE, push_access))
        return results

    def _probe_project(
        self, cwd: Path, source: GitHubTicketGraphConfig | None
    ) -> list[CapabilityProbeResult]:
        if source is None or source.project_owner is None or source.project_number is None:
            not_configured = "Project V2 is not configured for this repository"
            return [
                CapabilityProbeResult(
                    GitHubCapability.PROJECT_READ, ProbeState.UNKNOWN, not_configured
                ),
                CapabilityProbeResult(
                    GitHubCapability.PROJECT_WRITE, ProbeState.UNKNOWN, not_configured
                ),
            ]

        project_result = self._run(
            [
                "gh",
                "project",
                "view",
                str(source.project_number),
                "--owner",
                source.project_owner,
                "--format",
                "json",
            ],
            cwd=cwd,
        )
        project_payload = self._json_value(project_result)
        if (
            project_result.returncode == 0
            and isinstance(project_payload, dict)
            and project_payload.get("id")
        ):
            read_result = CapabilityProbeResult(
                GitHubCapability.PROJECT_READ, ProbeState.AVAILABLE, "gh project view succeeded"
            )
        elif project_result.returncode != 0:
            read_result = CapabilityProbeResult(
                GitHubCapability.PROJECT_READ,
                ProbeState.UNAVAILABLE,
                "The authenticated session cannot read this Project V2 board",
                remediation="Grant `read:project` access, or `project` for read/write.",
            )
        else:
            read_result = CapabilityProbeResult(
                GitHubCapability.PROJECT_READ,
                ProbeState.UNKNOWN,
                "gh project view returned an unexpected payload shape",
            )

        # GitHub exposes no non-mutating signal for Project V2 write access; confirming it would
        # require an actual write, which this probe deliberately never performs.
        write_result = CapabilityProbeResult(
            GitHubCapability.PROJECT_WRITE,
            ProbeState.UNKNOWN,
            "Project V2 write access can only be confirmed by an actual write attempt; this "
            "probe never performs one",
            remediation="Grant the `project` scope if a write is intended.",
        )
        return [read_result, write_result]

    def probe(
        self,
        cwd: Path,
        source: GitHubTicketGraphConfig | None,
    ) -> GitHubCapabilityReport:
        slug = self._slug(cwd)
        if slug is None:
            unauthenticated = "GitHub CLI is not authenticated against this repository"
            return GitHubCapabilityReport(
                repository=None,
                results=tuple(
                    CapabilityProbeResult(capability, ProbeState.UNKNOWN, unauthenticated)
                    for capability in GitHubCapability
                ),
            )

        results = self._probe_issue_family(cwd, slug)
        results.extend(self._probe_project(cwd, source))
        return GitHubCapabilityReport(repository=slug, results=tuple(results))
