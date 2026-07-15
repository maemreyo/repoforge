"""Pure presentation models for the guided-onboarding review flow."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class DefaultsMode(str, Enum):
    SAFE = "safe"
    ASK = "ask"
    NONE = "none"


def resolve_defaults_mode(requested: str | None, *, non_interactive: bool) -> DefaultsMode:
    if non_interactive:
        if requested not in {None, DefaultsMode.NONE.value, DefaultsMode.SAFE.value}:
            raise ValueError("--defaults ask is interactive-only")
        return (
            DefaultsMode.NONE if requested in {None, DefaultsMode.NONE.value} else DefaultsMode.SAFE
        )
    return DefaultsMode(requested or DefaultsMode.SAFE.value)


@dataclass(frozen=True, slots=True)
class RepositorySummary:
    repo_id: str
    path: str
    confidence: str
    mode: str
    remote: str
    base: str
    publishing: str
    profiles: str
    budget: str
    findings: str

    def row(self) -> tuple[str, ...]:
        return (
            self.repo_id,
            self.mode,
            self.confidence,
            self.remote,
            self.base,
            self.profiles,
            self.findings,
        )


def discovery_rows(
    result: Any,
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    eligible = tuple(
        (
            str(item.repo_id),
            str(item.identity.path),
            str(item.parent_repo_id or "root"),
        )
        for item in result.eligible
    )
    excluded = tuple(
        (
            str(item.path),
            str(getattr(item.reason, "value", item.reason)),
            str(item.detail or ""),
        )
        for item in result.exclusions
    )
    return eligible, excluded


def proposal_summary(state: Any) -> RepositorySummary:
    try:
        decoded = json.loads(state.proposal_json or "{}")
    except json.JSONDecodeError:
        decoded = {}
    proposal = decoded if isinstance(decoded, dict) else {}
    raw_policy = proposal.get("policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    raw_profiles = policy.get("profiles")
    profiles = raw_profiles if isinstance(raw_profiles, list) else []
    profile_names = tuple(
        str(item.get("name")) for item in profiles if isinstance(item, dict) and item.get("name")
    )
    raw_findings = proposal.get("findings")
    findings = raw_findings if isinstance(raw_findings, list) else []
    finding_codes = tuple(
        str(item.get("code")) for item in findings if isinstance(item, dict) and item.get("code")
    )
    max_files = policy.get("max_changed_files", "?")
    max_lines = policy.get("max_diff_lines", "?")
    max_bytes = policy.get("max_total_changed_bytes", "?")
    return RepositorySummary(
        repo_id=str(state.candidate.repo_id),
        path=str(proposal.get("path", state.candidate.identity.path)),
        confidence=str(proposal.get("confidence", "unknown")),
        mode=str(policy.get("mode", state.template)),
        remote=str(policy.get("remote") or "none"),
        base=str(policy.get("default_base") or "none"),
        publishing="enabled" if policy.get("publish_enabled") else "disabled",
        profiles=", ".join(profile_names) if profile_names else "none",
        budget=f"{max_files} files / {max_lines} lines / {max_bytes} bytes",
        findings=", ".join(finding_codes) if finding_codes else "none",
    )


def configuration_diff(current_text: str, proposed_text: str) -> str:
    return (
        "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile="current-config.toml",
                tofile="proposed-config.toml",
            )
        )
        or "# No source configuration changes.\n"
    )
