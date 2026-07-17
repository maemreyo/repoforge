"""TOML configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import tomli as tomllib

from .domain.adhoc import ExecutionMode, validate_adhoc_runners
from .domain.command_source import (
    derive_command_source_paths,
    validate_command_source_paths,
)
from .domain.diagnostics import (
    DiagnosticMutability,
    DiagnosticNetworkPolicy,
    DiagnosticParserKind,
    DiagnosticProfileConfig,
    DiagnosticSelectorConfig,
    DiagnosticSelectorKind,
    validate_diagnostic_profile,
)
from .domain.errors import ConfigError
from .domain.hygiene import (
    FormatterPolicy,
    HygieneNetworkPolicy,
    HygieneParserKind,
)
from .domain.mutation_policy import MUTATION_OPS, validate_allowed_mutation_ops
from .domain.provider_config import load_provider_manifests
from .domain.provider_manifest import ProviderManifest
from .domain.resource_budget import (
    DEFAULT_RESOURCE_BUDGET,
    RESOURCE_BUDGET_FIELDS,
    ResourceBudget,
)
from .domain.risk import RiskPolicy, default_risk_policy
from .domain.user_paths import (
    DEFAULT_CONFIG_PATH as DEFAULT_CONFIG_PATH,
)
from .domain.user_paths import (
    DEFAULT_STATE_ROOT as DEFAULT_STATE_ROOT,
)
from .domain.user_paths import (
    DEFAULT_WORKSPACE_ROOT as DEFAULT_WORKSPACE_ROOT,
)
from .domain.verification_steps import (
    HygieneBaselinePolicy,
    VerificationStep,
    VerificationStepKind,
    compile_legacy_steps,
)

DEFAULT_PATH_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
DEFAULT_ALLOWED_ENVIRONMENT = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "SSH_AUTH_SOCK",
    "GH_HOST",
    "GIT_SSH_COMMAND",
    "COREPACK_HOME",
    "PNPM_HOME",
)
DEFAULT_DENIED_PATHS = (
    ".git",
    ".git/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*secret*",
    "**/*credential*",
    ".github/workflows/**",
)
DEFAULT_PROTECTED_BRANCHES = ("main", "master", "develop", "production")
_POLICY_PRESETS: dict[str, dict[str, bool | int]] = {
    "strict": {
        "read_only": True,
        "publish_enabled": False,
        "require_verification_before_commit": True,
        "fetch_before_workspace": False,
        "max_changed_files": 25,
        "max_diff_lines": 2_000,
        "max_total_changed_bytes": 5 * 1024 * 1024,
    },
    "standard": {
        "read_only": False,
        "publish_enabled": False,
        "require_verification_before_commit": True,
        "fetch_before_workspace": False,
        "max_changed_files": 75,
        "max_diff_lines": 6_000,
        "max_total_changed_bytes": 10 * 1024 * 1024,
    },
    "relaxed": {
        "read_only": False,
        "publish_enabled": True,
        "require_verification_before_commit": True,
        "fetch_before_workspace": True,
        "max_changed_files": 150,
        "max_diff_lines": 12_000,
        "max_total_changed_bytes": 25 * 1024 * 1024,
    },
}
_SAFE_BRANCH_COMPONENT = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_REPO_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
EnumValue = TypeVar("EnumValue", bound=Enum)


def policy_preset_reference() -> tuple[tuple[str, bool, bool, int, int, int], ...]:
    """Return the stable, reviewable values expanded from named repository presets."""
    return tuple(
        (
            name,
            bool(values["read_only"]),
            bool(values["publish_enabled"]),
            int(values["max_changed_files"]),
            int(values["max_diff_lines"]),
            int(values["max_total_changed_bytes"]),
        )
        for name, values in _POLICY_PRESETS.items()
    )


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    description: str
    commands: tuple[tuple[str, ...], ...]
    verification: bool = False
    timeout_seconds: int | None = None
    working_directory: str | None = None
    command_source_paths: tuple[str, ...] = ()
    steps: tuple[VerificationStep, ...] = ()
    baseline_policy: HygieneBaselinePolicy = HygieneBaselinePolicy.STRICT_CLEAN


@dataclass(frozen=True)
class GitHubTicketGraphConfig:
    """Reviewed GitHub-native source for one repository's operational ticket graph."""

    root_issue: int
    repository: str | None = None
    project_owner: str | None = None
    project_number: int | None = None
    project_owner_type: str = "organization"
    status_field: str = "Status"
    priority_field: str = "Priority"
    initiative_field: str = "Initiative"
    type_field: str = "Type"


@dataclass(frozen=True)
class RepositoryConfig:
    repo_id: str
    path: Path
    display_name: str = ""
    remote: str = "origin"
    default_base: str = "main"
    allowed_base_branches: tuple[str, ...] = ("main",)
    branch_prefix: str = "ai/"
    protected_branches: tuple[str, ...] = ("main", "master", "develop", "production")
    read_only: bool = False
    publish_enabled: bool = True
    require_verification_before_commit: bool = True
    fetch_before_workspace: bool = True
    default_verification_profile: str | None = None
    max_changed_files: int = 150
    max_diff_lines: int = 12_000
    max_total_changed_bytes: int = 25 * 1024 * 1024
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = DEFAULT_DENIED_PATHS
    allowed_mutation_ops: tuple[str, ...] = MUTATION_OPS
    pr_labels: tuple[str, ...] = ()
    pr_reviewers: tuple[str, ...] = ()
    no_maintainer_edit: bool = False
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)
    diagnostics: dict[str, DiagnosticProfileConfig] = field(default_factory=dict)
    formatters: dict[str, FormatterPolicy] = field(default_factory=dict)
    risk_policy: RiskPolicy = field(
        default_factory=lambda: default_risk_policy(final_profile="full")
    )
    resource_budget: ResourceBudget = DEFAULT_RESOURCE_BUDGET
    execution_mode: ExecutionMode = ExecutionMode.STRICT
    adhoc_runners: tuple[str, ...] = ()
    adhoc_timeout_seconds: int = 300
    ticket_graph: GitHubTicketGraphConfig | None = None


