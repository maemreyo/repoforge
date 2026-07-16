from __future__ import annotations

import pytest

from repoforge.domain.client_capabilities import ClientCapabilities, parse_client_capabilities
from repoforge.domain.tool_contract import (
    ToolAlias,
    ToolContractRegistry,
    default_tool_contract_registry,
)

_REGISTERED = frozenset({"repo_list", "workspace_run_profile", "workspace_verify"})


def _capabilities(*flags: str, legacy: bool = False) -> ClientCapabilities:
    if legacy:
        return parse_client_capabilities(None)
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


def test_default_registry_keeps_verify_alias_only_in_contract_v1() -> None:
    registry = default_tool_contract_registry()

    assert registry.current_version == 2
    assert registry.supported_versions == (1, 2)
    assert registry.tool_names(1, _REGISTERED) == _REGISTERED
    assert registry.tool_names(2, _REGISTERED) == frozenset({"repo_list", "workspace_run_profile"})


def test_normal_client_defaults_to_current_contract() -> None:
    resolution = default_tool_contract_registry().resolve(_capabilities())

    assert resolution.version == 2
    assert resolution.requested_version is None
    assert resolution.reason == "current_default"


def test_legacy_or_missing_client_falls_back_to_oldest_supported_contract() -> None:
    resolution = default_tool_contract_registry().resolve(_capabilities(legacy=True))

    assert resolution.version == 1
    assert resolution.requested_version is None
    assert resolution.reason == "legacy_fallback"


def test_supported_explicit_contract_request_is_honored() -> None:
    resolution = default_tool_contract_registry().resolve(
        _capabilities("repoforge-tool-contract-v1")
    )

    assert resolution.version == 1
    assert resolution.requested_version == 1
    assert resolution.reason == "requested_supported_version"


def test_unknown_requested_version_falls_back_to_current() -> None:
    resolution = default_tool_contract_registry().resolve(
        _capabilities("repoforge-tool-contract-v99")
    )

    assert resolution.version == 2
    assert resolution.requested_version == 99
    assert resolution.reason == "unknown_requested_version"


def test_malformed_and_conflicting_requests_fall_back_to_current() -> None:
    registry = default_tool_contract_registry()

    malformed = registry.resolve(_capabilities("repoforge-tool-contract-vnext"))
    conflicting = registry.resolve(
        _capabilities("repoforge-tool-contract-v1", "repoforge-tool-contract-v2")
    )

    assert malformed.version == 2
    assert malformed.requested_version is None
    assert malformed.reason == "malformed_requested_version"
    assert conflicting.version == 2
    assert conflicting.requested_version is None
    assert conflicting.reason == "conflicting_requested_versions"


def test_alias_metadata_and_annotation_parity_are_explicit() -> None:
    registry = default_tool_contract_registry()
    alias = registry.aliases[0]

    assert alias.as_dict() == {
        "alias": "workspace_verify",
        "canonical": "workspace_run_profile",
        "deprecated_in": 1,
        "removed_in": 2,
        "notice": (
            "Deprecated compatibility alias; migrate to workspace_run_profile before "
            "tool contract v2."
        ),
    }
    assert alias.active_in(1) is True
    assert alias.active_in(2) is False
    registry.validate_alias_annotations(
        1,
        {
            "workspace_verify": "local-mutate",
            "workspace_run_profile": "local-mutate",
        },
    )
    with pytest.raises(ValueError, match="annotation drift"):
        registry.validate_alias_annotations(
            1,
            {
                "workspace_verify": "read-only",
                "workspace_run_profile": "local-mutate",
            },
        )


def test_removal_without_a_reviewed_deprecation_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="deprecation window"):
        ToolContractRegistry(
            current_version=2,
            supported_versions=(1, 2),
            aliases=(
                ToolAlias(
                    alias="old_tool",
                    canonical="new_tool",
                    deprecated_in=2,
                    removed_in=2,
                    notice="Deprecated; migrate to new_tool.",
                ),
            ),
        )


def test_non_contiguous_versions_and_unknown_version_queries_fail_closed() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        ToolContractRegistry(
            current_version=3,
            supported_versions=(1, 3),
            aliases=(),
        )

    registry = default_tool_contract_registry()
    with pytest.raises(ValueError, match="Unsupported tool contract version"):
        registry.tool_names(99, _REGISTERED)
