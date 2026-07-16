"""Human-owned minimal configuration v2 with deterministic rendering."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli as tomllib

from ...domain.policy_patch import PolicyPatchError, RepositoryPolicyPatch

SOURCE_CONFIG_VERSION = 2
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class SourceTicketGraph:
    """Human-owned GitHub-native ticket graph metadata preserved across refreshes."""

    root_issue: int
    repository: str | None = None
    project_owner: str | None = None
    project_number: int | None = None
    project_owner_type: str = "organization"
    status_field: str = "Status"
    priority_field: str = "Priority"
    initiative_field: str = "Initiative"
    type_field: str = "Type"

    @classmethod
    def from_table(cls, raw: object, *, context: str) -> SourceTicketGraph:
        if not isinstance(raw, dict):
            raise ValueError(f"{context} must be a TOML table")
        allowed = {
            "root_issue",
            "repository",
            "project_owner",
            "project_number",
            "project_owner_type",
            "status_field",
            "priority_field",
            "initiative_field",
            "type_field",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"{context} contains unsupported fields: {unknown}")
        root_issue = raw.get("root_issue")
        if not isinstance(root_issue, int) or isinstance(root_issue, bool) or root_issue <= 0:
            raise ValueError(f"{context}.root_issue must be a positive integer")
        repository = raw.get("repository")
        if repository is not None and (
            not isinstance(repository, str) or _GITHUB_REPOSITORY.fullmatch(repository) is None
        ):
            raise ValueError(f"{context}.repository must use owner/name format")
        project_owner = raw.get("project_owner")
        if project_owner is not None and (
            not isinstance(project_owner, str) or not project_owner.strip()
        ):
            raise ValueError(f"{context}.project_owner must be a non-empty string")
        project_number = raw.get("project_number")
        if project_number is not None and (
            not isinstance(project_number, int)
            or isinstance(project_number, bool)
            or project_number <= 0
        ):
            raise ValueError(f"{context}.project_number must be a positive integer")
        owner_type = raw.get("project_owner_type", "organization")
        if owner_type not in {"organization", "user"}:
            raise ValueError(f"{context}.project_owner_type must be 'organization' or 'user'")
        fields: dict[str, str] = {}
        for key, default in (
            ("status_field", "Status"),
            ("priority_field", "Priority"),
            ("initiative_field", "Initiative"),
            ("type_field", "Type"),
        ):
            value = raw.get(key, default)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{context}.{key} must be a non-empty bounded string")
            fields[key] = value.strip()
        if (project_owner is None) != (project_number is None):
            raise ValueError(f"{context} requires project_owner and project_number together")
        return cls(
            root_issue=root_issue,
            repository=repository,
            project_owner=project_owner.strip() if isinstance(project_owner, str) else None,
            project_number=project_number,
            project_owner_type=owner_type,
            **fields,
        )

    def as_table(self) -> dict[str, str | int]:
        result: dict[str, str | int] = {
            "root_issue": self.root_issue,
            "project_owner_type": self.project_owner_type,
            "status_field": self.status_field,
            "priority_field": self.priority_field,
            "initiative_field": self.initiative_field,
            "type_field": self.type_field,
        }
        if self.repository is not None:
            result["repository"] = self.repository
        if self.project_owner is not None:
            result["project_owner"] = self.project_owner
        if self.project_number is not None:
            result["project_number"] = self.project_number
        return result


@dataclass(frozen=True, slots=True)
class SourceRepository:
    repo_id: str
    path: str
    proposal_id: str | None = None
    policy_template: str = "standard"
    decisions: tuple[tuple[str, str], ...] = ()
    policy_overrides: tuple[tuple[str, str], ...] = ()
    policy_patch: RepositoryPolicyPatch = field(default_factory=RepositoryPolicyPatch)
    ticket_graph: SourceTicketGraph | None = None


@dataclass(frozen=True, slots=True)
class SourceConfiguration:
    tunnel_id: str | None
    profile: str
    repositories: tuple[SourceRepository, ...]


def parse_source(text: str) -> SourceConfiguration:
    raw: Any = tomllib.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a TOML table")
    tunnel = raw.get("tunnel")
    if tunnel is None:
        tunnel_id: str | None = None
        profile = "repoforge"
    elif isinstance(tunnel, dict) and isinstance(tunnel.get("id"), str):
        tunnel_id = str(tunnel["id"])
        profile = str(tunnel.get("profile", "repoforge"))
    else:
        raise ValueError("[tunnel].id must be a string when tunnel configuration is present")
    repos = raw.get("repo")
    if not isinstance(repos, list) or not repos:
        raise ValueError("At least one [[repo]] is required")
    metadata = raw.get("repositories", {})
    if not isinstance(metadata, dict):
        raise ValueError("repositories must be a TOML table")
    result: list[SourceRepository] = []
    repo_ids: set[str] = set()
    for item in repos:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("path"), str)
        ):
            raise ValueError("Each [[repo]] requires id and path")
        raw_decisions = item.get("decisions", [])
        raw_overrides = item.get("policy_overrides", [])
        if not isinstance(raw_decisions, list) or not all(
            isinstance(value, str) and "=" in value for value in raw_decisions
        ):
            raise ValueError("repo.decisions must be an array of CODE=CHOICE strings")
        if not isinstance(raw_overrides, list) or not all(
            isinstance(value, str) and "=" in value for value in raw_overrides
        ):
            raise ValueError("repo.policy_overrides must be an array of KEY=VALUE strings")
        decisions = tuple(sorted(tuple(value.split("=", 1)) for value in raw_decisions))
        overrides = tuple(sorted(tuple(value.split("=", 1)) for value in raw_overrides))
        try:
            policy_patch = RepositoryPolicyPatch.from_table(item.get("policy_patch"))
        except PolicyPatchError as exc:
            raise ValueError(f"repo {item['id']} policy_patch is invalid: {exc}") from exc
        repo_id = str(item["id"])
        if repo_id in repo_ids:
            raise ValueError(f"Duplicate repository id: {repo_id}")
        repo_ids.add(repo_id)
        raw_metadata = metadata.get(repo_id, {})
        if not isinstance(raw_metadata, dict):
            raise ValueError(f"repositories.{repo_id} must be a TOML table")
        unsupported_metadata = sorted(set(raw_metadata) - {"ticket_graph"})
        if unsupported_metadata:
            raise ValueError(
                f"repositories.{repo_id} contains unsupported source metadata: "
                f"{unsupported_metadata}"
            )
        ticket_graph = (
            SourceTicketGraph.from_table(
                raw_metadata["ticket_graph"], context=f"repositories.{repo_id}.ticket_graph"
            )
            if "ticket_graph" in raw_metadata
            else None
        )
        result.append(
            SourceRepository(
                repo_id,
                str(item["path"]),
                str(item["proposal_id"]) if item.get("proposal_id") else None,
                str(item.get("policy_template", "standard")),
                decisions,
                overrides,
                policy_patch,
                ticket_graph,
            )
        )
    unknown_metadata = sorted(set(metadata) - repo_ids)
    if unknown_metadata:
        raise ValueError(
            f"repositories contains metadata for unknown repository ids: {unknown_metadata}"
        )
    return SourceConfiguration(tunnel_id, profile, tuple(result))


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        entries = ", ".join(
            f"{_toml_key(str(key))} = {_toml_value(value[key])}" for key in sorted(value)
        )
        return "{ " + entries + " }"
    raise ValueError(f"Unsupported TOML value in policy patch: {type(value).__name__}")


_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _render_patch_table(prefix: str, table: dict[str, Any], lines: list[str]) -> None:
    scalar_keys = [key for key in sorted(table) if not isinstance(table[key], dict)]
    nested_keys = [key for key in sorted(table) if isinstance(table[key], dict)]
    if scalar_keys or not nested_keys:
        lines.extend(["", f"[{prefix}]"])
        for key in scalar_keys:
            lines.append(f"{_toml_key(key)} = {_toml_value(table[key])}")
    for key in nested_keys:
        _render_patch_table(f"{prefix}.{_toml_key(key)}", table[key], lines)


def render_source(config: SourceConfiguration) -> str:
    lines = [
        "# RepoForge user configuration. Approved policy is stored in immutable generations.",
        f"version = {SOURCE_CONFIG_VERSION}",
    ]
    if config.tunnel_id is not None:
        lines.extend(
            [
                "",
                "[tunnel]",
                f"id = {json.dumps(config.tunnel_id)}",
                f"profile = {json.dumps(config.profile)}",
            ]
        )
    for repo in config.repositories:
        lines.extend(
            [
                "",
                "[[repo]]",
                f"id = {json.dumps(repo.repo_id)}",
                f"path = {json.dumps(repo.path)}",
            ]
        )
        if repo.policy_template != "standard":
            lines.append(f"policy_template = {json.dumps(repo.policy_template)}")
        if repo.decisions:
            lines.append(
                "decisions = ["
                + ", ".join(json.dumps(f"{key}={value}") for key, value in repo.decisions)
                + "]"
            )
        if repo.policy_overrides:
            lines.append(
                "policy_overrides = ["
                + ", ".join(json.dumps(f"{key}={value}") for key, value in repo.policy_overrides)
                + "]"
            )
        if repo.proposal_id:
            lines.append(f"proposal_id = {json.dumps(repo.proposal_id)}")
        if not repo.policy_patch.is_empty():
            _render_patch_table("repo.policy_patch", repo.policy_patch.as_table(), lines)
    for repo in config.repositories:
        if repo.ticket_graph is None:
            continue
        lines.extend(["", f"[repositories.{_toml_key(repo.repo_id)}.ticket_graph]"])
        for key, value in repo.ticket_graph.as_table().items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    return "\n".join(lines).rstrip() + "\n"


def add_source_repository(
    config: SourceConfiguration, repository: SourceRepository
) -> SourceConfiguration:
    if any(item.repo_id == repository.repo_id for item in config.repositories):
        raise ValueError(f"Repository id already exists: {repository.repo_id}")
    if any(
        Path(item.path).expanduser().resolve() == Path(repository.path).expanduser().resolve()
        for item in config.repositories
    ):
        raise ValueError(f"Repository path already exists: {repository.path}")
    return SourceConfiguration(
        config.tunnel_id,
        config.profile,
        tuple(sorted((*config.repositories, repository), key=lambda item: item.repo_id)),
    )


def remove_source_repository(config: SourceConfiguration, repo_id: str) -> SourceConfiguration:
    remaining = tuple(item for item in config.repositories if item.repo_id != repo_id)
    if len(remaining) == len(config.repositories):
        raise ValueError(f"Unknown repository id: {repo_id}")
    if not remaining:
        raise ValueError("Cannot remove the final repository")
    return SourceConfiguration(config.tunnel_id, config.profile, remaining)