@dataclass(frozen=True)
class ServerConfig:
    workspace_root: Path
    state_root: Path
    max_file_bytes: int = 2_000_000
    max_tool_output_chars: int = 120_000
    default_command_timeout_seconds: int = 120
    verification_timeout_seconds: int = 1_800
    max_fingerprint_bytes: int = 50 * 1024 * 1024
    max_batch_files: int = 20
    audit_max_bytes: int = 5_000_000
    audit_backup_count: int = 3
    runtime_log_max_bytes: int = 5_000_000
    runtime_log_backup_count: int = 3
    idempotency_stale_seconds: int = 900
    idempotency_lock_timeout_seconds: int = 2
    max_background_profiles: int = 2
    fast_fail_threshold_seconds: float = 10.0
    stale_workspace_candidate_threshold: int = 3
    stale_workspace_min_age_seconds: float = 3_600.0
    path_prefixes: tuple[str, ...] = DEFAULT_PATH_PREFIXES
    allowed_environment: tuple[str, ...] = DEFAULT_ALLOWED_ENVIRONMENT
    resource_budget: ResourceBudget = DEFAULT_RESOURCE_BUDGET
    github_read_cache_ttl_seconds: int = 120
    github_webhook_enabled: bool = False
    github_webhook_bind: str = "127.0.0.1"
    github_webhook_port: int = 8766
    github_webhook_secret_env: str = "REPOFORGE_GITHUB_WEBHOOK_SECRET"
    github_webhook_max_body_bytes: int = 1_000_000


@dataclass(frozen=True)
class AppConfig:
    source_path: Path
    server: ServerConfig
    repositories: dict[str, RepositoryConfig]
    providers: tuple[ProviderManifest, ...] = ()


