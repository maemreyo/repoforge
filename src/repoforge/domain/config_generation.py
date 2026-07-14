"""Pure immutable configuration generation and semantic delta model."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import tomli as tomllib

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class CapabilityDeltaKind(str, Enum):
    EQUIVALENT = "equivalent"
    METADATA_ONLY = "metadata_only"
    EXPANSION = "expansion"
    RESTRICTION = "restriction"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class CapabilityChange:
    path: str
    before: object
    after: object
    direction: CapabilityDeltaKind
    reason: str


@dataclass(frozen=True, slots=True)
class CapabilityDelta:
    kind: CapabilityDeltaKind
    before_sha256: str
    after_sha256: str
    changes: tuple[CapabilityChange, ...]

    @property
    def before(self) -> str:
        """Backward-compatible canonical before identifier."""
        return self.before_sha256

    @property
    def after(self) -> str:
        """Backward-compatible canonical after identifier."""
        return self.after_sha256


@dataclass(frozen=True, slots=True)
class ApprovalEvent:
    actor: str
    approved_at: str
    proposal_id: str
    approval_token_sha256: str

    def __post_init__(self) -> None:
        if (
            not self.actor
            or not self.approved_at
            or not self.proposal_id
            or not _SHA256.fullmatch(self.approval_token_sha256)
        ):
            raise ValueError("Approval event is incomplete or has an invalid token hash")


@dataclass(frozen=True, slots=True)
class ConfigGeneration:
    generation: int
    source_sha256: str
    resolved_sha256: str
    repository_fingerprints: tuple[tuple[str, str], ...]
    created_at: str
    reason: str
    proposal_id: str | None
    approval: ApprovalEvent | None
    delta: CapabilityDeltaKind
    previous_generation: int | None
    correlation_id: str = ""
    active: bool = False

    def __post_init__(self) -> None:
        if self.generation <= 0:
            raise ValueError("Config generation must be positive")
        if not _SHA256.fullmatch(self.source_sha256) or not _SHA256.fullmatch(self.resolved_sha256):
            raise ValueError("Config generation hashes must be lowercase SHA-256")
        if not self.created_at or not self.reason:
            raise ValueError("Config generation requires creation time and reason")
        if self.previous_generation is not None and (
            self.previous_generation <= 0 or self.previous_generation >= self.generation
        ):
            raise ValueError("Previous generation must be positive and older")
        for repo_id, fingerprint in self.repository_fingerprints:
            if not repo_id or not _SHA256.fullmatch(fingerprint):
                raise ValueError("Repository generation fingerprint is invalid")
        if self.approval is not None and self.proposal_id != self.approval.proposal_id:
            raise ValueError("Generation approval does not match proposal")

    def repository_fingerprint_map(self) -> dict[str, str]:
        return dict(self.repository_fingerprints)


@dataclass(frozen=True, slots=True)
class ConfigMutation:
    source_text: str
    resolved_text: str
    repository_fingerprints: tuple[tuple[str, str], ...]
    reason: str
    created_at: str
    expected_generation: int | None
    expected_source_sha256: str | None
    proposal_id: str | None = None
    approval: ApprovalEvent | None = None
    correlation_id: str = ""

    def __post_init__(self) -> None:
        if not self.source_text or not self.resolved_text or not self.reason or not self.created_at:
            raise ValueError(
                "Config mutation requires source, resolved policy, reason and timestamp"
            )
        if self.expected_generation is not None and self.expected_generation < 0:
            raise ValueError("Expected generation cannot be negative")
        if self.expected_source_sha256 is not None and not _SHA256.fullmatch(
            self.expected_source_sha256
        ):
            raise ValueError("Expected source hash must be lowercase SHA-256")
        for repo_id, fingerprint in self.repository_fingerprints:
            if not repo_id or not _SHA256.fullmatch(fingerprint):
                raise ValueError("Repository mutation fingerprint is invalid")
        if self.approval is not None and self.proposal_id != self.approval.proposal_id:
            raise ValueError("Mutation approval does not match proposal")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_config(text: str) -> dict[str, Any]:
    """Parse resolved TOML and remove generation/proposal bookkeeping from policy semantics."""
    value = tomllib.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Resolved configuration must be a TOML table")
    result = json.loads(json.dumps(value))
    result.pop("repoforge_lock", None)
    return dict(result)


def _repo_map(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    repos = value.get("repositories", {})
    if not isinstance(repos, dict):
        return {}
    return {str(key): item for key, item in repos.items() if isinstance(item, dict)}


def _set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value}


def _profile_map(repo: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = repo.get("profiles", {})
    if not isinstance(profiles, dict):
        return {}
    return {str(name): raw for name, raw in profiles.items() if isinstance(raw, dict)}


def _profile_commands(profile: dict[str, Any]) -> set[str]:
    commands = profile.get("commands", [])
    if not isinstance(commands, list):
        return set()
    return {
        json.dumps([str(item) for item in command], separators=(",", ":"))
        for command in commands
        if isinstance(command, list) and all(isinstance(item, str) for item in command)
    }


def _diagnostic_map(repo: dict[str, Any]) -> dict[str, dict[str, Any]]:
    diagnostics = repo.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        return {}
    return {str(name): raw for name, raw in diagnostics.items() if isinstance(raw, dict)}


def _argv_identity(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(value)


def _record_profile_changes(
    changes: list[CapabilityChange],
    prefix: str,
    before_repo: dict[str, Any],
    after_repo: dict[str, Any],
) -> None:
    before = _profile_map(before_repo)
    after = _profile_map(after_repo)
    _record_set_change(
        changes,
        prefix,
        set(before),
        set(after),
        additions=CapabilityDeltaKind.EXPANSION,
        removals=CapabilityDeltaKind.RESTRICTION,
        reason="executable profile availability changed",
    )
    for name in sorted(set(before) & set(after)):
        left = before[name]
        right = after[name]
        profile_prefix = f"{prefix}.{name}"
        _record_set_change(
            changes,
            profile_prefix + ".commands",
            _profile_commands(left),
            _profile_commands(right),
            additions=CapabilityDeltaKind.EXPANSION,
            removals=CapabilityDeltaKind.RESTRICTION,
            reason="executable command capability changed",
        )
        _record_bool(
            changes,
            profile_prefix + ".verification",
            bool(left.get("verification", False)),
            bool(right.get("verification", False)),
            true_is_restriction=False,
            reason="verification profile eligibility changed",
        )
        _record_number(
            changes,
            profile_prefix + ".timeout_seconds",
            left.get("timeout_seconds", 0),
            right.get("timeout_seconds", 0),
            reason="profile process duration",
        )
        if left.get("working_directory") != right.get("working_directory"):
            changes.append(
                CapabilityChange(
                    profile_prefix + ".working_directory",
                    left.get("working_directory"),
                    right.get("working_directory"),
                    CapabilityDeltaKind.INCOMPATIBLE,
                    "profile execution scope changed",
                )
            )


def _record_diagnostic_changes(
    changes: list[CapabilityChange],
    prefix: str,
    before_repo: dict[str, Any],
    after_repo: dict[str, Any],
) -> None:
    before = _diagnostic_map(before_repo)
    after = _diagnostic_map(after_repo)
    _record_set_change(
        changes,
        prefix,
        set(before),
        set(after),
        additions=CapabilityDeltaKind.EXPANSION,
        removals=CapabilityDeltaKind.RESTRICTION,
        reason="reviewed diagnostic availability changed",
    )
    for diagnostic_id in sorted(set(before) & set(after)):
        left = before[diagnostic_id]
        right = after[diagnostic_id]
        diagnostic_prefix = f"{prefix}.{diagnostic_id}"
        for field, reason in (
            ("argv", "diagnostic executable or argument template changed"),
            ("selector_kind", "diagnostic selector type changed"),
            ("working_directory", "diagnostic execution scope changed"),
            ("network_policy", "diagnostic network policy changed"),
            ("mutability", "diagnostic mutation policy changed"),
            ("parser", "diagnostic output parser changed"),
        ):
            left_value = _argv_identity(left.get(field)) if field == "argv" else left.get(field)
            right_value = _argv_identity(right.get(field)) if field == "argv" else right.get(field)
            if left_value != right_value:
                changes.append(
                    CapabilityChange(
                        f"{diagnostic_prefix}.{field}",
                        left_value,
                        right_value,
                        CapabilityDeltaKind.INCOMPATIBLE,
                        reason,
                    )
                )
        for field, reason in (
            ("selector_values", "diagnostic selector allowlist changed"),
            ("artifact_paths", "diagnostic artifact path capability changed"),
        ):
            _record_set_change(
                changes,
                f"{diagnostic_prefix}.{field}",
                _set(left.get(field)),
                _set(right.get(field)),
                additions=CapabilityDeltaKind.EXPANSION,
                removals=CapabilityDeltaKind.RESTRICTION,
                reason=reason,
            )
        for field, reason in (
            ("timeout_seconds", "diagnostic process duration"),
            ("output_limit", "diagnostic output disclosure bound"),
        ):
            _record_number(
                changes,
                f"{diagnostic_prefix}.{field}",
                left.get(field, 0),
                right.get(field, 0),
                reason=reason,
            )


def _record_set_change(
    changes: list[CapabilityChange],
    path: str,
    before: set[str],
    after: set[str],
    *,
    additions: CapabilityDeltaKind,
    removals: CapabilityDeltaKind,
    reason: str,
) -> None:
    added = tuple(sorted(after - before))
    removed = tuple(sorted(before - after))
    if added:
        changes.append(CapabilityChange(path, (), added, additions, reason))
    if removed:
        changes.append(CapabilityChange(path, removed, (), removals, reason))


def _record_allowed_paths(
    changes: list[CapabilityChange], path: str, before: set[str], after: set[str]
) -> None:
    # Empty means every path not denied. Converting all -> explicit subset is a restriction;
    # converting subset -> all is an expansion. For two explicit sets, normal set direction applies.
    if before == after:
        return
    if not before and after:
        changes.append(
            CapabilityChange(
                path,
                ("<all>",),
                tuple(sorted(after)),
                CapabilityDeltaKind.RESTRICTION,
                "path allowlist narrowed from all paths",
            )
        )
        return
    if before and not after:
        changes.append(
            CapabilityChange(
                path,
                tuple(sorted(before)),
                ("<all>",),
                CapabilityDeltaKind.EXPANSION,
                "path allowlist widened to all non-denied paths",
            )
        )
        return
    _record_set_change(
        changes,
        path,
        before,
        after,
        additions=CapabilityDeltaKind.EXPANSION,
        removals=CapabilityDeltaKind.RESTRICTION,
        reason="path allowlist changed",
    )


def _record_bool(
    changes: list[CapabilityChange],
    path: str,
    before: bool,
    after: bool,
    *,
    true_is_restriction: bool,
    reason: str,
) -> None:
    if before == after:
        return
    restriction = after if true_is_restriction else not after
    changes.append(
        CapabilityChange(
            path,
            before,
            after,
            CapabilityDeltaKind.RESTRICTION if restriction else CapabilityDeltaKind.EXPANSION,
            reason,
        )
    )


def _record_number(
    changes: list[CapabilityChange],
    path: str,
    before: object,
    after: object,
    *,
    reason: str,
) -> None:
    if (
        isinstance(before, bool)
        or isinstance(after, bool)
        or not isinstance(before, (int, str))
        or not isinstance(after, (int, str))
    ):
        if before != after:
            changes.append(
                CapabilityChange(
                    path,
                    before,
                    after,
                    CapabilityDeltaKind.INCOMPATIBLE,
                    f"{reason} changed to a non-comparable value",
                )
            )
        return
    try:
        left = int(before)
        right = int(after)
    except ValueError:
        if before != after:
            changes.append(
                CapabilityChange(
                    path,
                    before,
                    after,
                    CapabilityDeltaKind.INCOMPATIBLE,
                    f"{reason} changed to a non-comparable value",
                )
            )
        return
    if left != right:
        changes.append(
            CapabilityChange(
                path,
                left,
                right,
                CapabilityDeltaKind.EXPANSION if right > left else CapabilityDeltaKind.RESTRICTION,
                f"{reason} increased" if right > left else f"{reason} decreased",
            )
        )


_REPO_RECOGNIZED = {
    "path",
    "display_name",
    "remote",
    "default_base",
    "allowed_base_branches",
    "branch_prefix",
    "protected_branches",
    "read_only",
    "publish_enabled",
    "require_verification_before_commit",
    "fetch_before_workspace",
    "default_verification_profile",
    "max_changed_files",
    "max_diff_lines",
    "max_total_changed_bytes",
    "allowed_paths",
    "denied_paths",
    "pr_labels",
    "pr_reviewers",
    "no_maintainer_edit",
    "profiles",
    "diagnostics",
}
_SERVER_RECOGNIZED = {
    "workspace_root",
    "state_root",
    "max_file_bytes",
    "max_tool_output_chars",
    "default_command_timeout_seconds",
    "verification_timeout_seconds",
    "max_fingerprint_bytes",
    "max_batch_files",
    "path_prefixes",
    "allowed_environment",
}


def _unclassified(value: dict[str, Any]) -> dict[str, Any]:
    loaded: Any = json.loads(json.dumps(value))
    if not isinstance(loaded, dict):
        return {}
    residual: dict[str, Any] = loaded
    server = residual.get("server")
    if isinstance(server, dict):
        for key in _SERVER_RECOGNIZED:
            server.pop(key, None)
        if not server:
            residual.pop("server", None)
    repos = residual.get("repositories")
    if isinstance(repos, dict):
        for repo_id in list(repos):
            repo = repos[repo_id]
            if isinstance(repo, dict):
                for key in _REPO_RECOGNIZED:
                    repo.pop(key, None)
                if not repo:
                    repos.pop(repo_id, None)
        if not repos:
            residual.pop("repositories", None)
    return residual


def _metadata(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"repositories": {}}
    for repo_id, repo in _repo_map(value).items():
        profiles = repo.get("profiles", {})
        descriptions = (
            {
                str(name): raw.get("description")
                for name, raw in profiles.items()
                if isinstance(profiles, dict) and isinstance(raw, dict)
            }
            if isinstance(profiles, dict)
            else {}
        )
        diagnostics = repo.get("diagnostics", {})
        diagnostic_summaries = (
            {
                str(name): raw.get("summary")
                for name, raw in diagnostics.items()
                if isinstance(diagnostics, dict) and isinstance(raw, dict)
            }
            if isinstance(diagnostics, dict)
            else {}
        )
        result["repositories"][repo_id] = {
            "display_name": repo.get("display_name"),
            "profile_descriptions": descriptions,
            "diagnostic_summaries": diagnostic_summaries,
        }
    return result


def classify_capability_delta(before_text: str, after_text: str) -> CapabilityDelta:
    """Classify policy changes using field semantics instead of structural set heuristics."""
    before = canonical_config(before_text)
    after = canonical_config(after_text)
    before_json = json.dumps(before, sort_keys=True, separators=(",", ":"))
    after_json = json.dumps(after, sort_keys=True, separators=(",", ":"))
    before_sha = sha256_text(before_json)
    after_sha = sha256_text(after_json)
    if before_json == after_json:
        return CapabilityDelta(CapabilityDeltaKind.EQUIVALENT, before_sha, after_sha, ())

    changes: list[CapabilityChange] = []
    before_repos = _repo_map(before)
    after_repos = _repo_map(after)
    _record_set_change(
        changes,
        "repositories",
        set(before_repos),
        set(after_repos),
        additions=CapabilityDeltaKind.EXPANSION,
        removals=CapabilityDeltaKind.RESTRICTION,
        reason="repository access changed",
    )

    for repo_id in sorted(set(before_repos) & set(after_repos)):
        left = before_repos[repo_id]
        right = after_repos[repo_id]
        prefix = f"repositories.{repo_id}"
        identity_fields = ("path", "remote", "default_base", "branch_prefix")
        identity_before = tuple(left.get(field) for field in identity_fields)
        identity_after = tuple(right.get(field) for field in identity_fields)
        if identity_before != identity_after:
            changes.append(
                CapabilityChange(
                    prefix + ".identity",
                    identity_before,
                    identity_after,
                    CapabilityDeltaKind.INCOMPATIBLE,
                    "repository identity, publication target, base, or branch namespace changed",
                )
            )
        if left.get("default_verification_profile") != right.get("default_verification_profile"):
            changes.append(
                CapabilityChange(
                    prefix + ".default_verification_profile",
                    left.get("default_verification_profile"),
                    right.get("default_verification_profile"),
                    CapabilityDeltaKind.INCOMPATIBLE,
                    "default executable verification behavior changed",
                )
            )
        _record_set_change(
            changes,
            prefix + ".allowed_base_branches",
            _set(left.get("allowed_base_branches")),
            _set(right.get("allowed_base_branches")),
            additions=CapabilityDeltaKind.EXPANSION,
            removals=CapabilityDeltaKind.RESTRICTION,
            reason="base branch allowlist changed",
        )
        _record_allowed_paths(
            changes,
            prefix + ".allowed_paths",
            _set(left.get("allowed_paths")),
            _set(right.get("allowed_paths")),
        )
        for field, add_direction, remove_direction, reason in (
            (
                "denied_paths",
                CapabilityDeltaKind.RESTRICTION,
                CapabilityDeltaKind.EXPANSION,
                "path deny policy changed",
            ),
            (
                "protected_branches",
                CapabilityDeltaKind.RESTRICTION,
                CapabilityDeltaKind.EXPANSION,
                "protected branch policy changed",
            ),
            (
                "pr_labels",
                CapabilityDeltaKind.EXPANSION,
                CapabilityDeltaKind.RESTRICTION,
                "automatic pull-request labels changed",
            ),
            (
                "pr_reviewers",
                CapabilityDeltaKind.EXPANSION,
                CapabilityDeltaKind.RESTRICTION,
                "automatic reviewer notifications changed",
            ),
        ):
            _record_set_change(
                changes,
                f"{prefix}.{field}",
                _set(left.get(field)),
                _set(right.get(field)),
                additions=add_direction,
                removals=remove_direction,
                reason=reason,
            )
        _record_profile_changes(changes, prefix + ".profiles", left, right)
        _record_diagnostic_changes(changes, prefix + ".diagnostics", left, right)
        for field, reason in (
            ("max_changed_files", "changed-file budget"),
            ("max_diff_lines", "diff-line budget"),
            ("max_total_changed_bytes", "changed-byte budget"),
        ):
            _record_number(
                changes,
                f"{prefix}.{field}",
                left.get(field, 0),
                right.get(field, 0),
                reason=reason,
            )
        _record_bool(
            changes,
            prefix + ".read_only",
            bool(left.get("read_only", False)),
            bool(right.get("read_only", False)),
            true_is_restriction=True,
            reason="repository write capability changed",
        )
        _record_bool(
            changes,
            prefix + ".publish_enabled",
            bool(left.get("publish_enabled", True)),
            bool(right.get("publish_enabled", True)),
            true_is_restriction=False,
            reason="repository publishing capability changed",
        )
        _record_bool(
            changes,
            prefix + ".require_verification_before_commit",
            bool(left.get("require_verification_before_commit", True)),
            bool(right.get("require_verification_before_commit", True)),
            true_is_restriction=True,
            reason="verified commit gate changed",
        )
        _record_bool(
            changes,
            prefix + ".no_maintainer_edit",
            bool(left.get("no_maintainer_edit", False)),
            bool(right.get("no_maintainer_edit", False)),
            true_is_restriction=True,
            reason="maintainer edit permission changed",
        )
        _record_bool(
            changes,
            prefix + ".fetch_before_workspace",
            bool(left.get("fetch_before_workspace", True)),
            bool(right.get("fetch_before_workspace", True)),
            true_is_restriction=False,
            reason="network fetch capability changed",
        )

    before_server = before.get("server", {}) if isinstance(before.get("server"), dict) else {}
    after_server = after.get("server", {}) if isinstance(after.get("server"), dict) else {}
    if before_server.get("workspace_root") != after_server.get(
        "workspace_root"
    ) or before_server.get("state_root") != after_server.get("state_root"):
        changes.append(
            CapabilityChange(
                "server.storage_identity",
                (before_server.get("workspace_root"), before_server.get("state_root")),
                (after_server.get("workspace_root"), after_server.get("state_root")),
                CapabilityDeltaKind.INCOMPATIBLE,
                "workspace or state trust root changed",
            )
        )
    for field, reason in (
        ("allowed_environment", "inherited environment capability changed"),
        ("path_prefixes", "executable discovery path changed"),
    ):
        _record_set_change(
            changes,
            f"server.{field}",
            _set(before_server.get(field)),
            _set(after_server.get(field)),
            additions=CapabilityDeltaKind.EXPANSION,
            removals=CapabilityDeltaKind.RESTRICTION,
            reason=reason,
        )
    for field, reason in (
        ("max_file_bytes", "file access bound"),
        ("max_tool_output_chars", "tool output bound"),
        ("default_command_timeout_seconds", "default process duration"),
        ("verification_timeout_seconds", "verification process duration"),
        ("max_fingerprint_bytes", "fingerprint data bound"),
        ("max_batch_files", "batch file access bound"),
    ):
        _record_number(
            changes,
            f"server.{field}",
            before_server.get(field, 0),
            after_server.get(field, 0),
            reason=reason,
        )

    residual_before = _unclassified(before)
    residual_after = _unclassified(after)
    if residual_before != residual_after:
        changes.append(
            CapabilityChange(
                "unclassified",
                residual_before,
                residual_after,
                CapabilityDeltaKind.INCOMPATIBLE,
                "unrecognized configuration semantics changed",
            )
        )

    directions = {change.direction for change in changes}
    if CapabilityDeltaKind.INCOMPATIBLE in directions or (
        CapabilityDeltaKind.EXPANSION in directions
        and CapabilityDeltaKind.RESTRICTION in directions
    ):
        kind = CapabilityDeltaKind.INCOMPATIBLE
    elif CapabilityDeltaKind.EXPANSION in directions:
        kind = CapabilityDeltaKind.EXPANSION
    elif CapabilityDeltaKind.RESTRICTION in directions:
        kind = CapabilityDeltaKind.RESTRICTION
    elif _metadata(before) != _metadata(after):
        kind = CapabilityDeltaKind.METADATA_ONLY
    else:
        # The canonical documents differ only in recognized non-capability presentation fields.
        kind = CapabilityDeltaKind.METADATA_ONLY
    return CapabilityDelta(kind, before_sha, after_sha, tuple(changes))
