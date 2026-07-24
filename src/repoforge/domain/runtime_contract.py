"""Cryptographic identity for one active Forge runtime contract."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTITY_FIELDS = (
    "server_build_sha",
    "server_version",
    "active_generation",
    "tool_surface_hash",
    "input_contract_digest",
    "output_contract_digest",
    "runtime_protocol_version",
    "process_start_identity",
)


@dataclass(frozen=True, slots=True)
class RuntimeContractIdentity:
    """One redaction-safe identity chain advertised by an active process."""

    server_build_sha: str
    server_version: str
    active_generation: int
    tool_surface_hash: str
    input_contract_digest: str
    output_contract_digest: str
    runtime_protocol_version: int
    process_start_identity: str

    def __post_init__(self) -> None:
        for name in (
            "server_build_sha",
            "tool_surface_hash",
            "input_contract_digest",
            "output_contract_digest",
            "process_start_identity",
        ):
            if _SHA256.fullmatch(str(getattr(self, name))) is None:
                raise ValueError(f"Runtime contract {name} must be a lowercase SHA-256")
        if not self.server_version or len(self.server_version) > 160:
            raise ValueError("Runtime contract server_version is invalid")
        if self.active_generation <= 0:
            raise ValueError("Runtime contract active_generation must be positive")
        if self.runtime_protocol_version <= 0:
            raise ValueError("Runtime contract protocol version must be positive")

    def as_dict(self) -> dict[str, object]:
        """Return the bounded public representation; no host paths or argv are included."""

        return {
            "server_build_sha": self.server_build_sha,
            "server_version": self.server_version,
            "active_generation": self.active_generation,
            "tool_surface_hash": self.tool_surface_hash,
            "input_contract_digest": self.input_contract_digest,
            "output_contract_digest": self.output_contract_digest,
            "runtime_protocol_version": self.runtime_protocol_version,
            "process_start_identity": self.process_start_identity,
        }


def changed_contract_fields(
    expected: RuntimeContractIdentity,
    actual: RuntimeContractIdentity,
) -> tuple[str, ...]:
    """Return exact identity components that changed, in stable contract order."""

    return tuple(
        name for name in _IDENTITY_FIELDS if getattr(expected, name) != getattr(actual, name)
    )


__all__ = ["RuntimeContractIdentity", "changed_contract_fields"]