def _expand_path(value: str, *, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _expect_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a TOML table")
    return value


def _tuple_of_strings(value: Any, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{context} must be an array of strings")
    return tuple(value)


def _positive_int(value: Any, default: int, context: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{context} must be a positive integer")
    return value


def _bounded_int(value: Any, default: int, minimum: int, maximum: int, context: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigError(f"{context} must be an integer between {minimum} and {maximum}")
    return value


def _bounded_float(
    value: Any, default: float, minimum: float, maximum: float, context: str
) -> float:
    if value is None:
        return default
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not minimum <= float(value) <= maximum
    ):
        raise ConfigError(f"{context} must be a number between {minimum} and {maximum}")
    return float(value)


def _load_resource_budget(
    raw: Any,
    context: str,
    defaults: ResourceBudget = DEFAULT_RESOURCE_BUDGET,
    ceiling: ResourceBudget | None = None,
) -> ResourceBudget:
    if raw is None:
        return defaults
    table = _expect_mapping(raw, context)
    unknown = sorted(set(table) - set(RESOURCE_BUDGET_FIELDS))
    if unknown:
        raise ConfigError(f"{context} contains unsupported budget fields: {unknown}")
    values: dict[str, int] = {}
    for field_name in RESOURCE_BUDGET_FIELDS:
        default = getattr(defaults, field_name)
        configured = _positive_int(table.get(field_name), default, f"{context}.{field_name}")
        if ceiling is not None and configured > getattr(ceiling, field_name):
            raise ConfigError(f"{context}.{field_name} cannot exceed its inherited server limit")
        values[field_name] = configured
    return ResourceBudget(**values)


def _boolean(value: Any, default: bool, context: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be true or false")
    return value


def _webhook_bind(value: Any) -> str:
    bind = str(value or "127.0.0.1").strip()
    if bind not in {"127.0.0.1", "::1", "localhost"}:
        raise ConfigError("server.github_webhook_bind must be a loopback address")
    return bind


def _environment_name(value: Any, context: str) -> str:
    name = str(value)
    if not _SAFE_ENVIRONMENT_NAME.fullmatch(name):
        raise ConfigError(f"{context} must be an uppercase environment variable name")
    return name


def _safe_ref(value: str, context: str) -> str:
    if not value or not _SAFE_BRANCH_COMPONENT.fullmatch(value):
        raise ConfigError(f"{context} contains unsafe characters: {value!r}")
    if value.startswith("-") or ".." in value or value.endswith("/"):
        raise ConfigError(f"{context} is not a safe Git ref component: {value!r}")
    return value


def _safe_remote(value: str, context: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or value.startswith("-"):
        raise ConfigError(f"{context} must be a safe configured remote name: {value!r}")
    return value


def _resolve_repository_preset(raw: dict[str, Any], repo_id: str) -> dict[str, Any]:
    policy = raw.get("policy")
    if policy is None:
        preset_fields = _POLICY_PRESETS["strict"].keys()
        return {**_POLICY_PRESETS["strict"], **raw} if preset_fields.isdisjoint(raw) else raw
    if not isinstance(policy, str) or policy not in _POLICY_PRESETS:
        allowed = ", ".join(("strict", "standard", "relaxed"))
        raise ConfigError(f"repositories.{repo_id}.policy must be one of: {allowed}")
    return {**_POLICY_PRESETS[policy], **raw}


def _load_profiles(raw: Any, repo_id: str) -> dict[str, ProfileConfig]:
    if raw is None:
        return {}
    table = _expect_mapping(raw, f"repositories.{repo_id}.profiles")
    profiles: dict[str, ProfileConfig] = {}
    for name, profile_raw in table.items():
        _safe_ref(name, f"profile name for {repo_id}")
        profile = _expect_mapping(profile_raw, f"repositories.{repo_id}.profiles.{name}")
        description = str(profile.get("description", ""))
        verification = _boolean(
            profile.get("verification"), False, f"profile {repo_id}.{name}.verification"
        )
        working_directory_raw = profile.get("working_directory")
        working_directory = None
        if working_directory_raw is not None:
            if not isinstance(working_directory_raw, str):
                raise ConfigError(f"profile {repo_id}.{name}.working_directory must be a string")
            normalized_workdir = working_directory_raw.replace("\\", "/").strip("/")
            if (
                not normalized_workdir
                or normalized_workdir.startswith("-")
                or any(part in {"", ".", ".."} for part in normalized_workdir.split("/"))
            ):
                raise ConfigError(
                    f"profile {repo_id}.{name}.working_directory must be a safe relative path"
                )
            working_directory = normalized_workdir
        timeout_raw = profile.get("timeout_seconds")
        timeout_seconds = None
        if timeout_raw is not None:
            timeout_seconds = _positive_int(
                timeout_raw, 1, f"profile {repo_id}.{name}.timeout_seconds"
            )
        raw_commands = profile.get("commands")
        commands: list[tuple[str, ...]] = []
        if raw_commands is not None:
            if not isinstance(raw_commands, list) or not raw_commands:
                raise ConfigError(
                    f"profile {repo_id}.{name}.commands must be a non-empty command array"
                )
            for index, command in enumerate(raw_commands):
                if (
                    not isinstance(command, list)
                    or not command
                    or not all(isinstance(arg, str) and arg for arg in command)
                ):
                    raise ConfigError(
                        f"profile {repo_id}.{name}.commands[{index}] must be a non-empty string array"
                    )
                commands.append(tuple(command))

        raw_steps = profile.get("steps")
        steps: list[VerificationStep] = []
        if raw_steps is not None:
            if not isinstance(raw_steps, list) or not raw_steps:
                raise ConfigError(f"profile {repo_id}.{name}.steps must be a non-empty table array")
            step_ids: set[str] = set()
            for index, raw_step in enumerate(raw_steps):
                context = f"profile {repo_id}.{name}.steps[{index}]"
                step = _expect_mapping(raw_step, context)
                unknown = sorted(set(step) - {"id", "kind", "command"})
                if unknown:
                    raise ConfigError(f"{context} has unsupported fields: {unknown}")
                step_id = step.get("id")
                if not isinstance(step_id, str):
                    raise ConfigError(f"{context}.id must be a string")
                if step_id in step_ids:
                    raise ConfigError(
                        f"profile {repo_id}.{name}.steps contains duplicate id {step_id!r}"
                    )
                step_ids.add(step_id)
                kind_raw = step.get("kind")
                try:
                    kind = VerificationStepKind(kind_raw)
                except (TypeError, ValueError) as exc:
                    raise ConfigError(
                        f"{context}.kind must be one of "
                        f"{[item.value for item in VerificationStepKind]}"
                    ) from exc
                command_raw = step.get("command")
                if (
                    not isinstance(command_raw, list)
                    or not command_raw
                    or not all(isinstance(arg, str) and arg for arg in command_raw)
                ):
                    raise ConfigError(f"{context}.command must be a non-empty string array")
                try:
                    steps.append(VerificationStep(step_id, kind, tuple(command_raw)))
                except ValueError as exc:
                    raise ConfigError(f"Invalid {context}: {exc}") from exc

        if not commands and not steps:
            raise ConfigError(f"profile {repo_id}.{name} must contain commands or steps")
        if steps:
            step_commands = [step.command for step in steps]
            if commands and commands != step_commands:
                raise ConfigError(f"profile {repo_id}.{name}.commands must match steps commands")
            commands = step_commands
        else:
            steps = list(compile_legacy_steps(tuple(commands)))

        baseline_raw = profile.get("baseline_policy", HygieneBaselinePolicy.STRICT_CLEAN.value)
        try:
            baseline_policy = HygieneBaselinePolicy(baseline_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"profile {repo_id}.{name}.baseline_policy must be one of "
                f"{[item.value for item in HygieneBaselinePolicy]}"
            ) from exc
        if baseline_policy is HygieneBaselinePolicy.NO_REGRESSION and not any(
            step.kind is VerificationStepKind.HYGIENE for step in steps
        ):
            raise ConfigError(
                f"profile {repo_id}.{name}.baseline_policy=no_regression requires a hygiene step"
            )

        command_source_context = f"repositories.{repo_id}.profiles.{name}.command_source_paths"
        declared_source_paths = _tuple_of_strings(
            profile.get("command_source_paths"), command_source_context
        )
        command_source_paths = validate_command_source_paths(
            declared_source_paths or derive_command_source_paths(tuple(commands)),
            command_source_context,
        )
        profiles[name] = ProfileConfig(
            name=name,
            description=description,
            commands=tuple(commands),
            verification=verification,
            timeout_seconds=timeout_seconds,
            working_directory=working_directory,
            command_source_paths=command_source_paths,
            steps=tuple(steps),
            baseline_policy=baseline_policy,
        )
    return profiles


def _enum_value(enum_type: type[EnumValue], value: object, context: str) -> EnumValue:
    if not isinstance(value, str):
        raise ConfigError(f"{context} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = sorted(str(item.value) for item in enum_type)
        raise ConfigError(f"{context} must be one of {allowed}") from exc


def _load_selector_config(
    table: dict[str, Any],
    *,
    name: str,
    context: str,
) -> DiagnosticSelectorConfig:
    kind = _enum_value(
        DiagnosticSelectorKind,
        table.get("kind", "none"),
        f"{context}.kind",
    )
    values = _tuple_of_strings(table.get("values"), f"{context}.values")
    char_classes = _tuple_of_strings(table.get("char_classes"), f"{context}.char_classes")
    max_length = _bounded_int(table.get("max_length"), 128, 1, 512, f"{context}.max_length")
    prefix = table.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise ConfigError(f"{context}.prefix must be a string")
    suffix = table.get("suffix")
    if suffix is not None and not isinstance(suffix, str):
        raise ConfigError(f"{context}.suffix must be a string")
    max_values = _bounded_int(table.get("max_values"), 1, 1, 16, f"{context}.max_values")
    expansion_raw = table.get("expansion", "repeat")
    if not isinstance(expansion_raw, str) or expansion_raw not in ("repeat", "join"):
        raise ConfigError(f"{context}.expansion must be 'repeat' or 'join'")
    separator = table.get("separator")
    if separator is not None and not isinstance(separator, str):
        raise ConfigError(f"{context}.separator must be a string")
    allow_leading_dash = table.get("allow_leading_dash", False)
    if not isinstance(allow_leading_dash, bool):
        raise ConfigError(f"{context}.allow_leading_dash must be a boolean")
    return DiagnosticSelectorConfig(
        kind=kind,
        name=name,
        values=values,
        char_classes=char_classes,
        max_length=max_length,
        prefix=prefix,
        suffix=suffix,
        max_values=max_values,
        expansion=expansion_raw,
        separator=separator,
        allow_leading_dash=allow_leading_dash,
    )


def _load_diagnostics(raw: Any, repo_id: str) -> dict[str, DiagnosticProfileConfig]:
    if raw is None:
        return {}
    table = _expect_mapping(raw, f"repositories.{repo_id}.diagnostics")
    diagnostics: dict[str, DiagnosticProfileConfig] = {}
    for diagnostic_id, diagnostic_raw in table.items():
        profile = _expect_mapping(
            diagnostic_raw,
            f"repositories.{repo_id}.diagnostics.{diagnostic_id}",
        )
        argv_raw = profile.get("argv")
        if (
            not isinstance(argv_raw, list)
            or not argv_raw
            or not all(isinstance(argument, str) and argument for argument in argv_raw)
        ):
            raise ConfigError(f"diagnostic {repo_id}.{diagnostic_id}.argv must be a string array")

        # The primary selector is addressed by the bare `{selector}` placeholder and keeps
        # every legacy top-level `selector_*` key working unchanged.
        primary_table = {
            "kind": profile.get("selector_kind", "none"),
            "values": profile.get("selector_values"),
            "char_classes": profile.get("selector_char_classes"),
            "max_length": profile.get("selector_max_length"),
            "prefix": profile.get("selector_prefix"),
            "suffix": profile.get("selector_suffix"),
            "max_values": profile.get("selector_max_values"),
            "expansion": profile.get("selector_expansion", "repeat"),
            "separator": profile.get("selector_separator"),
            "allow_leading_dash": profile.get("selector_allow_leading_dash", False),
        }
        selector = _load_selector_config(
            primary_table,
            name="selector",
            context=f"diagnostic {repo_id}.{diagnostic_id}.selector",
        )

        # A named second placeholder, `{selector:<name>}`, is declared through an optional
        # `[...diagnostics.<id>.selectors.<name>]` sub-table -- at most one is supported.
        selector2: DiagnosticSelectorConfig | None = None
        selectors_raw = profile.get("selectors")
        if selectors_raw is not None:
            selectors_table = _expect_mapping(
                selectors_raw, f"diagnostic {repo_id}.{diagnostic_id}.selectors"
            )
            if len(selectors_table) > 1:
                raise ConfigError(
                    f"diagnostic {repo_id}.{diagnostic_id}.selectors declares more than one "
                    "additional selector; at most two selectors are supported per diagnostic"
                )
            for extra_name, extra_raw in selectors_table.items():
                if not isinstance(extra_name, str) or extra_name == "selector":
                    raise ConfigError(
                        f"diagnostic {repo_id}.{diagnostic_id}.selectors has an invalid name: {extra_name!r}"
                    )
                extra_table = _expect_mapping(
                    extra_raw,
                    f"diagnostic {repo_id}.{diagnostic_id}.selectors.{extra_name}",
                )
                selector2 = _load_selector_config(
                    extra_table,
                    name=extra_name,
                    context=f"diagnostic {repo_id}.{diagnostic_id}.selectors.{extra_name}",
                )

        working_directory_raw = profile.get("working_directory")
        if working_directory_raw is not None and not isinstance(working_directory_raw, str):
            raise ConfigError(
                f"diagnostic {repo_id}.{diagnostic_id}.working_directory must be a string"
            )
        diagnostic = DiagnosticProfileConfig(
            diagnostic_id=str(diagnostic_id),
            summary=str(profile.get("summary", "")),
            argv_template=tuple(argv_raw),
            selector=selector,
            selector2=selector2,
            working_directory=working_directory_raw,
            timeout_seconds=_positive_int(
                profile.get("timeout_seconds"),
                30,
                f"diagnostic {repo_id}.{diagnostic_id}.timeout_seconds",
            ),
            network_policy=_enum_value(
                DiagnosticNetworkPolicy,
                profile.get("network_policy", "local_only"),
                f"diagnostic {repo_id}.{diagnostic_id}.network_policy",
            ),
            mutability=_enum_value(
                DiagnosticMutability,
                profile.get("mutability", "read_only"),
                f"diagnostic {repo_id}.{diagnostic_id}.mutability",
            ),
            parser=_enum_value(
                DiagnosticParserKind,
                profile.get("parser", "text"),
                f"diagnostic {repo_id}.{diagnostic_id}.parser",
            ),
            output_limit=_positive_int(
                profile.get("output_limit"),
                12_000,
                f"diagnostic {repo_id}.{diagnostic_id}.output_limit",
            ),
            artifact_paths=_tuple_of_strings(
                profile.get("artifact_paths"),
                f"diagnostic {repo_id}.{diagnostic_id}.artifact_paths",
            ),
        )
        diagnostics[diagnostic_id] = validate_diagnostic_profile(diagnostic)
    return diagnostics


def _load_formatters(raw: Any, repo_id: str) -> dict[str, FormatterPolicy]:
    if raw is None:
        return {}
    table = _expect_mapping(raw, f"repositories.{repo_id}.formatters")
    formatters: dict[str, FormatterPolicy] = {}
    for formatter_id, formatter_raw in table.items():
        policy = _expect_mapping(
            formatter_raw,
            f"repositories.{repo_id}.formatters.{formatter_id}",
        )
        formatters[str(formatter_id)] = FormatterPolicy(
            formatter_id=str(formatter_id),
            summary=str(policy.get("summary", "")),
            check_argv=_tuple_of_strings(
                policy.get("check_argv"),
                f"formatter {repo_id}.{formatter_id}.check_argv",
            ),
            fix_argv=_tuple_of_strings(
                policy.get("fix_argv"),
                f"formatter {repo_id}.{formatter_id}.fix_argv",
            ),
            include_globs=_tuple_of_strings(
                policy.get("include_globs"),
                f"formatter {repo_id}.{formatter_id}.include_globs",
            ),
            timeout_seconds=_positive_int(
                policy.get("timeout_seconds"),
                120,
                f"formatter {repo_id}.{formatter_id}.timeout_seconds",
            ),
            output_limit=_positive_int(
                policy.get("output_limit"),
                12_000,
                f"formatter {repo_id}.{formatter_id}.output_limit",
            ),
            max_paths=_positive_int(
                policy.get("max_paths"),
                80,
                f"formatter {repo_id}.{formatter_id}.max_paths",
            ),
            baseline_cache_ttl_seconds=_positive_int(
                policy.get("baseline_cache_ttl_seconds"),
                3_600,
                f"formatter {repo_id}.{formatter_id}.baseline_cache_ttl_seconds",
            ),
            network_policy=_enum_value(
                HygieneNetworkPolicy,
                policy.get("network_policy", "local_only"),
                f"formatter {repo_id}.{formatter_id}.network_policy",
            ),
            parser=_enum_value(
                HygieneParserKind,
                policy.get("parser", "ruff_format"),
                f"formatter {repo_id}.{formatter_id}.parser",
            ),
        )
    return formatters


def _load_github_ticket_graph(raw: Any, repo_id: str) -> GitHubTicketGraphConfig | None:
    if raw is None:
        return None
    context = f"repositories.{repo_id}.ticket_graph"
    table = _expect_mapping(raw, context)
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
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{context} contains unsupported fields: {unknown}")
    if "root_issue" not in table:
        raise ConfigError(f"{context}.root_issue is required")
    root_issue = _positive_int(table.get("root_issue"), 1, f"{context}.root_issue")
    repository_raw = table.get("repository")
    repository = str(repository_raw).strip() if repository_raw is not None else None
    if repository is not None and not _SAFE_GITHUB_REPOSITORY.fullmatch(repository):
        raise ConfigError(f"{context}.repository must use owner/name format")
    owner_raw = table.get("project_owner")
    owner = str(owner_raw).strip() if owner_raw is not None else None
    number_raw = table.get("project_number")
    number = (
        _positive_int(number_raw, 1, f"{context}.project_number")
        if number_raw is not None
        else None
    )
    if (owner is None) != (number is None) or owner == "":
        raise ConfigError(
            f"{context}.project_owner and {context}.project_number must be configured together"
        )
    owner_type = str(table.get("project_owner_type", "organization"))
    if owner_type not in {"organization", "user"}:
        raise ConfigError(f"{context}.project_owner_type must be 'organization' or 'user'")

    def field_name(name: str, default: str) -> str:
        value = table.get(name, default)
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise ConfigError(f"{context}.{name} must be a non-empty bounded string")
        return value.strip()

    return GitHubTicketGraphConfig(
        root_issue=root_issue,
        repository=repository,
        project_owner=owner,
        project_number=number,
        project_owner_type=owner_type,
        status_field=field_name("status_field", "Status"),
        priority_field=field_name("priority_field", "Priority"),
        initiative_field=field_name("initiative_field", "Initiative"),
        type_field=field_name("type_field", "Type"),
    )


def _load_risk_policy(
    raw: Any,
    repo_id: str,
    *,
    final_profile: str,
    profiles: dict[str, ProfileConfig],
    diagnostics: dict[str, DiagnosticProfileConfig],
) -> RiskPolicy:
    defaults = default_risk_policy(final_profile=final_profile)
    if raw is None:
        return defaults
    table = _expect_mapping(raw, f"repositories.{repo_id}.risk")

    def threshold(name: str, default: int) -> int:
        value = table.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
            raise ConfigError(f"repositories.{repo_id}.risk.{name} must be an integer in 0..100")
        return value

    resolved_final = str(table.get("final_profile", defaults.final_profile))
    if profiles and (resolved_final not in profiles or not profiles[resolved_final].verification):
        raise ConfigError(
            f"repositories.{repo_id}.risk.final_profile must reference a verification profile"
        )
    ordered_profiles = _tuple_of_strings(
        table.get("ordered_profiles", list(defaults.ordered_profiles)),
        f"repositories.{repo_id}.risk.ordered_profiles",
    )
    if profiles:
        unknown_profiles = [
            item
            for item in ordered_profiles
            if item not in profiles or not profiles[item].verification
        ]
        if unknown_profiles:
            raise ConfigError(
                f"repositories.{repo_id}.risk.ordered_profiles contains unknown verification profiles: {unknown_profiles}"
            )
    narrow_diagnostics = _tuple_of_strings(
        table.get("narrow_diagnostics", list(defaults.narrow_diagnostics)),
        f"repositories.{repo_id}.risk.narrow_diagnostics",
    )
    if diagnostics:
        unknown_diagnostics = [item for item in narrow_diagnostics if item not in diagnostics]
        if unknown_diagnostics:
            raise ConfigError(
                f"repositories.{repo_id}.risk.narrow_diagnostics contains unknown diagnostics: {unknown_diagnostics}"
            )
    try:
        return RiskPolicy(
            low_max=threshold("low_max", defaults.low_max),
            medium_max=threshold("medium_max", defaults.medium_max),
            high_max=threshold("high_max", defaults.high_max),
            critical_globs=_tuple_of_strings(
                table.get("critical_globs", list(defaults.critical_globs)),
                f"repositories.{repo_id}.risk.critical_globs",
            ),
            public_contract_globs=_tuple_of_strings(
                table.get("public_contract_globs", list(defaults.public_contract_globs)),
                f"repositories.{repo_id}.risk.public_contract_globs",
            ),
            manifest_globs=_tuple_of_strings(
                table.get("manifest_globs", list(defaults.manifest_globs)),
                f"repositories.{repo_id}.risk.manifest_globs",
            ),
            docs_globs=_tuple_of_strings(
                table.get("docs_globs", list(defaults.docs_globs)),
                f"repositories.{repo_id}.risk.docs_globs",
            ),
            narrow_diagnostics=narrow_diagnostics,
            ordered_profiles=ordered_profiles,
            final_profile=resolved_final,
        )
    except ValueError as exc:
        raise ConfigError(f"repositories.{repo_id}.risk is invalid: {exc}") from exc


def load_config(path: str | Path | None = None) -> AppConfig:
    config_value: str | Path = path or os.environ.get("REPOFORGE_CONFIG", str(DEFAULT_CONFIG_PATH))
    config_path = Path(config_value).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"Configuration file not found: {config_path}. "
            "Run `repoforge init --repo /path/to/repo` first."
        )
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load configuration {config_path}: {exc}") from exc
    base_dir = config_path.parent
    server_raw = _expect_mapping(raw.get("server", {}), "server")
    server = ServerConfig(
        workspace_root=_expand_path(
            str(server_raw.get("workspace_root", DEFAULT_WORKSPACE_ROOT)), base_dir=base_dir
        ),
        state_root=_expand_path(
            str(server_raw.get("state_root", DEFAULT_STATE_ROOT)), base_dir=base_dir
        ),
        max_file_bytes=_positive_int(
            server_raw.get("max_file_bytes"), 2_000_000, "server.max_file_bytes"
        ),
        max_tool_output_chars=_positive_int(
            server_raw.get("max_tool_output_chars"), 120_000, "server.max_tool_output_chars"
        ),
        default_command_timeout_seconds=_positive_int(
            server_raw.get("default_command_timeout_seconds"),
            120,
            "server.default_command_timeout_seconds",
        ),
        verification_timeout_seconds=_positive_int(
            server_raw.get("verification_timeout_seconds"),
            1_800,
            "server.verification_timeout_seconds",
        ),
        max_fingerprint_bytes=_positive_int(
            server_raw.get("max_fingerprint_bytes"),
            50 * 1024 * 1024,
            "server.max_fingerprint_bytes",
        ),
        max_batch_files=_positive_int(
            server_raw.get("max_batch_files"), 20, "server.max_batch_files"
        ),
        audit_max_bytes=_positive_int(
            server_raw.get("audit_max_bytes"), 5_000_000, "server.audit_max_bytes"
        ),
        audit_backup_count=_positive_int(
            server_raw.get("audit_backup_count"), 3, "server.audit_backup_count"
        ),
        runtime_log_max_bytes=_positive_int(
            server_raw.get("runtime_log_max_bytes"),
            5_000_000,
            "server.runtime_log_max_bytes",
        ),
        runtime_log_backup_count=_positive_int(
            server_raw.get("runtime_log_backup_count"),
            3,
            "server.runtime_log_backup_count",
        ),
        idempotency_stale_seconds=_positive_int(
            server_raw.get("idempotency_stale_seconds"),
            900,
            "server.idempotency_stale_seconds",
        ),
        idempotency_lock_timeout_seconds=_positive_int(
            server_raw.get("idempotency_lock_timeout_seconds"),
            2,
            "server.idempotency_lock_timeout_seconds",
        ),
        max_background_profiles=_positive_int(
            server_raw.get("max_background_profiles"),
            2,
            "server.max_background_profiles",
        ),
        fast_fail_threshold_seconds=_bounded_float(
            server_raw.get("fast_fail_threshold_seconds"),
            10.0,
            0.0,
            3_600.0,
            "server.fast_fail_threshold_seconds",
        ),
        stale_workspace_candidate_threshold=_bounded_int(
            server_raw.get("stale_workspace_candidate_threshold"),
            3,
            1,
            1_000,
            "server.stale_workspace_candidate_threshold",
        ),
        stale_workspace_min_age_seconds=_bounded_float(
            server_raw.get("stale_workspace_min_age_seconds"),
            3_600.0,
            0.0,
            30 * 24 * 3_600.0,
            "server.stale_workspace_min_age_seconds",
        ),
        github_read_cache_ttl_seconds=_bounded_int(
            server_raw.get("github_read_cache_ttl_seconds"),
            120,
            60,
            300,
            "server.github_read_cache_ttl_seconds",
        ),
        github_webhook_enabled=_boolean(
            server_raw.get("github_webhook_enabled"),
            False,
            "server.github_webhook_enabled",
        ),
        github_webhook_bind=_webhook_bind(server_raw.get("github_webhook_bind")),
        github_webhook_port=_bounded_int(
            server_raw.get("github_webhook_port"),
            8766,
            1,
            65535,
            "server.github_webhook_port",
        ),
        github_webhook_secret_env=_environment_name(
            server_raw.get("github_webhook_secret_env", "REPOFORGE_GITHUB_WEBHOOK_SECRET"),
            "server.github_webhook_secret_env",
        ),
        github_webhook_max_body_bytes=_bounded_int(
            server_raw.get("github_webhook_max_body_bytes"),
            1_000_000,
            1_024,
            10_000_000,
            "server.github_webhook_max_body_bytes",
        ),
        path_prefixes=_tuple_of_strings(server_raw.get("path_prefixes"), "server.path_prefixes")
        or DEFAULT_PATH_PREFIXES,
        allowed_environment=_tuple_of_strings(
            server_raw.get("allowed_environment"), "server.allowed_environment"
        )
        or DEFAULT_ALLOWED_ENVIRONMENT,
        resource_budget=_load_resource_budget(
            server_raw.get("resource_budget"),
            "server.resource_budget",
        ),
    )
    repositories_raw = _expect_mapping(raw.get("repositories"), "repositories")
    if not repositories_raw:
        raise ConfigError("At least one repository must be configured")
    repositories: dict[str, RepositoryConfig] = {}
    for repo_id, repo_raw_any in repositories_raw.items():
        if not _SAFE_REPO_ID.fullmatch(repo_id):
            raise ConfigError(f"Unsafe repository id: {repo_id!r}")
        repo_raw = _resolve_repository_preset(
            _expect_mapping(repo_raw_any, f"repositories.{repo_id}"), repo_id
        )
        if "path" not in repo_raw:
            raise ConfigError(f"repositories.{repo_id}.path is required")
        default_base = _safe_ref(
            str(repo_raw.get("default_base", "main")), f"repositories.{repo_id}.default_base"
        )
        allowed_bases = _tuple_of_strings(
            repo_raw.get("allowed_base_branches", [default_base]),
            f"repositories.{repo_id}.allowed_base_branches",
        )
        if not allowed_bases:
            allowed_bases = (default_base,)
        allowed_bases = tuple(
            _safe_ref(item, f"repositories.{repo_id}.allowed_base_branches")
            for item in allowed_bases
        )
        if default_base not in allowed_bases:
            raise ConfigError(
                f"repositories.{repo_id}.default_base must be in allowed_base_branches"
            )
        branch_prefix = str(repo_raw.get("branch_prefix", "ai/"))
        if not branch_prefix.endswith("/") or not _SAFE_BRANCH_COMPONENT.fullmatch(branch_prefix):
            raise ConfigError(
                f"repositories.{repo_id}.branch_prefix must be a safe prefix ending in '/': "
                f"{branch_prefix!r}"
            )
        protected = _tuple_of_strings(
            repo_raw.get("protected_branches", list(DEFAULT_PROTECTED_BRANCHES)),
            f"repositories.{repo_id}.protected_branches",
        )
        profiles = _load_profiles(repo_raw.get("profiles"), repo_id)
        diagnostics = _load_diagnostics(repo_raw.get("diagnostics"), repo_id)
        formatters = _load_formatters(repo_raw.get("formatters"), repo_id)
        ticket_graph = _load_github_ticket_graph(repo_raw.get("ticket_graph"), repo_id)
        default_verification_raw = repo_raw.get("default_verification_profile")
        default_verification = (
            str(default_verification_raw) if default_verification_raw is not None else None
        )
        if default_verification:
            if default_verification not in profiles:
                raise ConfigError(
                    f"repositories.{repo_id}.default_verification_profile references unknown "
                    f"profile {default_verification!r}"
                )
            if not profiles[default_verification].verification:
                raise ConfigError(
                    f"repositories.{repo_id}.default_verification_profile must reference a "
                    "verification profile"
                )
        verification_profiles = tuple(
            name for name, profile in profiles.items() if profile.verification
        )
        risk_final_profile = default_verification or (
            verification_profiles[-1] if verification_profiles else "full"
        )
        risk_policy = _load_risk_policy(
            repo_raw.get("risk"),
            repo_id,
            final_profile=risk_final_profile,
            profiles=profiles,
            diagnostics=diagnostics,
        )
        resource_budget = _load_resource_budget(
            repo_raw.get("resource_budget"),
            f"repositories.{repo_id}.resource_budget",
            defaults=server.resource_budget,
            ceiling=server.resource_budget,
        )
        allowed_mutation_ops = validate_allowed_mutation_ops(
            _tuple_of_strings(
                repo_raw.get("allowed_mutation_ops", list(MUTATION_OPS)),
                f"repositories.{repo_id}.allowed_mutation_ops",
            ),
            repo_id,
        )
        execution_mode = _enum_value(
            ExecutionMode,
            repo_raw.get("execution_mode", "strict"),
            f"repositories.{repo_id}.execution_mode",
        )
        adhoc_runners = validate_adhoc_runners(
            _tuple_of_strings(
                repo_raw.get("adhoc_runners"), f"repositories.{repo_id}.adhoc_runners"
            ),
            repo_id,
        )
        if execution_mode is ExecutionMode.RELAXED and not adhoc_runners:
            raise ConfigError(
                f"repositories.{repo_id}.execution_mode='relaxed' requires a non-empty adhoc_runners allowlist"
            )
        adhoc_timeout_seconds = _bounded_int(
            repo_raw.get("adhoc_timeout_seconds"),
            300,
            1,
            3_600,
            f"repositories.{repo_id}.adhoc_timeout_seconds",
        )
        repositories[repo_id] = RepositoryConfig(
            repo_id=repo_id,
            path=_expand_path(str(repo_raw["path"]), base_dir=base_dir),
            display_name=str(repo_raw.get("display_name", repo_id)),
            remote=_safe_remote(
                str(repo_raw.get("remote", "origin")), f"repositories.{repo_id}.remote"
            ),
            default_base=default_base,
            allowed_base_branches=allowed_bases,
            branch_prefix=branch_prefix,
            protected_branches=protected,
            read_only=_boolean(
                repo_raw.get("read_only"),
                False,
                f"repositories.{repo_id}.read_only",
            ),
            publish_enabled=_boolean(
                repo_raw.get("publish_enabled"),
                True,
                f"repositories.{repo_id}.publish_enabled",
            ),
            require_verification_before_commit=_boolean(
                repo_raw.get("require_verification_before_commit"),
                True,
                f"repositories.{repo_id}.require_verification_before_commit",
            ),
            fetch_before_workspace=_boolean(
                repo_raw.get("fetch_before_workspace"),
                True,
                f"repositories.{repo_id}.fetch_before_workspace",
            ),
            default_verification_profile=default_verification,
            max_changed_files=_positive_int(
                repo_raw.get("max_changed_files"), 150, f"repositories.{repo_id}.max_changed_files"
            ),
            max_diff_lines=_positive_int(
                repo_raw.get("max_diff_lines"), 12_000, f"repositories.{repo_id}.max_diff_lines"
            ),
            max_total_changed_bytes=_positive_int(
                repo_raw.get("max_total_changed_bytes"),
                25 * 1024 * 1024,
                f"repositories.{repo_id}.max_total_changed_bytes",
            ),
            allowed_paths=_tuple_of_strings(
                repo_raw.get("allowed_paths", []), f"repositories.{repo_id}.allowed_paths"
            ),
            denied_paths=_tuple_of_strings(
                repo_raw.get("denied_paths", list(DEFAULT_DENIED_PATHS)),
                f"repositories.{repo_id}.denied_paths",
            ),
            allowed_mutation_ops=allowed_mutation_ops,
            pr_labels=_tuple_of_strings(
                repo_raw.get("pr_labels", []), f"repositories.{repo_id}.pr_labels"
            ),
            pr_reviewers=_tuple_of_strings(
                repo_raw.get("pr_reviewers", []), f"repositories.{repo_id}.pr_reviewers"
            ),
            no_maintainer_edit=_boolean(
                repo_raw.get("no_maintainer_edit"),
                False,
                f"repositories.{repo_id}.no_maintainer_edit",
            ),
            profiles=profiles,
            diagnostics=diagnostics,
            formatters=formatters,
            risk_policy=risk_policy,
            resource_budget=resource_budget,
            execution_mode=execution_mode,
            adhoc_runners=adhoc_runners,
            adhoc_timeout_seconds=adhoc_timeout_seconds,
            ticket_graph=ticket_graph,
        )
    providers = load_provider_manifests(raw.get("providers"))
    return AppConfig(
        source_path=config_path,
        server=server,
        repositories=repositories,
        providers=providers,
    )
