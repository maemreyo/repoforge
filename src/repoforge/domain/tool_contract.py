"""Reviewed MCP tool-contract versions and compatibility policy."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from .client_capabilities import ClientCapabilities

_CONTRACT_FLAG_PREFIX = "repoforge-tool-contract-v"
_CONTRACT_FLAG = re.compile(r"^repoforge-tool-contract-v([1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class ContractResolution:
    """Deterministic contract choice for one connection."""

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
class ToolAlias:
    """Reviewed compatibility alias and its bounded deprecation window."""

    alias: str
    canonical: str
    deprecated_in: int
    removed_in: int
    notice: str
    promoted_in: int | None = None

    def active_in(self, version: int) -> bool:
        return self.deprecated_in <= version < self.removed_in

    def as_dict(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "canonical": self.canonical,
            "deprecated_in": self.deprecated_in,
            "removed_in": self.removed_in,
            "notice": self.notice,
            "promoted_in": self.promoted_in,
        }


@dataclass(frozen=True, slots=True)
class ToolContractRegistry:
    """Pure registry for supported MCP tool contracts and migration gates."""

    current_version: int
    supported_versions: tuple[int, ...]
    aliases: tuple[ToolAlias, ...]

    def __post_init__(self) -> None:
        if not self.supported_versions:
            raise ValueError("Tool contract versions cannot be empty")
        expected = tuple(range(self.supported_versions[0], self.supported_versions[-1] + 1))
        if self.supported_versions != expected:
            raise ValueError("Tool contract versions must be positive, sorted, and contiguous")
        if self.supported_versions[0] < 1:
            raise ValueError("Tool contract versions must be positive, sorted, and contiguous")
        if self.current_version not in self.supported_versions:
            raise ValueError("Current tool contract version must be supported")

        seen_aliases: set[str] = set()
        for alias in self.aliases:
            if not alias.alias or not alias.canonical or alias.alias == alias.canonical:
                raise ValueError("Tool aliases require distinct non-empty names")
            if alias.alias in seen_aliases:
                raise ValueError(f"Duplicate tool alias: {alias.alias}")
            seen_aliases.add(alias.alias)
            if alias.deprecated_in not in self.supported_versions:
                raise ValueError(f"Alias {alias.alias!r} has an unsupported deprecation version")
            if alias.removed_in not in self.supported_versions:
                raise ValueError(f"Alias {alias.alias!r} has an unsupported removal version")
            if alias.deprecated_in >= alias.removed_in:
                raise ValueError(
                    f"Alias {alias.alias!r} requires a reviewed deprecation window before removal"
                )
            if not alias.notice.strip():
                raise ValueError(f"Alias {alias.alias!r} requires a deprecation notice")
            if alias.promoted_in is not None:
                if alias.promoted_in not in self.supported_versions:
                    raise ValueError(f"Alias {alias.alias!r} has an unsupported promotion version")
                if alias.promoted_in < alias.removed_in:
                    raise ValueError(
                        f"Alias {alias.alias!r} cannot be promoted before alias removal"
                    )

    def resolve(self, capabilities: ClientCapabilities) -> ContractResolution:
        """Resolve one supported contract without treating client claims as authority."""

        matching_flags = [
            flag
            for flag in capabilities.compatibility_flags
            if flag.startswith(_CONTRACT_FLAG_PREFIX)
        ]
        parsed_versions = {
            int(match.group(1))
            for flag in matching_flags
            if (match := _CONTRACT_FLAG.fullmatch(flag)) is not None
        }
        malformed = any(_CONTRACT_FLAG.fullmatch(flag) is None for flag in matching_flags)

        if malformed:
            return ContractResolution(self.current_version, None, "malformed_requested_version")
        if len(parsed_versions) > 1:
            return ContractResolution(self.current_version, None, "conflicting_requested_versions")
        if parsed_versions:
            requested = next(iter(parsed_versions))
            if requested in self.supported_versions:
                return ContractResolution(requested, requested, "requested_supported_version")
            return ContractResolution(self.current_version, requested, "unknown_requested_version")
        if capabilities.legacy:
            return ContractResolution(
                self.supported_versions[0],
                None,
                "legacy_fallback",
            )
        return ContractResolution(self.current_version, None, "current_default")

    def tool_names(
        self,
        version: int,
        registered_names: Collection[str],
    ) -> frozenset[str]:
        """Return the registered names visible in one reviewed contract version."""

        if version not in self.supported_versions:
            raise ValueError(f"Unsupported tool contract version: {version}")
        visible = set(registered_names)
        for alias in self.aliases:
            if version >= alias.removed_in and (
                alias.promoted_in is None or version < alias.promoted_in
            ):
                visible.discard(alias.alias)
        return frozenset(visible)

    def validate_alias_annotations(
        self,
        version: int,
        annotation_fingerprints: Mapping[str, str],
    ) -> None:
        """Reject a compatibility alias that weakens or changes tool annotations."""

        if version not in self.supported_versions:
            raise ValueError(f"Unsupported tool contract version: {version}")
        for alias in self.aliases:
            if version >= alias.removed_in:
                continue
            alias_fingerprint = annotation_fingerprints.get(alias.alias)
            canonical_fingerprint = annotation_fingerprints.get(alias.canonical)
            if alias_fingerprint is None or canonical_fingerprint is None:
                raise ValueError(
                    f"Missing annotation evidence for alias {alias.alias!r} or "
                    f"canonical tool {alias.canonical!r}"
                )
            if alias_fingerprint != canonical_fingerprint:
                raise ValueError(
                    f"Tool alias annotation drift: {alias.alias!r} must match {alias.canonical!r}"
                )


def default_tool_contract_registry() -> ToolContractRegistry:
    """Return the reviewed RepoForge MCP tool-contract registry."""

    return ToolContractRegistry(
        current_version=2,
        supported_versions=(1, 2),
        aliases=(
            ToolAlias(
                alias="workspace_verify",
                canonical="workspace_run_profile",
                deprecated_in=1,
                removed_in=2,
                notice=(
                    "Deprecated compatibility alias; migrate to workspace_run_profile before "
                    "tool contract v2."
                ),
                promoted_in=2,
            ),
        ),
    )
