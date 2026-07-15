from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from repoforge.domain.execution_environment import (
    EnvironmentAdapterKind,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
    NetworkPolicy,
    ToolVersion,
)


def test_identity_hash_is_order_independent() -> None:
    first = EnvironmentIdentity(
        platform="linux",
        architecture="arm64",
        python_version="3.13",
        runtime_version="python/3.13",
        tools=(ToolVersion("git", "2.50"), ToolVersion("python", "3.13")),
        approved_env_value_hashes=(("PATH", "a" * 64), ("LANG", "b" * 64)),
        working_directory_policy_hash="c" * 64,
    )
    second = EnvironmentIdentity(
        platform="linux",
        architecture="arm64",
        python_version="3.13",
        runtime_version="python/3.13",
        tools=(ToolVersion("python", "3.13"), ToolVersion("git", "2.50")),
        approved_env_value_hashes=(("LANG", "b" * 64), ("PATH", "a" * 64)),
        working_directory_policy_hash="c" * 64,
    )

    assert first.identity_hash == second.identity_hash


def test_identity_changes_with_policy_and_environment() -> None:
    common = {
        "platform": "linux",
        "architecture": "arm64",
        "python_version": "3.13",
        "runtime_version": "python/3.13",
        "tools": (ToolVersion("python", "3.13"),),
    }
    first = EnvironmentIdentity(
        **common,
        approved_env_value_hashes=(("PATH", "a" * 64),),
        working_directory_policy_hash="b" * 64,
    )
    second = EnvironmentIdentity(
        **common,
        approved_env_value_hashes=(("PATH", "c" * 64),),
        working_directory_policy_hash="d" * 64,
    )

    assert first.identity_hash != second.identity_hash


def test_partial_tool_identity_is_not_cache_eligible() -> None:
    identity = EnvironmentIdentity(
        platform="linux",
        architecture="arm64",
        python_version="3.13",
        runtime_version="python/3.13",
        tools=(ToolVersion("missing"),),
    )

    assert identity.cache_eligible is False


def test_external_network_identity_is_not_cache_eligible() -> None:
    identity = EnvironmentIdentity(
        platform="linux",
        architecture="arm64",
        python_version="3.13",
        runtime_version="python/3.13",
        tools=(ToolVersion("python", "3.13"),),
        network_policy=NetworkPolicy.EXTERNAL,
    )

    assert identity.cache_eligible is False


def test_request_deduplicates_profile_tools() -> None:
    request = EnvironmentIdentityRequest(
        workspace_root=Path("/workspace"),
        command_cwd=Path("/workspace"),
        commands=(("python", "-m", "pytest"), ("python", "-m", "mypy"), ("git", "status")),
        working_directory_policy=".",
    )

    assert request.tools == ("python", "git")


def test_invalid_environment_hash_is_rejected() -> None:
    with pytest.raises(ValueError, match="environment value hash"):
        EnvironmentIdentity(approved_env_value_hashes=(("PATH", "invalid"),))


def test_identity_contains_no_environment_bodies_or_paths() -> None:
    secret_path = "/Users/operator/private/bin"
    identity = EnvironmentIdentity(
        adapter_kind=EnvironmentAdapterKind.NATIVE_REVIEWED,
        approved_env_var_names=("PATH",),
        approved_env_value_hashes=(("PATH", hashlib.sha256(secret_path.encode()).hexdigest()),),
    )

    rendered = repr(identity)
    assert secret_path not in rendered
    assert "/Users/" not in rendered
