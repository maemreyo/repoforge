"""Backend-neutral execution policy, enforcement, identity, and evidence models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

_ENVIRONMENT_IDENTITY_SCHEMA_VERSION = 2
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_NONEMPTY_NAME = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_\-.]{0,127}$")
_TOOL_PATTERN = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_\-.]{0,63}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_][ -~]{0,127}$")
_MAX_WARNINGS = 20
_MAX_WARNING_LENGTH = 256


def _stable_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _validate_optional_limit(name: str, value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or None")


class NetworkAccess(str, Enum):
    OFFLINE = "offline"
    PUBLIC_HTTP_HTTPS = "public_http_https"
    PUBLIC_GENERAL = "public_general"
    PRIVATE_APPROVED = "private_approved"
    HOST_INHERITED = "host_inherited"


class FilesystemAccess(str, Enum):
    SOURCE_READ = "source_read"
    WORKSPACE_WRITE = "workspace_write"
    MANAGED_STATE_WRITE = "managed_state_write"
    HOST_ACCOUNT_ACCESS = "host_account_access"


class CredentialCapability(str, Enum):
    GITHUB_READ = "github_read"
    PACKAGE_REGISTRY_READ = "package_registry_read"


class EnforcementRequirement(str, Enum):
    ADVISORY_BACKEND_ALLOWED = "advisory_backend_allowed"
    ENFORCEMENT_REQUIRED = "enforcement_required"


class EnforcementLevel(str, Enum):
    ENFORCED = "enforced"
    ADVISORY = "advisory"
    OBSERVED = "observed"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"


class CommandFailureMode(str, Enum):
    RAISE = "raise"
    RETURN = "return"


class ExecutionScopeKind(str, Enum):
    WORKSPACE = "workspace"
    SNAPSHOT_READ_ONLY = "snapshot_read_only"


class EnvironmentAdapterKind(str, Enum):
    NATIVE_REVIEWED = "native_reviewed"
    DEV_CONTAINER = "dev_container"
    HERMETIC_CONTAINER = "hermetic_container"


# Compatibility enums retained while reviewed configuration is compiled into the
# backend-neutral request model. They must never be treated as effective evidence.
class NetworkPolicy(str, Enum):
    NONE = "none"
    RESTRICTED = "restricted"
    EXTERNAL = "external"


class FilesystemCapability(str, Enum):
    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    MANAGED_STATE_WRITE = "managed_state_write"


@dataclass(frozen=True, slots=True)
class RequestedResourceLimits:
    cpu_seconds: int | None = None
    memory_bytes: int | None = None
    disk_bytes: int | None = None
    subprocesses: int | None = None
    network_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "cpu_seconds",
            "memory_bytes",
            "disk_bytes",
            "subprocesses",
            "network_bytes",
        ):
            _validate_optional_limit(name, getattr(self, name))

    def payload(self) -> dict[str, int | None]:
        return {
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "disk_bytes": self.disk_bytes,
            "subprocesses": self.subprocesses,
            "network_bytes": self.network_bytes,
        }


@dataclass(frozen=True, slots=True)
class EffectiveResourceLimits:
    cpu_seconds: int | None = None
    memory_bytes: int | None = None
    disk_bytes: int | None = None
    subprocesses: int | None = None
    network_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "cpu_seconds",
            "memory_bytes",
            "disk_bytes",
            "subprocesses",
            "network_bytes",
        ):
            _validate_optional_limit(name, getattr(self, name))

    def payload(self) -> dict[str, int | None]:
        return {
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "disk_bytes": self.disk_bytes,
            "subprocesses": self.subprocesses,
            "network_bytes": self.network_bytes,
        }


@dataclass(frozen=True, slots=True)
class EnforcementAssessment:
    network: EnforcementLevel = EnforcementLevel.ADVISORY
    filesystem: EnforcementLevel = EnforcementLevel.ADVISORY
    timeout: EnforcementLevel = EnforcementLevel.ENFORCED
    output: EnforcementLevel = EnforcementLevel.ENFORCED
    process_cleanup: EnforcementLevel = EnforcementLevel.ENFORCED
    cpu: EnforcementLevel = EnforcementLevel.UNSUPPORTED
    memory: EnforcementLevel = EnforcementLevel.UNSUPPORTED
    disk: EnforcementLevel = EnforcementLevel.UNSUPPORTED
    subprocess_count: EnforcementLevel = EnforcementLevel.UNSUPPORTED
    network_bytes: EnforcementLevel = EnforcementLevel.UNSUPPORTED

    def payload(self) -> dict[str, str]:
        return {
            "network": self.network.value,
            "filesystem": self.filesystem.value,
            "timeout": self.timeout.value,
            "output": self.output.value,
            "process_cleanup": self.process_cleanup.value,
            "cpu": self.cpu.value,
            "memory": self.memory.value,
            "disk": self.disk.value,
            "subprocess_count": self.subprocess_count.value,
            "network_bytes": self.network_bytes.value,
        }


NATIVE_ADVISORY_ENFORCEMENT = EnforcementAssessment()


@dataclass(frozen=True, slots=True)
class RequestedExecutionPolicy:
    network: NetworkAccess
    filesystem: FilesystemAccess
    credentials: tuple[CredentialCapability, ...] = ()
    resources: RequestedResourceLimits = field(default_factory=RequestedResourceLimits)
    enforcement_requirement: EnforcementRequirement = (
        EnforcementRequirement.ADVISORY_BACKEND_ALLOWED
    )

    def __post_init__(self) -> None:
        if self.network is NetworkAccess.HOST_INHERITED:
            raise ValueError("host_inherited is an effective backend fact, not a request")
        if self.filesystem is FilesystemAccess.HOST_ACCOUNT_ACCESS:
            raise ValueError("host_account_access is an effective backend fact, not a request")
        if len(set(self.credentials)) != len(self.credentials):
            raise ValueError("credentials must be unique")

    @property
    def policy_hash(self) -> str:
        return _stable_hash(
            {
                "network": self.network.value,
                "filesystem": self.filesystem.value,
                "credentials": sorted(item.value for item in self.credentials),
                "resources": self.resources.payload(),
                "enforcement_requirement": self.enforcement_requirement.value,
            }
        )


@dataclass(frozen=True, slots=True)
class EffectiveExecutionPolicy:
    network: NetworkAccess
    filesystem: FilesystemAccess
    credential_capabilities: tuple[CredentialCapability, ...] = ()
    resource_limits: EffectiveResourceLimits = field(default_factory=EffectiveResourceLimits)
    enforcement: EnforcementAssessment = field(default_factory=EnforcementAssessment)
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.credential_capabilities)) != len(self.credential_capabilities):
            raise ValueError("credential_capabilities must be unique")
        if len(self.degradation_reasons) > 20:
            raise ValueError("degradation_reasons accepts at most 20 entries")
        for reason in self.degradation_reasons:
            if not reason or len(reason) > 128:
                raise ValueError("degradation reasons must be bounded non-empty strings")
        if self.degraded != bool(self.degradation_reasons):
            raise ValueError("degraded must match whether degradation_reasons are present")

    @property
    def policy_hash(self) -> str:
        return _stable_hash(
            {
                "network": self.network.value,
                "filesystem": self.filesystem.value,
                "credential_capabilities": sorted(
                    item.value for item in self.credential_capabilities
                ),
                "resource_limits": self.resource_limits.payload(),
                "enforcement": self.enforcement.payload(),
                "degraded": self.degraded,
                "degradation_reasons": list(self.degradation_reasons),
            }
        )


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    kind: ExecutionScopeKind
    root: Path
    command_cwd: Path
    workspace_id: str | None
    working_directory_policy: str

    def __post_init__(self) -> None:
        root = self.root.resolve(strict=False)
        cwd = self.command_cwd.resolve(strict=False)
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise ValueError("command_cwd must remain inside execution root") from exc
        if self.kind is ExecutionScopeKind.WORKSPACE and not self.workspace_id:
            raise ValueError("workspace execution scope requires workspace_id")
        if self.kind is ExecutionScopeKind.SNAPSHOT_READ_ONLY and self.workspace_id is not None:
            raise ValueError("snapshot execution scope cannot carry a workspace_id")


@dataclass(frozen=True, slots=True)
class EnvironmentIdentityRequest:
    """Legacy reviewed inputs retained until every caller uses ExecutionRequest."""

    workspace_root: Path
    command_cwd: Path
    commands: tuple[tuple[str, ...], ...]
    working_directory_policy: str
    network_policy: NetworkPolicy = NetworkPolicy.EXTERNAL
    filesystem_capability: FilesystemCapability = FilesystemCapability.WORKSPACE_WRITE
    lockfiles: tuple[str, ...] = (
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
    )
    manifests: tuple[str, ...] = (
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
    )

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(command[0] for command in self.commands))


@dataclass(frozen=True, slots=True)
class ToolVersion:
    name: str
    version: str | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        if not _TOOL_PATTERN.fullmatch(self.name):
            raise ValueError(f"Invalid tool name: {self.name!r}")
        if self.version is not None and not _VERSION_PATTERN.fullmatch(self.version):
            raise ValueError(f"Invalid tool version: {self.version!r}")
        if self.digest is not None and not _SHA256.fullmatch(self.digest):
            raise ValueError(f"Invalid tool digest: {self.digest!r}")


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    """Secret-free environment identity bound to truthful effective policy."""

    schema_version: int = _ENVIRONMENT_IDENTITY_SCHEMA_VERSION
    adapter_kind: EnvironmentAdapterKind = EnvironmentAdapterKind.NATIVE_REVIEWED
    adapter_version: str = "2"
    platform: str = ""
    architecture: str = ""
    python_version: str = ""
    runtime_version: str = ""
    tools: tuple[ToolVersion, ...] = ()
    lockfile_digests: tuple[tuple[str, str], ...] = ()
    manifest_digests: tuple[tuple[str, str], ...] = ()
    approved_env_var_names: tuple[str, ...] = ()
    approved_env_value_hashes: tuple[tuple[str, str], ...] = ()
    requested_policy_hash: str = ""
    effective_policy_hash: str = ""
    effective_network: NetworkAccess = NetworkAccess.HOST_INHERITED
    effective_filesystem: FilesystemAccess = FilesystemAccess.HOST_ACCOUNT_ACCESS
    enforcement_assessment: EnforcementAssessment = field(default_factory=EnforcementAssessment)
    backend_capability_hash: str = ""
    working_directory_policy_hash: str = ""
    # Historical fields remain readable but are never enforcement evidence.
    network_policy: NetworkPolicy = NetworkPolicy.NONE
    filesystem_capability: FilesystemCapability = FilesystemCapability.READ

    def __post_init__(self) -> None:
        if self.schema_version not in {1, _ENVIRONMENT_IDENTITY_SCHEMA_VERSION}:
            raise ValueError(f"Unsupported schema version: {self.schema_version}")
        if not self.adapter_version:
            raise ValueError("adapter_version must be a non-empty string")
        for collection_name, values in (
            ("lockfile", self.lockfile_digests),
            ("manifest", self.manifest_digests),
        ):
            for name, digest in values:
                if not _NONEMPTY_NAME.fullmatch(name):
                    raise ValueError(f"Invalid {collection_name} name: {name!r}")
                if not _SHA256.fullmatch(digest):
                    raise ValueError(f"Invalid {collection_name} digest for {name!r}: {digest!r}")
        for name in self.approved_env_var_names:
            if not _NONEMPTY_NAME.fullmatch(name):
                raise ValueError(f"Invalid env var name: {name!r}")
        for name, digest in self.approved_env_value_hashes:
            if not _NONEMPTY_NAME.fullmatch(name) or not _SHA256.fullmatch(digest):
                raise ValueError(f"Invalid environment value hash for {name!r}")
        for name in (
            "requested_policy_hash",
            "effective_policy_hash",
            "backend_capability_hash",
            "working_directory_policy_hash",
        ):
            value = getattr(self, name)
            if value and not _SHA256.fullmatch(value):
                raise ValueError(f"Invalid {name}: {value!r}")

    @property
    def identity_hash(self) -> str:
        return _stable_hash(
            {
                "schema_version": self.schema_version,
                "adapter_kind": self.adapter_kind.value,
                "adapter_version": self.adapter_version,
                "platform": self.platform,
                "architecture": self.architecture,
                "python_version": self.python_version,
                "runtime_version": self.runtime_version,
                "tools": [
                    {"name": tool.name, "version": tool.version, "digest": tool.digest}
                    for tool in sorted(self.tools, key=lambda item: item.name)
                ],
                "lockfile_digests": sorted(self.lockfile_digests),
                "manifest_digests": sorted(self.manifest_digests),
                "approved_env_var_names": sorted(self.approved_env_var_names),
                "approved_env_value_hashes": sorted(self.approved_env_value_hashes),
                "requested_policy_hash": self.requested_policy_hash,
                "effective_policy_hash": self.effective_policy_hash,
                "effective_network": self.effective_network.value,
                "effective_filesystem": self.effective_filesystem.value,
                "enforcement_assessment": self.enforcement_assessment.payload(),
                "backend_capability_hash": self.backend_capability_hash,
                "working_directory_policy_hash": self.working_directory_policy_hash,
                "legacy_network_policy": self.network_policy.value,
                "legacy_filesystem_capability": self.filesystem_capability.value,
            }
        )

    @property
    def is_complete(self) -> bool:
        return bool(
            self.schema_version == _ENVIRONMENT_IDENTITY_SCHEMA_VERSION
            and self.platform
            and self.architecture
            and self.python_version
            and self.runtime_version
            and self.tools
            and not any(tool.version is None for tool in self.tools)
            and self.requested_policy_hash
            and self.effective_policy_hash
            and self.backend_capability_hash
            and self.working_directory_policy_hash
        )

    @property
    def cache_eligible(self) -> bool:
        """Compatibility view; new code must call assess_reuse_eligibility()."""
        return self.is_complete and self.effective_network is not NetworkAccess.HOST_INHERITED


class ReuseIneligibilityReason(str, Enum):
    IDENTITY_INCOMPLETE = "identity_incomplete"
    POLICY_BINDING_MISMATCH = "policy_binding_mismatch"
    ENFORCEMENT_REQUIRED = "enforcement_required"
    MUTATING_STAGE = "mutating_stage"
    FINAL_STAGE = "final_stage"
    UNSUPPORTED_DEGRADATION = "unsupported_degradation"


@dataclass(frozen=True, slots=True)
class ReuseEligibility:
    eligible: bool
    reasons: tuple[ReuseIneligibilityReason, ...] = ()


def assess_reuse_eligibility(
    identity: EnvironmentIdentity,
    *,
    requested: RequestedExecutionPolicy,
    effective: EffectiveExecutionPolicy,
    read_only: bool,
    final: bool,
) -> ReuseEligibility:
    reasons: list[ReuseIneligibilityReason] = []
    if not identity.is_complete:
        reasons.append(ReuseIneligibilityReason.IDENTITY_INCOMPLETE)
    if (
        identity.requested_policy_hash != requested.policy_hash
        or identity.effective_policy_hash != effective.policy_hash
    ):
        reasons.append(ReuseIneligibilityReason.POLICY_BINDING_MISMATCH)
    if requested.enforcement_requirement is EnforcementRequirement.ENFORCEMENT_REQUIRED:
        reasons.append(ReuseIneligibilityReason.ENFORCEMENT_REQUIRED)
    if not read_only:
        reasons.append(ReuseIneligibilityReason.MUTATING_STAGE)
    if final:
        reasons.append(ReuseIneligibilityReason.FINAL_STAGE)
    if set(effective.degradation_reasons) - {
        "network_not_isolated",
        "filesystem_not_isolated",
    }:
        reasons.append(ReuseIneligibilityReason.UNSUPPORTED_DEGRADATION)
    unique = tuple(dict.fromkeys(reasons))
    return ReuseEligibility(not unique, unique)


@dataclass(frozen=True, slots=True)
class EnforcementEvidence:
    network: str
    filesystem: str
    timeout: str
    output: str
    process_cleanup: str
    cpu: str
    memory: str
    disk: str
    subprocess_count: str
    network_bytes: str


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    adapter_kind: str
    identity_schema_version: int
    environment_identity_hash: str
    requested_policy_hash: str
    effective_policy_hash: str
    requested_network: str
    effective_network: str
    requested_filesystem: str
    effective_filesystem: str
    degraded: bool
    enforcement: EnforcementEvidence
    warnings: tuple[str, ...]


def build_execution_evidence(
    requested: RequestedExecutionPolicy,
    identity: EnvironmentIdentity,
    effective: EffectiveExecutionPolicy,
    warnings: Sequence[str] = (),
) -> ExecutionEvidence:
    bounded_warnings = tuple(str(item).strip()[:_MAX_WARNING_LENGTH] for item in warnings)[
        :_MAX_WARNINGS
    ]
    assessment = effective.enforcement
    return ExecutionEvidence(
        adapter_kind=identity.adapter_kind.value,
        identity_schema_version=identity.schema_version,
        environment_identity_hash=identity.identity_hash,
        requested_policy_hash=requested.policy_hash,
        effective_policy_hash=effective.policy_hash,
        requested_network=requested.network.value,
        effective_network=effective.network.value,
        requested_filesystem=requested.filesystem.value,
        effective_filesystem=effective.filesystem.value,
        degraded=effective.degraded,
        enforcement=EnforcementEvidence(
            network=assessment.network.value,
            filesystem=assessment.filesystem.value,
            timeout=assessment.timeout.value,
            output=assessment.output.value,
            process_cleanup=assessment.process_cleanup.value,
            cpu=assessment.cpu.value,
            memory=assessment.memory.value,
            disk=assessment.disk.value,
            subprocess_count=assessment.subprocess_count.value,
            network_bytes=assessment.network_bytes.value,
        ),
        warnings=bounded_warnings,
    )


def normalize_tool_name(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_")[:64]
