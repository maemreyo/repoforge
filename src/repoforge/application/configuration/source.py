"""Human-owned minimal configuration v2 with deterministic rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli as tomllib

SOURCE_CONFIG_VERSION = 2


@dataclass(frozen=True, slots=True)
class SourceRepository:
    repo_id: str
    path: str
    proposal_id: str | None = None
    policy_template: str = "standard"
    decisions: tuple[tuple[str, str], ...] = ()
    policy_overrides: tuple[tuple[str, str], ...] = ()


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
    result: list[SourceRepository] = []
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
        result.append(
            SourceRepository(
                str(item["id"]),
                str(item["path"]),
                str(item["proposal_id"]) if item.get("proposal_id") else None,
                str(item.get("policy_template", "standard")),
                decisions,
                overrides,
            )
        )
    return SourceConfiguration(tunnel_id, profile, tuple(result))


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
