"""Pure environment-identity and capability model for execution environments."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

_ENVIRONMENT_IDENTITY_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_NONEMPTY_NAME = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_\-.]{0,127}$")
_TOOL_PATTERN = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_\-.]{0,63}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_][ -~]{0,127}$")


class NetworkPolicy(str, Enum):
    NONE = "none"
    RESTRICTED = "restricted"
    EXTERNAL = "external"


class FilesystemCapability(str, Enum):
    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    MANAGED_STATE_WRITE = "managed_state_write"


class EnvironmentAdapterKind(str, Enum):
    NATIVE_REVIEWED = "native_reviewed"
    DEV_CONTAINER = "dev_container"
    HERMETIC_CONTAINER = "hermetic_container"


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
    """Fingerprint of a native execution environment for cache and receipt binding.

    All fields are deterministic, secret-free, and safe for audit, diagnostics,
    and verification receipts.
    """

    schema_version: int = _ENVIRONMENT_IDENTITY_SCHEMA_VERSION
    adapter_kind: EnvironmentAdapterKind = EnvironmentAdapterKind.NATIVE_REVIEWED
    adapter_version: str = "1"
    platform: str = ""
    architecture: str = ""
    python_version: str = ""
    runtime_version: str = ""
    tools: tuple[ToolVersion, ...] = ()
    lockfile_digests: tuple[tuple[str, str], ...] = ()
    manifest_digests: tuple[tuple[str, str], ...] = ()
    approved_env_var_names: tuple[str, ...] = ()
    network_policy: NetworkPolicy = NetworkPolicy.NONE
    filesystem_capability: FilesystemCapability = FilesystemCapability.READ
    working_directory_policy_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != _ENVIRONMENT_IDENTITY_SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema version: {self.schema_version}")
        if not isinstance(self.adapter_kind, EnvironmentAdapterKind):
            raise ValueError("adapter_kind must be an EnvironmentAdapterKind")
        if not self.adapter_version or not isinstance(self.adapter_version, str):
            raise ValueError("adapter_version must be a non-empty string")
        for name, digest in self.lockfile_digests:
            if not _NONEMPTY_NAME.fullmatch(name):
                raise ValueError(f"Invalid lockfile name: {name!r}")
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"Invalid lockfile digest for {name!r}: {digest!r}")
        for name, digest in self.manifest_digests:
            if not _NONEMPTY_NAME.fullmatch(name):
                raise ValueError(f"Invalid manifest name: {name!r}")
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"Invalid manifest digest for {name!r}: {digest!r}")
        for name in self.approved_env_var_names:
            if not _NONEMPTY_NAME.fullmatch(name):
                raise ValueError(f"Invalid env var name: {name!r}")
        if not isinstance(self.network_policy, NetworkPolicy):
            raise ValueError("network_policy must be a NetworkPolicy")
        if not isinstance(self.filesystem_capability, FilesystemCapability):
            raise ValueError("filesystem_capability must be a FilesystemCapability")
        if self.working_directory_policy_hash and not _SHA256.fullmatch(
            self.working_directory_policy_hash
        ):
            raise ValueError(f"Invalid working_directory_policy_hash: {self.working_directory_policy_hash!r}")

    @property
    def identity_hash(self) -> str:
        """Deterministic SHA-256 of all identity fields."""
        payload = {
            "schema_version": self.schema_version,
            "adapter_kind": self.adapter_kind.value,
            "adapter_version": self.adapter_version,
            "platform": self.platform,
            "architecture": self.architecture,
            "python_version": self.python_version,
            "runtime_version": self.runtime_version,
            "tools": sorted(
                [
                    {
                        "name": t.name,
                        "version": t.version,
                        "digest": t.digest,
                    }
                    for t in self.tools
                ],
                key=lambda x: x["name"],
            ),
            "lockfile_digests": sorted(self.lockfile_digests),
            "manifest_digests": sorted(self.manifest_digests),
            "approved_env_var_names": sorted(self.approved_env_var_names),
            "network_policy": self.network_policy.value,
            "filesystem_capability": self.filesystem_capability.value,
            "working_directory_policy_hash": self.working_directory_policy_hash,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def cache_eligible(self) -> bool:
        return bool(
            self.platform
            and self.architecture
            and self.python_version
            and self.runtime_version
            and self.tools
            and not any(t.version is None for t in self.tools)
        )


def normalize_tool_name(raw: str) -> str:
    """Normalize an executable name for the identity model."""
    return raw.strip().lower().replace(" ", "_")[:64]
