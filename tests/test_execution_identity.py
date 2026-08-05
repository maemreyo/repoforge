from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from repoforge.domain.execution_environment import (
    EffectiveExecutionPolicy,
    EffectiveResourceLimits,
    EnforcementAssessment,
    EnforcementLevel,
    EnforcementRequirement,
    EnvironmentAdapterKind,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
    FilesystemAccess,
    NetworkAccess,
    NetworkPolicy,
    RequestedExecutionPolicy,
    RequestedResourceLimits,
    ReuseIneligibilityReason,
    ToolVersion,
    assess_reuse_eligibility,
    build_execution_evidence,
)


def requested_policy() -> RequestedExecutionPolicy:
    return RequestedExecutionPolicy(
        network=NetworkAccess.OFFLINE,
        filesystem=FilesystemAccess.SOURCE_READ,
        credentials=(),
        resources=RequestedResourceLimits(
            cpu_seconds=30,
            memory_bytes=512 * 1024 * 1024,
            disk_bytes=1024 * 1024 * 1024,
            subprocesses=8,
            network_bytes=0,
        ),
        enforcement_requirement=EnforcementRequirement.ADVISORY_BACKEND_ALLOWED,
    )


def effective_native_policy() -> EffectiveExecutionPolicy:
    return EffectiveExecutionPolicy(
        network=NetworkAccess.HOST_INHERITED,
        filesystem=FilesystemAccess.HOST_ACCOUNT_ACCESS,
        credential_capabilities=(),
        resource_limits=EffectiveResourceLimits(),
        enforcement=EnforcementAssessment(
            network=EnforcementLevel.ADVISORY,
            filesystem=EnforcementLevel.ADVISORY,
            timeout=EnforcementLevel.ENFORCED,
            output=EnforcementLevel.ENFORCED,
            process_cleanup=EnforcementLevel.ENFORCED,
            cpu=EnforcementLevel.UNSUPPORTED,
            memory=EnforcementLevel.UNSUPPORTED,
            disk=EnforcementLevel.UNSUPPORTED,
            subprocess_count=EnforcementLevel.UNSUPPORTED,
            network_bytes=EnforcementLevel.UNSUPPORTED,
        ),
        degraded=True,
        degradation_reasons=("network_not_isolated", "filesystem_not_isolated"),
    )


def complete_identity() -> EnvironmentIdentity:
    requested = requested_policy()
    effective = effective_native_policy()
    return EnvironmentIdentity(
        adapter_version="2",
        platform="linux",
        architecture="arm64",
        python_version="3.13",
        runtime_version="python/3.13",
        tools=(ToolVersion("python", "3.13"),),
        requested_policy_hash=requested.policy_hash,
        effective_policy_hash=effective.policy_hash,
        effective_network=effective.network,
        effective_filesystem=effective.filesystem,
        enforcement_assessment=effective.enforcement,
        backend_capability_hash="a" * 64,
        working_directory_policy_hash="b" * 64,
    )


def test_policy_hashes_are_stable_and_distinguish_effective_behavior() -> None:
    requested = requested_policy()
    effective = effective_native_policy()

    assert requested.policy_hash == requested_policy().policy_hash
    assert effective.policy_hash == effective_native_policy().policy_hash
    assert requested.policy_hash != effective.policy_hash


def test_requested_policy_rejects_effective_only_values() -> None:
    with pytest.raises(ValueError, match="host_inherited"):
        dataclasses.replace(requested_policy(), network=NetworkAccess.HOST_INHERITED)
    with pytest.raises(ValueError, match="host_account_access"):
        dataclasses.replace(requested_policy(), filesystem=FilesystemAccess.HOST_ACCOUNT_ACCESS)


def test_identity_v2_binds_effective_policy() -> None:
    identity = complete_identity()

    assert identity.schema_version == 2
    assert identity.is_complete is True
    assert len(identity.identity_hash) == 64


def test_reuse_eligibility_is_separate_from_identity_completeness() -> None:
    eligibility = assess_reuse_eligibility(
        complete_identity(),
        requested=requested_policy(),
        effective=effective_native_policy(),
        read_only=True,
        final=False,
    )

    assert eligibility.eligible is True
    assert eligibility.reasons == ()


def test_unknown_tool_version_is_incomplete_and_not_reusable() -> None:
    identity = dataclasses.replace(complete_identity(), tools=(ToolVersion("python"),))
    eligibility = assess_reuse_eligibility(
        identity,
        requested=requested_policy(),
        effective=effective_native_policy(),
        read_only=True,
        final=False,
    )

    assert identity.is_complete is False
    assert eligibility.eligible is False
    assert eligibility.reasons == (ReuseIneligibilityReason.IDENTITY_INCOMPLETE,)


def test_execution_evidence_is_bounded_and_truthful() -> None:
    evidence = build_execution_evidence(
        requested_policy(),
        complete_identity(),
        effective_native_policy(),
        warnings=("tool version unavailable",),
    )

    assert evidence.requested_network == "offline"
    assert evidence.effective_network == "host_inherited"
    assert evidence.enforcement.network == "advisory"
    assert evidence.warnings == ("tool version unavailable",)


def test_execution_evidence_carries_effective_path_and_tool_versions() -> None:
    """#380 AC2: tool versions and effective PATH must be observable through the
    evidence a caller already receives, not only inside the identity a caller does not
    normally see."""
    identity = dataclasses.replace(
        complete_identity(),
        effective_path="/usr/local/bin:/usr/bin:/bin",
        tools=(ToolVersion("python", "3.13"), ToolVersion("git", "2.50")),
    )

    evidence = build_execution_evidence(requested_policy(), identity, effective_native_policy())

    assert evidence.effective_path == "/usr/local/bin:/usr/bin:/bin"
    assert evidence.tool_versions == (ToolVersion("python", "3.13"), ToolVersion("git", "2.50"))


def test_effective_path_is_excluded_from_identity_repr() -> None:
    """effective_path must reach a caller only through the explicit ExecutionEvidence
    path (see test above), never leak incidentally through repr() the way
    test_identity_contains_no_environment_bodies_or_paths already guards for the hashed
    fields -- this is the same guarantee for the one field that is not hashed."""
    identity = dataclasses.replace(complete_identity(), effective_path="/Users/operator/bin")

    assert "/Users/operator/bin" not in repr(identity)


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


def test_identity_hash_distinguishes_adapter_kind() -> None:
    """#380 AC4: host and sandbox environments must have distinct, testable identities.
    No sandbox adapter exists yet (that is #384's scope), but the identity model itself
    must already be sandbox-ready: two identities differing only in adapter_kind must
    hash differently, so #384 can build a real DEV_CONTAINER/HERMETIC_CONTAINER adapter
    on top of a type that already distinguishes it from NATIVE_REVIEWED."""
    common = {
        "platform": "linux",
        "architecture": "arm64",
        "python_version": "3.13",
        "runtime_version": "python/3.13",
        "tools": (ToolVersion("python", "3.13"),),
        "working_directory_policy_hash": "b" * 64,
    }
    host = EnvironmentIdentity(adapter_kind=EnvironmentAdapterKind.NATIVE_REVIEWED, **common)
    dev_container = EnvironmentIdentity(adapter_kind=EnvironmentAdapterKind.DEV_CONTAINER, **common)
    hermetic = EnvironmentIdentity(adapter_kind=EnvironmentAdapterKind.HERMETIC_CONTAINER, **common)

    assert len({host.identity_hash, dev_container.identity_hash, hermetic.identity_hash}) == 3


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
