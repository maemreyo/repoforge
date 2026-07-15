"""Pure provider manifest and registry domain models."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

_MANIFEST_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_.\-]{1,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_VERSION_STRING = re.compile(r"^[A-Za-z0-9_][ -~]{0,127}$")


class ProviderKind(str, Enum):
    CODE_INTELLIGENCE = "code_intelligence"
    ANALYZER = "analyzer"
    POLICY = "policy"
    EXECUTION = "execution"


class ProviderHealthStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"


class CoverageModel(str, Enum):
    NONE = "none"
    LINE = "line"
    BRANCH = "branch"
    STATEMENT = "statement"
    FUNCTION = "function"


class ConfidenceModel(str, Enum):
    NONE = "none"
    STATIC = "static"
    DYNAMIC = "dynamic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class ProviderOutputBounds:
    max_stdout_chars: int = 100_000
    max_stderr_chars: int = 10_000
    max_artifact_bytes: int = 10_000_000

    def __post_init__(self) -> None:
        for field, name in (
            (self.max_stdout_chars, "max_stdout_chars"),
            (self.max_stderr_chars, "max_stderr_chars"),
            (self.max_artifact_bytes, "max_artifact_bytes"),
        ):
            if not isinstance(field, int) or isinstance(field, bool) or field <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProviderFilesystemRequirement:
    capability: str = "read"
    allowed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        valid = {"none", "read", "workspace_write", "managed_state_write"}
        if self.capability not in valid:
            raise ValueError(f"Invalid filesystem capability: {self.capability!r}")


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    """Typed manifest for an external provider (analyzer, intelligence, policy, execution).

    All fields are deterministic, secret-free, and safe for audit and diagnostics.
    """

    schema_version: int = _MANIFEST_SCHEMA_VERSION
    provider_id: str = ""
    kind: ProviderKind = ProviderKind.ANALYZER
    version: str = ""
    executable: str = ""
    executable_digest: str = ""
    supported_languages: tuple[str, ...] = ()
    supported_capabilities: tuple[str, ...] = ()
    health_probe_command: tuple[str, ...] = ()
    coverage_model: CoverageModel = CoverageModel.NONE
    confidence_model: ConfidenceModel = ConfidenceModel.NONE
    network_policy: str = "none"
    filesystem: ProviderFilesystemRequirement = ProviderFilesystemRequirement()
    output_bounds: ProviderOutputBounds = ProviderOutputBounds()
    fallback_provider_id: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != _MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"Unsupported manifest schema version: {self.schema_version}")
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError(f"Invalid provider_id: {self.provider_id!r}")
        if not isinstance(self.kind, ProviderKind):
            raise ValueError("kind must be a ProviderKind")
        if not self.version or not _VERSION_STRING.fullmatch(self.version):
            raise ValueError(f"Invalid version: {self.version!r}")
        if not self.executable:
            raise ValueError("executable must be non-empty")
        if self.executable_digest and not _DIGEST.fullmatch(self.executable_digest):
            raise ValueError(f"Invalid executable_digest: {self.executable_digest!r}")
        for lang in self.supported_languages:
            if not lang or not isinstance(lang, str):
                raise ValueError(f"Invalid language: {lang!r}")
        for cap in self.supported_capabilities:
            if not cap or not isinstance(cap, str):
                raise ValueError(f"Invalid capability: {cap!r}")
        for cmd in self.health_probe_command:
            if not cmd or not isinstance(cmd, str):
                raise ValueError(f"Invalid health probe command element: {cmd!r}")
        if not isinstance(self.coverage_model, CoverageModel):
            raise ValueError("coverage_model must be a CoverageModel")
        if not isinstance(self.confidence_model, ConfidenceModel):
            raise ValueError("confidence_model must be a ConfidenceModel")
        valid_network = {"none", "restricted", "external"}
        if self.network_policy not in valid_network:
            raise ValueError(f"Invalid network_policy: {self.network_policy!r}")
        if self.fallback_provider_id and not _PROVIDER_ID.fullmatch(self.fallback_provider_id):
            raise ValueError(f"Invalid fallback_provider_id: {self.fallback_provider_id!r}")

    @property
    def manifest_hash(self) -> str:
        """Deterministic SHA-256 of all manifest fields."""
        payload = {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "kind": self.kind.value,
            "version": self.version,
            "executable": self.executable,
            "executable_digest": self.executable_digest,
            "supported_languages": sorted(self.supported_languages),
            "supported_capabilities": sorted(self.supported_capabilities),
            "health_probe_command": tuple(self.health_probe_command),
            "coverage_model": self.coverage_model.value,
            "confidence_model": self.confidence_model.value,
            "network_policy": self.network_policy,
            "filesystem": {
                "capability": self.filesystem.capability,
                "allowed_paths": tuple(sorted(self.filesystem.allowed_paths)),
            },
            "output_bounds": {
                "max_stdout_chars": self.output_bounds.max_stdout_chars,
                "max_stderr_chars": self.output_bounds.max_stderr_chars,
                "max_artifact_bytes": self.output_bounds.max_artifact_bytes,
            },
            "fallback_provider_id": self.fallback_provider_id,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def health_check_enabled(self) -> bool:
        return len(self.health_probe_command) > 0

    @property
    def has_fallback(self) -> bool:
        return bool(self.fallback_provider_id)

    def supports(self, required_capabilities: tuple[str, ...]) -> bool:
        return set(required_capabilities).issubset(self.supported_capabilities)

    def is_compatible_with(self, requested_version: str) -> bool:
        if not requested_version or not _VERSION_STRING.fullmatch(requested_version):
            return False
        return self.version.split(".", 1)[0] == requested_version.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider_id: str
    status: ProviderHealthStatus
    message: str = ""
    checked_at: str = ""

    def __post_init__(self) -> None:
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError(f"Invalid provider_id: {self.provider_id!r}")
        if not isinstance(self.status, ProviderHealthStatus):
            raise ValueError("status must be a ProviderHealthStatus")
