"""Coverage for the GitHub capability probe adapter (#211): real API behavior only, never
token-scope string matching, and UNKNOWN whenever evidence is inconclusive."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from repoforge.adapters.github.capability_probe import CommandGitHubCapabilityProbe
from repoforge.config import GitHubTicketGraphConfig, ServerConfig
from repoforge.domain.github_capability_probe import GitHubCapability, ProbeState
from repoforge.ports.cancellation import CancellationToken
from repoforge.ports.command import CommandResult

_API_VERSION = "2022-11-28"
_HEADERS = (
    "-H",
    "Accept: application/vnd.github+json",
    "-H",
    f"X-GitHub-Api-Version: {_API_VERSION}",
)


def _api_argv(endpoint: str) -> tuple[str, ...]:
    return ("gh", "api", "--method", "GET", endpoint, *_HEADERS)


def _slug_argv() -> tuple[str, ...]:
    return ("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")


def _project_argv(number: int, owner: str) -> tuple[str, ...]:
    return ("gh", "project", "view", str(number), "--owner", owner, "--format", "json")


class ProbeExecutor:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], CommandResult] = {}
        self.calls: list[tuple[str, ...]] = []

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

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
        del input_text, timeout, check, extra_env, output_limit, cancel_token
        command = tuple(argv)
        self.calls.append(command)
        if command not in self.responses:
            raise AssertionError(f"unhandled command: {command}")
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
        raise AssertionError("run_bytes is not used by capability probes")

    def ok(self, argv: tuple[str, ...], payload: object) -> None:
        self.responses[argv] = CommandResult(argv, "/repo", 0, json.dumps(payload), "")

    def http_error(self, argv: tuple[str, ...], status: int, message: str = "error") -> None:
        self.responses[argv] = CommandResult(argv, "/repo", 1, "", f"gh: {message} (HTTP {status})")


def _server() -> ServerConfig:
    return ServerConfig(workspace_root=Path("/workspaces"), state_root=Path("/state"))


def _repo_payload(*, has_issues: bool = True, push: bool | None = True) -> dict[str, object]:
    payload: dict[str, object] = {"has_issues": has_issues}
    if push is not None:
        payload["permissions"] = {"push": push}
    return payload


def _seed_authenticated_repo(
    executor: ProbeExecutor, *, has_issues: bool = True, push: bool | None = True
) -> None:
    executor.responses[_slug_argv()] = CommandResult(_slug_argv(), "/repo", 0, "acme/widgets\n", "")
    executor.ok(_api_argv("repos/acme/widgets"), _repo_payload(has_issues=has_issues, push=push))


def test_all_capabilities_available_for_a_fully_authorized_write_token() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor)
    executor.ok(_api_argv("repos/acme/widgets/issues?per_page=1"), [{"number": 7}])
    executor.ok(_api_argv("repos/acme/widgets/issues/7/sub_issues?per_page=1"), [])
    executor.ok(_api_argv("repos/acme/widgets/issues/7/dependencies/blocked_by?per_page=1"), [])

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    assert report.repository == "acme/widgets"
    for capability in (
        GitHubCapability.ISSUE_READ,
        GitHubCapability.ISSUE_WRITE,
        GitHubCapability.SUB_ISSUES_READ,
        GitHubCapability.SUB_ISSUES_WRITE,
        GitHubCapability.DEPENDENCIES_READ,
        GitHubCapability.DEPENDENCIES_WRITE,
    ):
        result = report.get(capability)
        assert result is not None
        assert result.state is ProbeState.AVAILABLE, capability


def test_read_only_token_reports_writes_unavailable_without_touching_reads() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor, push=False)
    executor.ok(_api_argv("repos/acme/widgets/issues?per_page=1"), [{"number": 7}])
    executor.ok(_api_argv("repos/acme/widgets/issues/7/sub_issues?per_page=1"), [])
    executor.ok(_api_argv("repos/acme/widgets/issues/7/dependencies/blocked_by?per_page=1"), [])

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    for capability in (
        GitHubCapability.ISSUE_READ,
        GitHubCapability.SUB_ISSUES_READ,
        GitHubCapability.DEPENDENCIES_READ,
    ):
        assert report.get(capability).state is ProbeState.AVAILABLE  # type: ignore[union-attr]
    for capability in (
        GitHubCapability.ISSUE_WRITE,
        GitHubCapability.SUB_ISSUES_WRITE,
        GitHubCapability.DEPENDENCIES_WRITE,
    ):
        assert report.get(capability).state is ProbeState.UNAVAILABLE  # type: ignore[union-attr]


def test_dependencies_only_failure_does_not_blanket_fail_the_report() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor)
    executor.ok(_api_argv("repos/acme/widgets/issues?per_page=1"), [{"number": 7}])
    executor.ok(_api_argv("repos/acme/widgets/issues/7/sub_issues?per_page=1"), [])
    executor.http_error(
        _api_argv("repos/acme/widgets/issues/7/dependencies/blocked_by?per_page=1"), 403
    )

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    assert report.get(GitHubCapability.ISSUE_READ).state is ProbeState.AVAILABLE  # type: ignore[union-attr]
    assert report.get(GitHubCapability.SUB_ISSUES_READ).state is ProbeState.AVAILABLE  # type: ignore[union-attr]
    dependencies = report.get(GitHubCapability.DEPENDENCIES_READ)
    assert dependencies is not None
    assert dependencies.state is ProbeState.UNAVAILABLE
    assert dependencies.remediation


def test_issues_disabled_marks_the_whole_issue_family_unavailable_without_probing() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor, has_issues=False)

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    for capability in (
        GitHubCapability.ISSUE_READ,
        GitHubCapability.ISSUE_WRITE,
        GitHubCapability.SUB_ISSUES_READ,
        GitHubCapability.SUB_ISSUES_WRITE,
        GitHubCapability.DEPENDENCIES_READ,
        GitHubCapability.DEPENDENCIES_WRITE,
    ):
        assert report.get(capability).state is ProbeState.UNAVAILABLE  # type: ignore[union-attr]
    # No issue-scoped endpoint should have been called once has_issues is known false.
    assert not any("issues?per_page" in "".join(call) for call in executor.calls)


def test_zero_issues_leaves_sub_issue_and_dependency_read_unknown() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor)
    executor.ok(_api_argv("repos/acme/widgets/issues?per_page=1"), [])

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    assert report.get(GitHubCapability.ISSUE_READ).state is ProbeState.AVAILABLE  # type: ignore[union-attr]
    assert report.get(GitHubCapability.SUB_ISSUES_READ).state is ProbeState.UNKNOWN  # type: ignore[union-attr]
    assert report.get(GitHubCapability.DEPENDENCIES_READ).state is ProbeState.UNKNOWN  # type: ignore[union-attr]


def test_inconclusive_error_reports_unknown_never_guessing_unavailable() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor)
    executor.http_error(_api_argv("repos/acme/widgets/issues?per_page=1"), 500, "internal error")

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    result = report.get(GitHubCapability.ISSUE_READ)
    assert result is not None
    assert result.state is ProbeState.UNKNOWN


def test_project_v2_not_configured_reports_unknown_without_calling_gh_project() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor)
    executor.ok(_api_argv("repos/acme/widgets/issues?per_page=1"), [])

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    assert report.get(GitHubCapability.PROJECT_READ).state is ProbeState.UNKNOWN  # type: ignore[union-attr]
    assert report.get(GitHubCapability.PROJECT_WRITE).state is ProbeState.UNKNOWN  # type: ignore[union-attr]
    assert not any(call[:2] == ("gh", "project") for call in executor.calls)


def test_project_v2_read_available_when_configured_and_reachable() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor)
    executor.ok(_api_argv("repos/acme/widgets/issues?per_page=1"), [])
    executor.ok(_project_argv(7, "acme"), {"id": "PVT_kw123"})

    source = GitHubTicketGraphConfig(root_issue=1, project_owner="acme", project_number=7)
    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), source)

    assert report.get(GitHubCapability.PROJECT_READ).state is ProbeState.AVAILABLE  # type: ignore[union-attr]
    # Read success must never be penalized for lacking write ("project") scope.
    assert report.get(GitHubCapability.PROJECT_WRITE).state is ProbeState.UNKNOWN  # type: ignore[union-attr]


def test_project_v2_read_unavailable_on_permission_error() -> None:
    executor = ProbeExecutor()
    _seed_authenticated_repo(executor)
    executor.ok(_api_argv("repos/acme/widgets/issues?per_page=1"), [])
    executor.http_error(_project_argv(7, "acme"), 403, "not accessible")

    source = GitHubTicketGraphConfig(root_issue=1, project_owner="acme", project_number=7)
    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), source)

    result = report.get(GitHubCapability.PROJECT_READ)
    assert result is not None
    assert result.state is ProbeState.UNAVAILABLE


def test_unauthenticated_session_reports_unknown_for_every_capability() -> None:
    executor = ProbeExecutor()
    executor.responses[_slug_argv()] = CommandResult(
        _slug_argv(), "/repo", 1, "", "gh: not logged in"
    )

    probe = CommandGitHubCapabilityProbe(executor, _server())
    report = probe.probe(Path("/repo"), None)

    assert report.repository is None
    assert len(report.results) == len(GitHubCapability)
    assert all(result.state is ProbeState.UNKNOWN for result in report.results)
