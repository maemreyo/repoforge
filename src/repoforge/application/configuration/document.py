"""Deterministic resolved TOML document manipulation for approved repository policies."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import tomli as tomllib

from ...domain.policy_patch import RepositoryPolicyPatch
from ...domain.repository_proposal import RepositoryProposal
from ...domain.user_paths import DEFAULT_STATE_ROOT, DEFAULT_WORKSPACE_ROOT
from .source import SourceTicketGraph

RESOLVED_CONFIG_FORMAT_VERSION = 3

_DEFAULT_SERVER: dict[str, Any] = {
    "workspace_root": DEFAULT_WORKSPACE_ROOT,
    "state_root": DEFAULT_STATE_ROOT,
    "max_file_bytes": 2_000_000,
    "max_tool_output_chars": 120_000,
    "default_command_timeout_seconds": 120,
    "verification_timeout_seconds": 1_800,
    "max_fingerprint_bytes": 50 * 1024 * 1024,
    "max_batch_files": 20,
    "path_prefixes": ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"],
    "allowed_environment": [
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "SSH_AUTH_SOCK",
        "GH_HOST",
        "GIT_SSH_COMMAND",
        "COREPACK_HOME",
        "PNPM_HOME",
    ],
}


def parse_resolved(text: str | None) -> dict[str, Any]:
    if not text:
        return {"server": deepcopy(_DEFAULT_SERVER), "repositories": {}}
    value = tomllib.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Resolved configuration must be a TOML table")
    result = deepcopy(value)
    result.pop("repoforge_lock", None)
    result.setdefault("server", deepcopy(_DEFAULT_SERVER))
    result.setdefault("repositories", {})
    return result


def apply_proposal(document: dict[str, Any], proposal: RepositoryProposal) -> dict[str, Any]:
    result = deepcopy(document)
    repositories = result.setdefault("repositories", {})
    if not isinstance(repositories, dict):
        raise ValueError("repositories must be a table")
    policy = proposal.policy
    existing = repositories.get(proposal.repo_id)
    existing_ticket_graph = (
        deepcopy(existing.get("ticket_graph"))
        if isinstance(existing, dict) and isinstance(existing.get("ticket_graph"), dict)
        else None
    )
    profile_map: dict[str, Any] = {}
    for profile in policy.profiles:
        profile_map[profile.name] = {
            "description": profile.description,
            "verification": profile.verification,
            "commands": [list(command) for command in profile.commands],
            "working_directory": profile.working_directory,
            "timeout_seconds": profile.timeout_seconds,
        }
    default_profile = (
        "full" if "full" in profile_map else (sorted(profile_map)[0] if profile_map else None)
    )
    repo: dict[str, Any] = {
        "path": proposal.path,
        "display_name": proposal.repo_id,
        "remote": policy.remote or "origin",
        "default_base": policy.default_base or "main",
        "allowed_base_branches": list(
            policy.allowed_base_branches or ((policy.default_base or "main"),)
        ),
        "branch_prefix": "ai/",
        "protected_branches": ["main", "master", "develop", "production"],
        "read_only": policy.mode.value == "read_only",
        "publish_enabled": policy.publish_enabled,
        "require_verification_before_commit": bool(profile_map),
        "fetch_before_workspace": policy.publish_enabled and policy.remote is not None,
        "max_changed_files": policy.max_changed_files,
        "max_diff_lines": policy.max_diff_lines,
        "max_total_changed_bytes": policy.max_total_changed_bytes,
        "allowed_paths": list(policy.allowed_paths),
        "denied_paths": list(policy.denied_paths),
        "pr_labels": [],
        "pr_reviewers": [],
        "no_maintainer_edit": False,
        "profiles": profile_map,
    }
    if default_profile:
        repo["default_verification_profile"] = default_profile
    if existing_ticket_graph is not None:
        repo["ticket_graph"] = existing_ticket_graph
    repositories[proposal.repo_id] = repo
    return result


def apply_policy_patch(
    document: dict[str, Any], repo_id: str, patch: RepositoryPolicyPatch
) -> dict[str, Any]:
    """Layer one repository's durable policy patch over its template-derived entry."""

    if patch.is_empty():
        return deepcopy(document)
    result = deepcopy(document)
    repositories = result.get("repositories")
    if not isinstance(repositories, dict) or repo_id not in repositories:
        raise ValueError(f"Unknown repository id: {repo_id}")
    repo = repositories[repo_id]
    if not isinstance(repo, dict):
        raise ValueError(f"repositories.{repo_id} must be a table")
    profiles = repo.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError(f"repositories.{repo_id}.profiles must be a table")
    for profile in patch.profiles:
        profiles[profile.name] = profile.as_table()
    for name in patch.remove_profiles:
        profiles.pop(name, None)
    default_profile = repo.get("default_verification_profile")
    verification_profiles = sorted(
        name
        for name, table in profiles.items()
        if isinstance(table, dict) and table.get("verification") is True
    )
    if default_profile not in profiles or (
        verification_profiles and default_profile not in verification_profiles
    ):
        fallback = (
            "full"
            if "full" in verification_profiles
            else (verification_profiles[0] if verification_profiles else None)
        )
        if fallback is None:
            repo.pop("default_verification_profile", None)
            repo["require_verification_before_commit"] = False
        else:
            repo["default_verification_profile"] = fallback
    diagnostics = repo.setdefault("diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise ValueError(f"repositories.{repo_id}.diagnostics must be a table")
    for name, table in patch.diagnostics:
        diagnostics[name] = deepcopy(table)
    for name in patch.remove_diagnostics:
        diagnostics.pop(name, None)
    formatters = repo.setdefault("formatters", {})
    if not isinstance(formatters, dict):
        raise ValueError(f"repositories.{repo_id}.formatters must be a table")
    for name, table in patch.formatters:
        formatters[name] = deepcopy(table)
    for name in patch.remove_formatters:
        formatters.pop(name, None)
    return result


def apply_ticket_graph(
    document: dict[str, Any], repo_id: str, ticket_graph: SourceTicketGraph | None
) -> dict[str, Any]:
    """Layer human-owned GitHub ticket metadata over one resolved repository entry."""

    result = deepcopy(document)
    repositories = result.get("repositories")
    if not isinstance(repositories, dict) or repo_id not in repositories:
        raise ValueError(f"Unknown repository id: {repo_id}")
    repo = repositories[repo_id]
    if not isinstance(repo, dict):
        raise ValueError(f"repositories.{repo_id} must be a table")
    if ticket_graph is None:
        repo.pop("ticket_graph", None)
    else:
        repo["ticket_graph"] = ticket_graph.as_table()
    return result


def remove_repository(document: dict[str, Any], repo_id: str) -> dict[str, Any]:
    result = deepcopy(document)
    repositories = result.get("repositories")
    if not isinstance(repositories, dict) or repo_id not in repositories:
        raise ValueError(f"Unknown repository id: {repo_id}")
    del repositories[repo_id]
    if not repositories:
        raise ValueError("Cannot remove the final repository")
    return result


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {type(value).__name__}")


def render_resolved(
    document: dict[str, Any],
    *,
    generation: int,
    source_path: str,
    source_sha256: str,
    created_at: str,
    reason: str,
    proposal_id: str | None,
    repository_fingerprints: tuple[tuple[str, str], ...],
) -> str:
    lines = [
        "# Generated by RepoForge. Immutable generation; do not edit.",
        "[repoforge_lock]",
        f"format_version = {RESOLVED_CONFIG_FORMAT_VERSION}",
        f"generation = {generation}",
        f"source_config = {_toml(source_path)}",
        f"source_sha256 = {_toml(source_sha256)}",
        f"created_at = {_toml(created_at)}",
        f"reason = {_toml(reason)}",
    ]
    if proposal_id:
        lines.append(f"proposal_id = {_toml(proposal_id)}")
    lines.extend(["", "[repoforge_lock.repositories]"])
    for repo_id, fingerprint in sorted(repository_fingerprints):
        lines.append(f"{_toml(repo_id)} = {_toml(fingerprint)}")
    server = document.get("server", _DEFAULT_SERVER)
    lines.extend(["", "[server]"])
    if isinstance(server, dict):
        for key in sorted(server):
            if isinstance(server[key], (str, int, bool, list)):
                lines.append(f"{key} = {_toml(server[key])}")
        resource_budget = server.get("resource_budget")
        if isinstance(resource_budget, dict):
            lines.extend(["", "[server.resource_budget]"])
            for key in sorted(resource_budget):
                value = resource_budget[key]
                if isinstance(value, int):
                    lines.append(f"{key} = {_toml(value)}")
    providers = document.get("providers", [])
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            lines.extend(["", "[[providers]]"])
            for key in sorted(k for k in provider if k not in {"filesystem", "output_bounds"}):
                value = provider[key]
                if isinstance(value, (str, int, bool, list)):
                    lines.append(f"{key} = {_toml(value)}")
            for section in ("filesystem", "output_bounds"):
                values = provider.get(section)
                if not isinstance(values, dict):
                    continue
                lines.extend(["", f"[providers.{section}]"])
                for key in sorted(values):
                    value = values[key]
                    if isinstance(value, (str, int, bool, list)):
                        lines.append(f"{key} = {_toml(value)}")
    repositories = document.get("repositories", {})
    if isinstance(repositories, dict):
        for repo_id in sorted(repositories):
            raw = repositories[repo_id]
            if not isinstance(raw, dict):
                continue
            lines.extend(["", f"[repositories.{repo_id}]"])
            for key in sorted(
                k
                for k in raw
                if k
                not in {
                    "profiles",
                    "diagnostics",
                    "formatters",
                    "resource_budget",
                    "ticket_graph",
                }
            ):
                value = raw[key]
                if isinstance(value, (str, int, bool, list)):
                    lines.append(f"{key} = {_toml(value)}")
            resource_budget = raw.get("resource_budget")
            if isinstance(resource_budget, dict):
                lines.extend(["", f"[repositories.{repo_id}.resource_budget]"])
                for key in sorted(resource_budget):
                    value = resource_budget[key]
                    if isinstance(value, int):
                        lines.append(f"{key} = {_toml(value)}")
            ticket_graph = raw.get("ticket_graph")
            if isinstance(ticket_graph, dict):
                lines.extend(["", f"[repositories.{repo_id}.ticket_graph]"])
                for key in sorted(ticket_graph):
                    value = ticket_graph[key]
                    if isinstance(value, (str, int)) and not isinstance(value, bool):
                        lines.append(f"{key} = {_toml(value)}")
            profiles = raw.get("profiles", {})
            if isinstance(profiles, dict):
                for name in sorted(profiles):
                    profile = profiles[name]
                    if not isinstance(profile, dict):
                        continue
                    lines.extend(["", f"[repositories.{repo_id}.profiles.{name}]"])
                    for key in (
                        "description",
                        "verification",
                        "timeout_seconds",
                        "working_directory",
                    ):
                        if key in profile and isinstance(profile[key], (str, int, bool)):
                            lines.append(f"{key} = {_toml(profile[key])}")
                    commands = profile.get("commands")
                    if isinstance(commands, list):
                        lines.append("commands = [")
                        for command in commands:
                            if isinstance(command, list):
                                lines.append(f"  {_toml(command)},")
                        lines.append("]")
            diagnostics = raw.get("diagnostics", {})
            if isinstance(diagnostics, dict):
                for diagnostic_id in sorted(diagnostics):
                    diagnostic = diagnostics[diagnostic_id]
                    if not isinstance(diagnostic, dict):
                        continue
                    lines.extend(["", f"[repositories.{repo_id}.diagnostics.{diagnostic_id}]"])
                    for key in (
                        "summary",
                        "selector_kind",
                        "selector_max_length",
                        "selector_max_values",
                        "selector_expansion",
                        "selector_separator",
                        "selector_prefix",
                        "selector_suffix",
                        "selector_allow_leading_dash",
                        "timeout_seconds",
                        "network_policy",
                        "mutability",
                        "parser",
                        "output_limit",
                        "working_directory",
                    ):
                        if key in diagnostic and isinstance(diagnostic[key], (str, int, bool)):
                            lines.append(f"{key} = {_toml(diagnostic[key])}")
                    for key in (
                        "argv",
                        "selector_values",
                        "selector_char_classes",
                        "artifact_paths",
                    ):
                        value = diagnostic.get(key)
                        if isinstance(value, list):
                            lines.append(f"{key} = {_toml(value)}")
                    selectors = diagnostic.get("selectors")
                    if isinstance(selectors, dict):
                        for extra_name in sorted(selectors):
                            extra = selectors[extra_name]
                            if not isinstance(extra, dict):
                                continue
                            lines.extend(
                                [
                                    "",
                                    f"[repositories.{repo_id}.diagnostics.{diagnostic_id}"
                                    f".selectors.{extra_name}]",
                                ]
                            )
                            for key in (
                                "kind",
                                "max_length",
                                "max_values",
                                "expansion",
                                "separator",
                                "prefix",
                                "suffix",
                                "allow_leading_dash",
                            ):
                                if key in extra and isinstance(extra[key], (str, int, bool)):
                                    lines.append(f"{key} = {_toml(extra[key])}")
                            for key in ("values", "char_classes"):
                                value = extra.get(key)
                                if isinstance(value, list):
                                    lines.append(f"{key} = {_toml(value)}")
            formatters = raw.get("formatters", {})
            if isinstance(formatters, dict):
                for formatter_id in sorted(formatters):
                    formatter = formatters[formatter_id]
                    if not isinstance(formatter, dict):
                        continue
                    lines.extend(["", f"[repositories.{repo_id}.formatters.{formatter_id}]"])
                    for key in (
                        "summary",
                        "timeout_seconds",
                        "output_limit",
                        "max_paths",
                        "baseline_cache_ttl_seconds",
                        "network_policy",
                        "parser",
                    ):
                        if key in formatter and isinstance(formatter[key], (str, int, bool)):
                            lines.append(f"{key} = {_toml(formatter[key])}")
                    for key in ("check_argv", "fix_argv", "include_globs"):
                        value = formatter.get(key)
                        if isinstance(value, list):
                            lines.append(f"{key} = {_toml(value)}")
    return "\n".join(lines).rstrip() + "\n"
