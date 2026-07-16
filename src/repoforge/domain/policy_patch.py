"""Typed repository policy patches layered over template-derived proposals.

A patch is the durable record of deliberate, reviewed customization (custom command
profiles, diagnostics, formatters) that must survive ``rf repo refresh``. Profiles are
fully typed here. Diagnostic and formatter tables are shape-validated against the exact
key allowlist that :func:`repoforge.application.configuration.document.render_resolved`
can render; their deep semantic validation stays single-sourced in
:func:`repoforge.config.load_config`, which every candidate resolved configuration must
pass before it can be accepted as a generation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .verification_steps import (
    HygieneBaselinePolicy,
    VerificationStep,
    VerificationStepKind,
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

MAX_PATCH_ENTRIES = 16
MAX_COMMANDS_PER_PROFILE = 16
MAX_ARGUMENTS_PER_COMMAND = 64
MAX_ARGUMENT_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 500
MAX_TIMEOUT_SECONDS = 7_200

# Keys ``render_resolved`` can persist for a diagnostic; anything else would be
# silently dropped between acceptance and load, so it is rejected here instead.
DIAGNOSTIC_SCALAR_KEYS = frozenset(
    {
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
    }
)
DIAGNOSTIC_LIST_KEYS = frozenset(
    {"argv", "selector_values", "selector_char_classes", "artifact_paths"}
)
DIAGNOSTIC_SELECTOR_SCALAR_KEYS = frozenset(
    {
        "kind",
        "max_length",
        "max_values",
        "expansion",
        "separator",
        "prefix",
        "suffix",
        "allow_leading_dash",
    }
)
DIAGNOSTIC_SELECTOR_LIST_KEYS = frozenset({"values", "char_classes"})
FORMATTER_SCALAR_KEYS = frozenset(
    {
        "summary",
        "timeout_seconds",
        "output_limit",
        "max_paths",
        "baseline_cache_ttl_seconds",
        "network_policy",
        "parser",
    }
)
FORMATTER_LIST_KEYS = frozenset({"check_argv", "fix_argv", "include_globs"})


class PolicyPatchError(ValueError):
    """Raised when a policy patch is malformed or exceeds its bounds."""


def _safe_name(value: object, context: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise PolicyPatchError(
            f"{context} must match [A-Za-z0-9._-] and be at most 64 characters: {value!r}"
        )
    return value


def _bounded_str(value: object, context: str, *, limit: int = MAX_ARGUMENT_LENGTH) -> str:
    if not isinstance(value, str):
        raise PolicyPatchError(f"{context} must be a string")
    if len(value) > limit:
        raise PolicyPatchError(f"{context} exceeds {limit} characters")
    return value


def _validate_command(command: object, context: str) -> tuple[str, ...]:
    if not isinstance(command, (list, tuple)) or not command:
        raise PolicyPatchError(f"{context} must be a non-empty argument array")
    if len(command) > MAX_ARGUMENTS_PER_COMMAND:
        raise PolicyPatchError(f"{context} exceeds {MAX_ARGUMENTS_PER_COMMAND} arguments")
    arguments: list[str] = []
    for index, argument in enumerate(command):
        text = _bounded_str(argument, f"{context}[{index}]")
        if not text:
            raise PolicyPatchError(f"{context}[{index}] must be a non-empty string")
        arguments.append(text)
    return tuple(arguments)


@dataclass(frozen=True, slots=True)
class ProfilePatch:
    """One complete replacement definition for a named command profile."""

    name: str
    description: str
    commands: tuple[tuple[str, ...], ...]
    verification: bool = False
    timeout_seconds: int | None = None
    working_directory: str | None = None
    steps: tuple[VerificationStep, ...] = ()
    baseline_policy: HygieneBaselinePolicy = HygieneBaselinePolicy.STRICT_CLEAN

    def __post_init__(self) -> None:
        _safe_name(self.name, "profile name")
        _bounded_str(
            self.description, f"profile {self.name}.description", limit=MAX_DESCRIPTION_LENGTH
        )
        if not self.commands:
            raise PolicyPatchError(f"profile {self.name} requires at least one command")
        if len(self.commands) > MAX_COMMANDS_PER_PROFILE:
            raise PolicyPatchError(
                f"profile {self.name} exceeds {MAX_COMMANDS_PER_PROFILE} commands"
            )
        validated = tuple(
            _validate_command(command, f"profile {self.name}.commands[{index}]")
            for index, command in enumerate(self.commands)
        )
        object.__setattr__(self, "commands", validated)
        if not isinstance(self.verification, bool):
            raise PolicyPatchError(f"profile {self.name}.verification must be a boolean")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise PolicyPatchError(
                f"profile {self.name}.timeout_seconds must be an integer in 1..{MAX_TIMEOUT_SECONDS}"
            )
        if self.working_directory is not None:
            workdir = (
                _bounded_str(self.working_directory, f"profile {self.name}.working_directory")
                .replace("\\", "/")
                .strip("/")
            )
            if (
                not workdir
                or workdir.startswith("-")
                or any(part in {"", ".", ".."} for part in workdir.split("/"))
            ):
                raise PolicyPatchError(
                    f"profile {self.name}.working_directory must be a safe relative path"
                )
            object.__setattr__(self, "working_directory", workdir)
        if self.steps:
            step_ids = [step.step_id for step in self.steps]
            if len(set(step_ids)) != len(step_ids):
                raise PolicyPatchError(f"profile {self.name}.steps contains duplicate ids")
            if tuple(step.command for step in self.steps) != self.commands:
                raise PolicyPatchError(f"profile {self.name}.commands must match steps commands")
        if self.baseline_policy is HygieneBaselinePolicy.NO_REGRESSION and not any(
            step.kind is VerificationStepKind.HYGIENE for step in self.steps
        ):
            raise PolicyPatchError(
                f"profile {self.name}.baseline_policy=no_regression requires a hygiene step"
            )

    def as_table(self) -> dict[str, Any]:
        table: dict[str, Any] = {
            "description": self.description,
            "verification": self.verification,
            "commands": [list(command) for command in self.commands],
        }
        if self.timeout_seconds is not None:
            table["timeout_seconds"] = self.timeout_seconds
        if self.working_directory is not None:
            table["working_directory"] = self.working_directory
        if self.steps:
            table["steps"] = [
                {
                    "id": step.step_id,
                    "kind": step.kind.value,
                    "command": list(step.command),
                }
                for step in self.steps
            ]
        if self.baseline_policy is not HygieneBaselinePolicy.STRICT_CLEAN:
            table["baseline_policy"] = self.baseline_policy.value
        return table

    @classmethod
    def from_table(cls, name: str, raw: object) -> ProfilePatch:
        if not isinstance(raw, dict):
            raise PolicyPatchError(f"profile {name} patch must be a table")
        unknown = sorted(
            set(raw)
            - {
                "description",
                "verification",
                "commands",
                "timeout_seconds",
                "working_directory",
                "steps",
                "baseline_policy",
            }
        )
        if unknown:
            raise PolicyPatchError(f"profile {name} patch has unsupported keys: {unknown}")
        commands_raw = raw.get("commands")
        if not isinstance(commands_raw, (list, tuple)):
            raise PolicyPatchError(f"profile {name} patch requires a commands array")
        verification = raw.get("verification", False)
        if not isinstance(verification, bool):
            raise PolicyPatchError(f"profile {name}.verification must be a boolean")
        commands = tuple(
            _validate_command(command, f"profile {name}.commands[{index}]")
            for index, command in enumerate(commands_raw)
        )
        raw_steps = raw.get("steps", [])
        if not isinstance(raw_steps, (list, tuple)):
            raise PolicyPatchError(f"profile {name}.steps must be an array of inline tables")
        steps: list[VerificationStep] = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict) or set(raw_step) != {"id", "kind", "command"}:
                raise PolicyPatchError(
                    f"profile {name}.steps[{index}] must contain id, kind, and command"
                )
            try:
                steps.append(
                    VerificationStep(
                        str(raw_step["id"]),
                        VerificationStepKind(str(raw_step["kind"])),
                        _validate_command(
                            raw_step["command"], f"profile {name}.steps[{index}].command"
                        ),
                    )
                )
            except ValueError as exc:
                raise PolicyPatchError(f"Invalid profile {name}.steps[{index}]: {exc}") from exc
        try:
            baseline_policy = HygieneBaselinePolicy(
                str(raw.get("baseline_policy", HygieneBaselinePolicy.STRICT_CLEAN.value))
            )
        except ValueError as exc:
            raise PolicyPatchError(f"profile {name}.baseline_policy is invalid") from exc
        return cls(
            name=name,
            description=str(raw.get("description", "")),
            commands=commands,
            verification=verification,
            timeout_seconds=raw.get("timeout_seconds"),
            working_directory=raw.get("working_directory"),
            steps=tuple(steps),
            baseline_policy=baseline_policy,
        )


def _validate_bounded_table(
    raw: object,
    context: str,
    *,
    scalar_keys: frozenset[str],
    list_keys: frozenset[str],
    nested_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PolicyPatchError(f"{context} must be a table")
    allowed = scalar_keys | list_keys | ({nested_key} if nested_key else set())
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PolicyPatchError(f"{context} has unrenderable or unsupported keys: {unknown}")
    result: dict[str, Any] = {}
    for key in sorted(raw):
        value = raw[key]
        if key in scalar_keys:
            if not isinstance(value, (str, int, bool)):
                raise PolicyPatchError(f"{context}.{key} must be a string, integer, or boolean")
            if isinstance(value, str):
                _bounded_str(value, f"{context}.{key}")
            result[key] = value
        elif key in list_keys:
            if not isinstance(value, list) or len(value) > MAX_ARGUMENTS_PER_COMMAND:
                raise PolicyPatchError(
                    f"{context}.{key} must be a string array of at most "
                    f"{MAX_ARGUMENTS_PER_COMMAND} entries"
                )
            result[key] = [_bounded_str(item, f"{context}.{key}[]") for item in value]
        elif nested_key is not None and key == nested_key:
            if not isinstance(value, dict) or len(value) > 1:
                raise PolicyPatchError(f"{context}.{key} supports at most one named selector")
            nested: dict[str, Any] = {}
            for selector_name in sorted(value):
                nested[_safe_name(selector_name, f"{context}.{key} name")] = (
                    _validate_bounded_table(
                        value[selector_name],
                        f"{context}.{key}.{selector_name}",
                        scalar_keys=DIAGNOSTIC_SELECTOR_SCALAR_KEYS,
                        list_keys=DIAGNOSTIC_SELECTOR_LIST_KEYS,
                    )
                )
            result[key] = nested
    return result


def validate_diagnostic_table(name: str, raw: object) -> dict[str, Any]:
    table = _validate_bounded_table(
        raw,
        f"diagnostic {name}",
        scalar_keys=DIAGNOSTIC_SCALAR_KEYS,
        list_keys=DIAGNOSTIC_LIST_KEYS,
        nested_key="selectors",
    )
    if not table.get("argv"):
        raise PolicyPatchError(f"diagnostic {name} requires a non-empty argv array")
    return table


def validate_formatter_table(name: str, raw: object) -> dict[str, Any]:
    table = _validate_bounded_table(
        raw,
        f"formatter {name}",
        scalar_keys=FORMATTER_SCALAR_KEYS,
        list_keys=FORMATTER_LIST_KEYS,
    )
    if not table.get("check_argv"):
        raise PolicyPatchError(f"formatter {name} requires a non-empty check_argv array")
    return table


def _validate_names(values: object, context: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise PolicyPatchError(f"{context} must be an array of names")
    if len(values) > MAX_PATCH_ENTRIES:
        raise PolicyPatchError(f"{context} exceeds {MAX_PATCH_ENTRIES} entries")
    return tuple(sorted({_safe_name(value, context) for value in values}))


@dataclass(frozen=True, slots=True)
class RepositoryPolicyPatch:
    """Durable overlay applied to one repository after template proposal application."""

    profiles: tuple[ProfilePatch, ...] = ()
    diagnostics: tuple[tuple[str, dict[str, Any]], ...] = ()
    formatters: tuple[tuple[str, dict[str, Any]], ...] = ()
    remove_profiles: tuple[str, ...] = ()
    remove_diagnostics: tuple[str, ...] = ()
    remove_formatters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, entries in (
            ("profiles", self.profiles),
            ("diagnostics", self.diagnostics),
            ("formatters", self.formatters),
        ):
            if len(entries) > MAX_PATCH_ENTRIES:
                raise PolicyPatchError(f"patch {label} exceeds {MAX_PATCH_ENTRIES} entries")
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            raise PolicyPatchError("patch profiles contain duplicate names")
        object.__setattr__(
            self, "profiles", tuple(sorted(self.profiles, key=lambda item: item.name))
        )
        for attribute in ("diagnostics", "formatters"):
            entries = getattr(self, attribute)
            seen = [name for name, _ in entries]
            if len(seen) != len(set(seen)):
                raise PolicyPatchError(f"patch {attribute} contain duplicate names")
            validator = (
                validate_diagnostic_table
                if attribute == "diagnostics"
                else validate_formatter_table
            )
            object.__setattr__(
                self,
                attribute,
                tuple(
                    (_safe_name(name, f"{attribute} name"), validator(name, table))
                    for name, table in sorted(entries)
                ),
            )
        object.__setattr__(
            self, "remove_profiles", _validate_names(self.remove_profiles, "remove_profiles")
        )
        object.__setattr__(
            self,
            "remove_diagnostics",
            _validate_names(self.remove_diagnostics, "remove_diagnostics"),
        )
        object.__setattr__(
            self,
            "remove_formatters",
            _validate_names(self.remove_formatters, "remove_formatters"),
        )
        for label, set_names, removed in (
            ("profiles", {profile.name for profile in self.profiles}, self.remove_profiles),
            ("diagnostics", {name for name, _ in self.diagnostics}, self.remove_diagnostics),
            ("formatters", {name for name, _ in self.formatters}, self.remove_formatters),
        ):
            overlap = sorted(set_names & set(removed))
            if overlap:
                raise PolicyPatchError(f"patch {label} both set and remove: {overlap}")

    def is_empty(self) -> bool:
        return not (
            self.profiles
            or self.diagnostics
            or self.formatters
            or self.remove_profiles
            or self.remove_diagnostics
            or self.remove_formatters
        )

    def merge(self, other: RepositoryPolicyPatch) -> RepositoryPolicyPatch:
        """Layer ``other`` on top of this patch; later sets win, later removes drop sets."""

        profiles = {profile.name: profile for profile in self.profiles}
        for profile in other.profiles:
            profiles[profile.name] = profile
        for name in other.remove_profiles:
            profiles.pop(name, None)
        diagnostics = dict(self.diagnostics)
        for name, table in other.diagnostics:
            diagnostics[name] = table
        for name in other.remove_diagnostics:
            diagnostics.pop(name, None)
        formatters = dict(self.formatters)
        for name, table in other.formatters:
            formatters[name] = table
        for name in other.remove_formatters:
            formatters.pop(name, None)
        remove_profiles = (set(self.remove_profiles) | set(other.remove_profiles)) - set(profiles)
        remove_diagnostics = (set(self.remove_diagnostics) | set(other.remove_diagnostics)) - set(
            diagnostics
        )
        remove_formatters = (set(self.remove_formatters) | set(other.remove_formatters)) - set(
            formatters
        )
        return RepositoryPolicyPatch(
            profiles=tuple(profiles.values()),
            diagnostics=tuple(diagnostics.items()),
            formatters=tuple(formatters.items()),
            remove_profiles=tuple(remove_profiles),
            remove_diagnostics=tuple(remove_diagnostics),
            remove_formatters=tuple(remove_formatters),
        )

    def as_table(self) -> dict[str, Any]:
        table: dict[str, Any] = {}
        if self.remove_profiles:
            table["remove_profiles"] = list(self.remove_profiles)
        if self.remove_diagnostics:
            table["remove_diagnostics"] = list(self.remove_diagnostics)
        if self.remove_formatters:
            table["remove_formatters"] = list(self.remove_formatters)
        if self.profiles:
            table["profiles"] = {profile.name: profile.as_table() for profile in self.profiles}
        if self.diagnostics:
            table["diagnostics"] = {name: value for name, value in self.diagnostics}
        if self.formatters:
            table["formatters"] = {name: value for name, value in self.formatters}
        return table

    @classmethod
    def from_table(cls, raw: object) -> RepositoryPolicyPatch:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise PolicyPatchError("policy_patch must be a table")
        unknown = sorted(
            set(raw)
            - {
                "profiles",
                "diagnostics",
                "formatters",
                "remove_profiles",
                "remove_diagnostics",
                "remove_formatters",
            }
        )
        if unknown:
            raise PolicyPatchError(f"policy_patch has unsupported keys: {unknown}")
        profiles_raw = raw.get("profiles", {})
        if not isinstance(profiles_raw, dict):
            raise PolicyPatchError("policy_patch.profiles must be a table")
        diagnostics_raw = raw.get("diagnostics", {})
        if not isinstance(diagnostics_raw, dict):
            raise PolicyPatchError("policy_patch.diagnostics must be a table")
        formatters_raw = raw.get("formatters", {})
        if not isinstance(formatters_raw, dict):
            raise PolicyPatchError("policy_patch.formatters must be a table")
        return cls(
            profiles=tuple(
                ProfilePatch.from_table(_safe_name(name, "profile name"), value)
                for name, value in sorted(profiles_raw.items())
            ),
            diagnostics=tuple(sorted(diagnostics_raw.items())),
            formatters=tuple(sorted(formatters_raw.items())),
            remove_profiles=tuple(raw.get("remove_profiles", ())),
            remove_diagnostics=tuple(raw.get("remove_diagnostics", ())),
            remove_formatters=tuple(raw.get("remove_formatters", ())),
        )

    def canonical_json(self) -> str:
        return json.dumps(self.as_table(), sort_keys=True, ensure_ascii=False)
