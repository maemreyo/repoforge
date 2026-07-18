"""Typed result model for GitHub capability probes (#211).

A probe answers "can this authenticated GitHub CLI session actually do X" by observing real
API behavior -- repository permission payloads and bounded read attempts -- never by parsing
token-scope strings, which fail silently for fine-grained PATs and GitHub Apps and collapse
distinct capabilities into one blanket signal. Every capability resolves to one of three
states; UNKNOWN is always preferred over a guessed availability.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GitHubCapability(str, Enum):
    ISSUE_READ = "issue_read"
    ISSUE_WRITE = "issue_write"
    SUB_ISSUES_READ = "sub_issues_read"
    SUB_ISSUES_WRITE = "sub_issues_write"
    DEPENDENCIES_READ = "dependencies_read"
    DEPENDENCIES_WRITE = "dependencies_write"
    PROJECT_READ = "project_read"
    PROJECT_WRITE = "project_write"


class ProbeState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CapabilityProbeResult:
    capability: GitHubCapability
    state: ProbeState
    detail: str
    remediation: str | None = None

    def as_dict(self) -> dict[str, object]:
        item: dict[str, object] = {
            "capability": self.capability.value,
            "state": self.state.value,
            "detail": self.detail,
        }
        if self.remediation:
            item["remediation"] = self.remediation
        return item


@dataclass(frozen=True, slots=True)
class GitHubCapabilityReport:
    repository: str | None
    results: tuple[CapabilityProbeResult, ...]

    def get(self, capability: GitHubCapability) -> CapabilityProbeResult | None:
        for result in self.results:
            if result.capability is capability:
                return result
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "results": [result.as_dict() for result in self.results],
        }
