"""Forge v2-only MCP tool-contract policy."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from .client_capabilities import ClientCapabilities


@dataclass(frozen=True, slots=True)
class ContractResolution:
    """Fixed contract resolution retained for runtime health reporting."""

    version: int
    requested_version: int | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "requested_version": self.requested_version,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ToolContractRegistry:
    """Static Forge v2 registry with no aliases or per-client negotiation."""

    current_version: int = 2
    supported_versions: tuple[int, ...] = (2,)
    aliases: tuple[()] = ()

    def __post_init__(self) -> None:
        if self.current_version != 2 or self.supported_versions != (2,) or self.aliases:
            raise ValueError("Forge v2 contract is fixed at version 2 with no aliases")

    def resolve(self, capabilities: ClientCapabilities) -> ContractResolution:
        del capabilities
        return ContractResolution(2, None, "forge_v2_identity")

    def tool_names(
        self,
        version: int,
        registered_names: Collection[str],
    ) -> frozenset[str]:
        if version != 2:
            raise ValueError(f"Unsupported Forge v2 contract version: {version}")
        return frozenset(registered_names)

    def validate_alias_annotations(
        self,
        version: int,
        annotation_fingerprints: Mapping[str, str],
    ) -> None:
        del annotation_fingerprints
        if version != 2:
            raise ValueError(f"Unsupported Forge v2 contract version: {version}")


def default_tool_contract_registry() -> ToolContractRegistry:
    return ToolContractRegistry()


__all__ = [
    "ContractResolution",
    "ToolContractRegistry",
    "default_tool_contract_registry",
]
