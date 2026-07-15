"""Provider identity and advisory capability contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

_MANIFEST_SCHEMA_VERSION = 1
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9_][ -~]{0,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.+-]{0,63}$")


class ProviderKind(str, Enum):
    CODE_INTELLIGENCE = "code_intelligence"
    ANALYZER = "analyzer"
    POLICY = "policy"
    EXECUTION = "execution"


class ProviderAvailabilityStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"


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


def _validate_digest(value: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError("Provider runtime digest must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ProviderExecutableIdentity:
    executable: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.executable or "\x00" in self.executable:
            raise ValueError("Provider executable must be non-empty")
        _validate_digest(self.sha256)


@dataclass(frozen=True, slots=True)
class ProviderImageIdentity:
    image: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.image or "\x00" in self.image:
            raise ValueError("Provider image must be non-empty")
        _validate_digest(self.sha256)


ProviderRuntimeIdentity = ProviderExecutableIdentity | ProviderImageIdentity


@dataclass(frozen=True, slots=True)
class ProviderOutputBounds:
    max_stdout_chars: int = 100_000
    max_stderr_chars: int = 10_000
    max_artifact_bytes: int = 10_000_000

    def __post_init__(self) -> None:
        values = (
            (self.max_stdout_chars, "max_stdout_chars"),
            (self.max_stderr_chars, "max_stderr_chars"),
            (self.max_artifact_bytes, "max_artifact_bytes"),
        )
        for value, name in values:
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProviderFilesystemRequirement:
    capability: str = "read"
    allowed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.capability not in {"none", "read", "workspace_write", "managed_state_write"}:
            raise ValueError(f"Invalid filesystem capability: {self.capability!r}")
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise ValueError("Provider filesystem allowed_paths must be unique")
        if any(not path or "\x00" in path for path in self.allowed_paths):
            raise ValueError("Provider filesystem allowed_paths must contain non-empty paths")


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    """Reviewed, secret-free identity and advisory capability declaration."""

    provider_id: str
    kind: ProviderKind
    version: str
    runtime: ProviderRuntimeIdentity
    supported_languages: tuple[str, ...] = ()
    supported_capabilities: tuple[str, ...] = ()
    health_probe_arguments: tuple[str, ...] = ()
    coverage_model: CoverageModel = CoverageModel.NONE
    confidence_model: ConfidenceModel = ConfidenceModel.NONE
    network_policy: str = "none"
    filesystem: ProviderFilesystemRequirement = ProviderFilesystemRequirement()
    output_bounds: ProviderOutputBounds = ProviderOutputBounds()
    fallback_provider_id: str = ""
    schema_version: int = _MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"Unsupported manifest schema version: {self.schema_version}")
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError(f"Invalid provider_id: {self.provider_id!r}")
        if not self.version or not _VERSION.fullmatch(self.version):
            raise ValueError(f"Invalid version: {self.version!r}")
        self._validate_names(self.supported_languages, "supported_languages")
        self._validate_names(self.supported_capabilities, "supported_capabilities")
        if any(not argument or "\x00" in argument for argument in self.health_probe_arguments):
            raise ValueError("health_probe_arguments must contain non-empty strings")
        if self.network_policy not in {"none", "restricted", "external"}:
            raise ValueError(f"Invalid network_policy: {self.network_policy!r}")
        if self.fallback_provider_id and not _PROVIDER_ID.fullmatch(self.fallback_provider_id):
            raise ValueError(f"Invalid fallback_provider_id: {self.fallback_provider_id!r}")

    @staticmethod
    def _validate_names(values: tuple[str, ...], field: str) -> None:
        if len(set(values)) != len(values) or any(not _NAME.fullmatch(value) for value in values):
            raise ValueError(f"{field} must contain unique normalized names")

    @property
    def manifest_hash(self) -> str:
        runtime = (
            {"type": "executable", "value": self.runtime.executable, "sha256": self.runtime.sha256}
            if isinstance(self.runtime, ProviderExecutableIdentity)
            else {"type": "image", "value": self.runtime.image, "sha256": self.runtime.sha256}
        )
        payload = {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "kind": self.kind.value,
            "version": self.version,
            "runtime": runtime,
            "supported_languages": sorted(self.supported_languages),
            "supported_capabilities": sorted(self.supported_capabilities),
            "health_probe_arguments": self.health_probe_arguments,
            "coverage_model": self.coverage_model.value,
            "confidence_model": self.confidence_model.value,
            "network_policy": self.network_policy,
            "filesystem": {
                "capability": self.filesystem.capability,
                "allowed_paths": sorted(self.filesystem.allowed_paths),
            },
            "output_bounds": {
                "max_stdout_chars": self.output_bounds.max_stdout_chars,
                "max_stderr_chars": self.output_bounds.max_stderr_chars,
                "max_artifact_bytes": self.output_bounds.max_artifact_bytes,
            },
            "fallback_provider_id": self.fallback_provider_id,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def supports(self, required_capabilities: tuple[str, ...]) -> bool:
        return set(required_capabilities).issubset(self.supported_capabilities)

    def is_compatible_with(self, requested_version: str) -> bool:
        if not requested_version or not _VERSION.fullmatch(requested_version):
            return False
        return self.version.split(".", 1)[0] == requested_version.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    provider_id: str
    status: ProviderAvailabilityStatus
    message: str
    resolved_executable: str | None = None

    def __post_init__(self) -> None:
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError(f"Invalid provider_id: {self.provider_id!r}")
