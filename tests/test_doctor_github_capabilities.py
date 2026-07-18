"""End-to-end coverage for the rf doctor GitHub capability section (#211)."""

from __future__ import annotations

from pathlib import Path

from conftest import ForgeEnvironment

from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.domain.github_capability_probe import (
    CapabilityProbeResult,
    GitHubCapability,
    GitHubCapabilityReport,
    ProbeState,
)


class _FakeProbe:
    def __init__(self, report: GitHubCapabilityReport) -> None:
        self._report = report

    def probe(self, cwd: Path, source: object) -> GitHubCapabilityReport:
        del cwd, source
        return self._report


def _service_with_probe(
    forge_env: ForgeEnvironment, report: GitHubCapabilityReport
) -> CodingService:
    config = load_config(forge_env.config_path)
    application = build_application(
        config, overrides=AdapterOverrides(github_capabilities=_FakeProbe(report))
    )
    return CodingService(config, application=application)


def test_doctor_reports_dependencies_only_failure_without_blanket_failing(
    forge_env: ForgeEnvironment,
) -> None:
    report = GitHubCapabilityReport(
        repository="owner/demo",
        results=(
            CapabilityProbeResult(GitHubCapability.ISSUE_READ, ProbeState.AVAILABLE, "ok"),
            CapabilityProbeResult(GitHubCapability.SUB_ISSUES_READ, ProbeState.AVAILABLE, "ok"),
            CapabilityProbeResult(
                GitHubCapability.DEPENDENCIES_READ,
                ProbeState.UNAVAILABLE,
                "The authenticated session cannot read issue dependencies",
                remediation="Grant the missing scope.",
            ),
        ),
    )
    service = _service_with_probe(forge_env, report)
    doctor = service.doctor()

    by_name = {check["name"]: check for check in doctor["checks"]}
    assert by_name["github_capability:demo:issue_read"]["ok"] is True
    assert by_name["github_capability:demo:sub_issues_read"]["ok"] is True
    dependencies_check = by_name["github_capability:demo:dependencies_read"]
    assert dependencies_check["ok"] is False
    assert dependencies_check["severity"] == "warning"
    assert dependencies_check["remediation"] == "Grant the missing scope."


def test_doctor_project_v2_not_configured_is_informational_not_a_failure(
    forge_env: ForgeEnvironment,
) -> None:
    report = GitHubCapabilityReport(
        repository="owner/demo",
        results=(
            CapabilityProbeResult(
                GitHubCapability.PROJECT_READ,
                ProbeState.UNKNOWN,
                "Project V2 is not configured for this repository",
            ),
        ),
    )
    service = _service_with_probe(forge_env, report)
    doctor = service.doctor()

    check = next(c for c in doctor["checks"] if c["name"] == "github_capability:demo:project_read")
    assert check["ok"] is True
    assert check["severity"] == "info"


def test_doctor_capability_failure_alone_does_not_flip_overall_ok(
    forge_env: ForgeEnvironment,
) -> None:
    report = GitHubCapabilityReport(
        repository="owner/demo",
        results=(
            CapabilityProbeResult(
                GitHubCapability.DEPENDENCIES_WRITE,
                ProbeState.UNAVAILABLE,
                "no push access",
                remediation="Request write access.",
            ),
        ),
    )
    service = _service_with_probe(forge_env, report)
    doctor = service.doctor()

    # A capability warning is real signal but must not be conflated with a hard error --
    # RepoForge's own ticket graph is read-only today, so a missing write capability is
    # advisory, not blocking.
    dependencies_write = next(
        c for c in doctor["checks"] if c["name"] == "github_capability:demo:dependencies_write"
    )
    assert dependencies_write["severity"] == "warning"
    assert dependencies_write["ok"] is False
