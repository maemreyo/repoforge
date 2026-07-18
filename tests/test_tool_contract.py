from __future__ import annotations

import pytest

from repoforge.contracts.registry import V2_TOOL_NAMES
from repoforge.domain.client_capabilities import parse_client_capabilities
from repoforge.domain.tool_contract import (
    ToolContractRegistry,
    default_tool_contract_registry,
)


def _capabilities(*flags: str):
    return parse_client_capabilities(
        {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "contract-test", "version": "1"},
            "capabilities": {
                "experimental": {
                    "repoforge": {"compatibilityFlags": list(flags)},
                }
            },
        }
    )


def test_default_registry_is_fixed_to_v2_without_aliases() -> None:
    registry = default_tool_contract_registry()

    assert registry.current_version == 2
    assert registry.supported_versions == (2,)
    assert registry.aliases == ()
    assert registry.tool_names(2, V2_TOOL_NAMES) == frozenset(V2_TOOL_NAMES)


def test_client_flags_do_not_negotiate_the_forge_v2_identity() -> None:
    registry = default_tool_contract_registry()

    for capabilities in (
        _capabilities(),
        _capabilities("repoforge-tool-contract-v1"),
        _capabilities("repoforge-tool-contract-v99"),
        parse_client_capabilities(None),
    ):
        resolution = registry.resolve(capabilities)
        assert resolution.version == 2
        assert resolution.requested_version is None
        assert resolution.reason == "forge_v2_identity"


def test_registry_rejects_any_non_v2_shape() -> None:
    with pytest.raises(ValueError, match="fixed at version 2"):
        ToolContractRegistry(current_version=1, supported_versions=(1,))
    with pytest.raises(ValueError, match="fixed at version 2"):
        ToolContractRegistry(current_version=2, supported_versions=(1, 2))


def test_unknown_version_queries_fail_closed() -> None:
    registry = default_tool_contract_registry()

    with pytest.raises(ValueError, match="Unsupported Forge v2 contract version"):
        registry.tool_names(1, V2_TOOL_NAMES)
    with pytest.raises(ValueError, match="Unsupported Forge v2 contract version"):
        registry.validate_alias_annotations(99, {})


def test_resolution_serializes_stable_identity_reason() -> None:
    resolution = default_tool_contract_registry().resolve(_capabilities())

    assert resolution.as_dict() == {
        "version": 2,
        "requested_version": None,
        "reason": "forge_v2_identity",
    }
