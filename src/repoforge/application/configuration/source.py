"""Human-owned minimal configuration v2 with deterministic rendering."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli as tomllib

from ...domain.generated_paths import GeneratedPathRule, parse_generated_paths
from ...domain.issue_writes import IssueWritePolicy, IssueWritePolicyError
from ...domain.policy_patch import PolicyPatchError, RepositoryPolicyPatch

SOURCE_CONFIG_VERSION = 2
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_AUTH_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_GITHUB_HOST = re.compile(r"^[a-z0-9.-]+$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_SHAPED_REFERENCE = re.compile(
    r"^(?:gh[pousr]_|github_pat_|sk-|xox[baprs]-)|(?:token|secret|password)=",
    re.IGNORECASE,
)


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
class SourceRiskPolicy:
    """Human-owned risk-policy fields preserved from source into resolved generations."""

    low_max: int | None = None
    medium_max: int | None = None
    high_max: int | None = None
    critical_globs: tuple[str, ...] | None = None
    public_contract_globs: tuple[str, ...] | None = None
    manifest_globs: tuple[str, ...] | None = None
    docs_globs: tuple[str, ...] | None = None
    narrow_diagnostics: tuple[str, ...] | None = None
    ordered_profiles: tuple[str, ...] | None = None
    final_profile: str | None = None

    @classmethod
    def from_table(cls, raw: object, *, context: str) -> SourceRiskPolicy:
        if not isinstance(raw, dict):
            raise ValueError(f"{context} must be a TOML table")
        allowed = {
            "low_max",
            "medium_max",
            "high_max",
            "critical_globs",
            "public_contract_globs",
            "manifest_globs",
            "docs_globs",
            "narrow_diagnostics",
            "ordered_profiles",
            "final_profile",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"{context} contains unsupported fields: {unknown}")

        def threshold(name: str) -> int | None:
            value = raw.get(name)
            if value is None:
                return None
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
                raise ValueError(f"{context}.{name} must be an integer in 0..100")
            return value

        def strings(name: str) -> tuple[str, ...] | None:
            value = raw.get(name)
            if value is None:
                return None
            if (
                not isinstance(value, list)
                or len(value) > 64
                or not all(isinstance(item, str) and item and len(item) <= 512 for item in value)
            ):
                raise ValueError(
                    f"{context}.{name} must be an array of at most 64 non-empty bounded strings"
                )
            return tuple(value)

        final_profile = raw.get("final_profile")
        if final_profile is not None and (
            not isinstance(final_profile, str) or not final_profile or len(final_profile) > 64
        ):
            raise ValueError(f"{context}.final_profile must be a non-empty bounded string")
        result = cls(
            low_max=threshold("low_max"),
            medium_max=threshold("medium_max"),
            high_max=threshold("high_max"),
            critical_globs=strings("critical_globs"),
            public_contract_globs=strings("public_contract_globs"),
            manifest_globs=strings("manifest_globs"),
            docs_globs=strings("docs_globs"),
            narrow_diagnostics=strings("narrow_diagnostics"),
            ordered_profiles=strings("ordered_profiles"),
            final_profile=final_profile,
        )
        if (
            result.low_max is not None
            and result.medium_max is not None
            and result.high_max is not None
            and not result.low_max < result.medium_max < result.high_max
        ):
            raise ValueError(f"{context} thresholds must be strictly increasing")
        return result

    def as_table(self) -> dict[str, int | str | list[str]]:
        result: dict[str, int | str | list[str]] = {}
        for name, value in (
            ("low_max", self.low_max),
            ("medium_max", self.medium_max),
            ("high_max", self.high_max),
        ):
            if value is not None:
                result[name] = value
        if self.final_profile is not None:
            result["final_profile"] = self.final_profile
        for name in (
            "critical_globs",
            "public_contract_globs",
            "manifest_globs",
            "docs_globs",
            "narrow_diagnostics",
            "ordered_profiles",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = list(value)
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
    risk_policy: SourceRiskPolicy | None = None
    generated_paths: tuple[GeneratedPathRule, ...] = ()
    issue_writes: IssueWritePolicy = field(default_factory=IssueWritePolicy)


@dataclass(frozen=True, slots=True)
class SourceAuthProfile:
    """Secret-free, human-owned declaration of one repository auth profile."""

    profile_id: str
    provider: str
    credential_kind: str
    credential_reference: str
    actor_class: str
    expected_actor_id: str
    enabled: bool
    repository_id: str
    repository_patterns: tuple[str, ...]
    boundary_id: str
    capability_ids: tuple[str, ...]
    github_host: str
    transport_kind: str
    credential_fingerprint: str
    allowed_access: tuple[str, ...]
    github_login: str | None = None
    github_app_id: str | None = None
    github_installation_id: str | None = None
    github_permissions: tuple[str, ...] = ()
    ssh_identity_file: str | None = None
    https_token_environment: str | None = None
    source_ssh_alias: str | None = None
    lease_seconds: int = 300

    @classmethod
    def from_table(cls, profile_id: str, raw: object, *, context: str) -> SourceAuthProfile:
        if _AUTH_PROFILE_ID.fullmatch(profile_id) is None:
            raise ValueError(f"{context} has an invalid profile id")
        if not isinstance(raw, dict):
            raise ValueError(f"{context} must be a TOML table")
        allowed = {
            "provider",
            "credential_kind",
            "credential_reference",
            "actor_class",
            "expected_actor_id",
            "enabled",
            "repository_id",
            "repository_patterns",
            "boundary_id",
            "capability_ids",
            "github_host",
            "github_login",
            "github_app_id",
            "github_installation_id",
            "github_permissions",
            "transport_kind",
            "credential_fingerprint",
            "allowed_access",
            "ssh_identity_file",
            "https_token_environment",
            "source_ssh_alias",
            "lease_seconds",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"{context} contains unsupported fields: {unknown}")

        def required_string(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"{context}.{name} must be a non-empty bounded string")
            return value

        def optional_string(name: str) -> str | None:
            value = raw.get(name)
            if value is None:
                return None
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"{context}.{name} must be a non-empty bounded string")
            return value

        def strings(name: str, *, required: bool) -> tuple[str, ...]:
            value = raw.get(name)
            if value is None and not required:
                return ()
            if (
                not isinstance(value, list)
                or (required and not value)
                or len(value) > 64
                or not all(isinstance(item, str) and item and len(item) <= 512 for item in value)
                or len(set(value)) != len(value)
            ):
                raise ValueError(
                    f"{context}.{name} must be a unique array of bounded non-empty strings"
                )
            return tuple(value)

        provider = required_string("provider")
        if provider != "github":
            raise ValueError(f"{context}.provider must be 'github'")
        credential_kind = required_string("credential_kind")
        if credential_kind not in {"stored_account", "github_app"}:
            raise ValueError(f"{context}.credential_kind must be 'stored_account' or 'github_app'")
        credential_reference = required_string("credential_reference")
        if (
            _AUTH_PROFILE_ID.fullmatch(credential_reference) is None
            or _SECRET_SHAPED_REFERENCE.search(credential_reference) is not None
        ):
            raise ValueError(f"{context}.credential_reference must be a safe opaque identifier")
        actor_class = required_string("actor_class")
        if actor_class not in {"human_operated", "delegated_human", "autonomous_agent"}:
            raise ValueError(f"{context}.actor_class is unsupported")
        expected_actor_id = required_string("expected_actor_id")
        repository_id = required_string("repository_id")
        boundary_id = required_string("boundary_id")
        repository_patterns = strings("repository_patterns", required=True)
        capability_ids = strings("capability_ids", required=True)
        github_host = required_string("github_host")
        if _GITHUB_HOST.fullmatch(github_host) is None:
            raise ValueError(f"{context}.github_host must be a lowercase host name")
        transport_kind = required_string("transport_kind")
        if transport_kind not in {"ssh", "https"}:
            raise ValueError(f"{context}.transport_kind must be 'ssh' or 'https'")
        credential_fingerprint = required_string("credential_fingerprint")
        if _SHA256.fullmatch(credential_fingerprint) is None:
            raise ValueError(f"{context}.credential_fingerprint must be a lowercase SHA-256")
        allowed_access = strings("allowed_access", required=True)
        if not set(allowed_access) <= {"read", "write"}:
            raise ValueError(f"{context}.allowed_access contains an unsupported access mode")
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"{context}.enabled must be a boolean")
        lease_seconds = raw.get("lease_seconds", 300)
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 30 <= lease_seconds <= 3600
        ):
            raise ValueError(f"{context}.lease_seconds must be an integer in 30..3600")

        github_login = optional_string("github_login")
        github_app_id = optional_string("github_app_id")
        github_installation_id = optional_string("github_installation_id")
        github_permissions = strings("github_permissions", required=False)
        if credential_kind == "stored_account":
            if actor_class not in {"human_operated", "delegated_human"} or github_login is None:
                raise ValueError(
                    f"{context} stored-account profiles require a human actor and github_login"
                )
            if (
                github_app_id is not None
                or github_installation_id is not None
                or github_permissions
            ):
                raise ValueError(
                    f"{context} stored-account profiles cannot declare GitHub App fields"
                )
        else:
            if (
                actor_class != "autonomous_agent"
                or github_app_id is None
                or github_installation_id is None
                or not github_permissions
            ):
                raise ValueError(
                    f"{context} GitHub App profiles require an autonomous actor and app fields"
                )
            if github_login is not None:
                raise ValueError(f"{context} GitHub App profiles cannot declare github_login")
        for permission in github_permissions:
            name, separator, level = permission.partition(":")
            if not separator or not name or level not in {"read", "write", "admin"}:
                raise ValueError(f"{context}.github_permissions contains an invalid permission")

        ssh_identity_file = optional_string("ssh_identity_file")
        https_token_environment = optional_string("https_token_environment")
        source_ssh_alias = optional_string("source_ssh_alias")
        if transport_kind == "ssh":
            if ssh_identity_file is None or not Path(ssh_identity_file).is_absolute():
                raise ValueError(
                    f"{context}.ssh_identity_file must be an absolute identity-file path"
                )
            if https_token_environment is not None:
                raise ValueError(
                    f"{context} SSH transport cannot declare an HTTPS token environment"
                )
        else:
            if (
                https_token_environment is None
                or _ENVIRONMENT_NAME.fullmatch(https_token_environment) is None
            ):
                raise ValueError(
                    f"{context}.https_token_environment must be an uppercase environment name"
                )
            if ssh_identity_file is not None:
                raise ValueError(f"{context} HTTPS transport cannot declare an SSH identity file")

        return cls(
            profile_id=profile_id,
            provider=provider,
            credential_kind=credential_kind,
            credential_reference=credential_reference,
            actor_class=actor_class,
            expected_actor_id=expected_actor_id,
            enabled=enabled,
            repository_id=repository_id,
            repository_patterns=repository_patterns,
            boundary_id=boundary_id,
            capability_ids=capability_ids,
            github_host=github_host,
            transport_kind=transport_kind,
            credential_fingerprint=credential_fingerprint,
            allowed_access=allowed_access,
            github_login=github_login,
            github_app_id=github_app_id,
            github_installation_id=github_installation_id,
            github_permissions=github_permissions,
            ssh_identity_file=ssh_identity_file,
            https_token_environment=https_token_environment,
            source_ssh_alias=source_ssh_alias,
            lease_seconds=lease_seconds,
        )

    def as_table(self) -> dict[str, bool | int | str | list[str]]:
        result: dict[str, bool | int | str | list[str]] = {
            "provider": self.provider,
            "credential_kind": self.credential_kind,
            "credential_reference": self.credential_reference,
            "actor_class": self.actor_class,
            "expected_actor_id": self.expected_actor_id,
            "enabled": self.enabled,
            "repository_id": self.repository_id,
            "repository_patterns": list(self.repository_patterns),
            "boundary_id": self.boundary_id,
            "capability_ids": list(self.capability_ids),
            "github_host": self.github_host,
            "transport_kind": self.transport_kind,
            "credential_fingerprint": self.credential_fingerprint,
            "allowed_access": list(self.allowed_access),
            "lease_seconds": self.lease_seconds,
        }
        for name in (
            "github_login",
            "github_app_id",
            "github_installation_id",
            "ssh_identity_file",
            "https_token_environment",
            "source_ssh_alias",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.github_permissions:
            result["github_permissions"] = list(self.github_permissions)
        return result


@dataclass(frozen=True, slots=True)
class SourceConfiguration:
    tunnel_id: str | None
    profile: str
    repositories: tuple[SourceRepository, ...]
    #: Lifetime tunnel-client gives one MCP transport connection, in seconds. Lives beside
    #: the other connection parameters rather than under `[server]`, because `[server]` is
    #: read once at setup and then frozen -- deliberately, since it carries capability
    #: grants like `allowed_environment` -- while these are re-read from source on every
    #: runtime start. `None` passes nothing and leaves the tunnel's own default in force.
    mcp_connection_max_ttl_seconds: int | None = None
    auth_profiles: tuple[SourceAuthProfile, ...] = ()


def parse_source(text: str) -> SourceConfiguration:
    raw: Any = tomllib.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a TOML table")
    unknown_root = sorted(set(raw) - {"version", "tunnel", "repo", "repositories", "auth_profiles"})
    if unknown_root:
        raise ValueError(f"Configuration contains unsupported fields: {unknown_root}")
    raw_auth_profiles = raw.get("auth_profiles", {})
    if not isinstance(raw_auth_profiles, dict):
        raise ValueError("auth_profiles must be a TOML table")
    auth_profiles = tuple(
        SourceAuthProfile.from_table(str(profile_id), table, context=f"auth_profiles.{profile_id}")
        for profile_id, table in sorted(raw_auth_profiles.items())
    )
    tunnel = raw.get("tunnel")
    mcp_connection_max_ttl_seconds: int | None = None
    if tunnel is None:
        tunnel_id: str | None = None
        profile = "repoforge"
    elif isinstance(tunnel, dict) and isinstance(tunnel.get("id"), str):
        tunnel_id = str(tunnel["id"])
        profile = str(tunnel.get("profile", "repoforge"))
        raw_ttl = tunnel.get("mcp_connection_max_ttl_seconds")
        if raw_ttl is not None:
            if not isinstance(raw_ttl, int) or isinstance(raw_ttl, bool) or raw_ttl <= 0:
                raise ValueError(
                    "[tunnel].mcp_connection_max_ttl_seconds must be a positive integer"
                )
            mcp_connection_max_ttl_seconds = raw_ttl
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
        unsupported_metadata = sorted(
            set(raw_metadata) - {"ticket_graph", "risk", "generated_paths", "issue_writes"}
        )
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
        risk_policy = (
            SourceRiskPolicy.from_table(
                raw_metadata["risk"], context=f"repositories.{repo_id}.risk"
            )
            if "risk" in raw_metadata
            else None
        )
        try:
            generated_paths = parse_generated_paths(
                raw_metadata.get("generated_paths"),
                context=f"repositories.{repo_id}.generated_paths",
            )
            issue_writes = IssueWritePolicy.from_table(
                raw_metadata.get("issue_writes"),
                context=f"repositories.{repo_id}.issue_writes",
            )
        except (ValueError, IssueWritePolicyError) as exc:
            raise ValueError(str(exc)) from exc
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
                risk_policy,
                generated_paths,
                issue_writes,
            )
        )
    unknown_metadata = sorted(set(metadata) - repo_ids)
    if unknown_metadata:
        raise ValueError(
            f"repositories contains metadata for unknown repository ids: {unknown_metadata}"
        )
    return SourceConfiguration(
        tunnel_id,
        profile,
        tuple(result),
        mcp_connection_max_ttl_seconds,
        auth_profiles,
    )


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
        if config.mcp_connection_max_ttl_seconds is not None:
            lines.append(
                "mcp_connection_max_ttl_seconds = " + str(config.mcp_connection_max_ttl_seconds)
            )
    for auth_profile in sorted(config.auth_profiles, key=lambda item: item.profile_id):
        lines.extend(["", f"[auth_profiles.{_toml_key(auth_profile.profile_id)}]"])
        for key, value in auth_profile.as_table().items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
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
        if repo.generated_paths or repo.issue_writes != IssueWritePolicy():
            lines.extend(["", f"[repositories.{_toml_key(repo.repo_id)}]"])
            if repo.generated_paths:
                lines.append(
                    "generated_paths = "
                    + _toml_value([rule.as_table() for rule in repo.generated_paths])
                )
            if repo.issue_writes != IssueWritePolicy():
                lines.append("issue_writes = " + _toml_value(repo.issue_writes.as_table()))
        if repo.ticket_graph is not None:
            lines.extend(["", f"[repositories.{_toml_key(repo.repo_id)}.ticket_graph]"])
            for key, value in repo.ticket_graph.as_table().items():
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        if repo.risk_policy is not None:
            lines.extend(["", f"[repositories.{_toml_key(repo.repo_id)}.risk]"])
            for key, risk_value in repo.risk_policy.as_table().items():
                lines.append(f"{_toml_key(key)} = {_toml_value(risk_value)}")
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
        config.mcp_connection_max_ttl_seconds,
        config.auth_profiles,
    )


def remove_source_repository(config: SourceConfiguration, repo_id: str) -> SourceConfiguration:
    remaining = tuple(item for item in config.repositories if item.repo_id != repo_id)
    if len(remaining) == len(config.repositories):
        raise ValueError(f"Unknown repository id: {repo_id}")
    if not remaining:
        raise ValueError("Cannot remove the final repository")
    return SourceConfiguration(
        config.tunnel_id,
        config.profile,
        remaining,
        config.mcp_connection_max_ttl_seconds,
        config.auth_profiles,
    )
