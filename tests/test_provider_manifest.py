from __future__ import annotations

import hashlib

import pytest

from repoforge.adapters.codegraph.config import CodeGraphOptions
from repoforge.domain.provider_manifest import (
    ConfidenceModel,
    CoverageModel,
    ProviderAvailability,
    ProviderAvailabilityStatus,
    ProviderExecutableIdentity,
    ProviderFilesystemRequirement,
    ProviderImageIdentity,
    ProviderKind,
    ProviderManifest,
    ProviderOutputBounds,
)


def _executable_manifest(
    *,
    provider_id: str = "python-analyzer",
    runtime: ProviderExecutableIdentity | None = None,
    supported_languages: tuple[str, ...] = ("python",),
    supported_capabilities: tuple[str, ...] = ("lint", "security"),
    health_probe_arguments: tuple[str, ...] = (),
    coverage_model: CoverageModel = CoverageModel.NONE,
    confidence_model: ConfidenceModel = ConfidenceModel.NONE,
    network_policy: str = "none",
    filesystem: ProviderFilesystemRequirement | None = None,
    output_bounds: ProviderOutputBounds | None = None,
    fallback_provider_id: str = "",
    codegraph: CodeGraphOptions | None = None,
) -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id,
        kind=ProviderKind.ANALYZER,
        version="2.4.1",
        runtime=runtime or ProviderExecutableIdentity("python3", "a" * 64),
        supported_languages=supported_languages,
        supported_capabilities=supported_capabilities,
        health_probe_arguments=health_probe_arguments,
        coverage_model=coverage_model,
        confidence_model=confidence_model,
        network_policy=network_policy,
        filesystem=filesystem or ProviderFilesystemRequirement(),
        output_bounds=output_bounds or ProviderOutputBounds(),
        fallback_provider_id=fallback_provider_id,
        codegraph=codegraph,
    )


def test_manifest_accepts_digest_pinned_executable() -> None:
    manifest = _executable_manifest()

    assert isinstance(manifest.runtime, ProviderExecutableIdentity)
    assert manifest.runtime.executable == "python3"
    assert len(manifest.manifest_hash) == 64


def test_manifest_accepts_digest_pinned_image() -> None:
    manifest = ProviderManifest(
        provider_id="container-analyzer",
        kind=ProviderKind.ANALYZER,
        version="1.0.0",
        runtime=ProviderImageIdentity(
            image="registry.example.invalid/analyzer",
            sha256="b" * 64,
        ),
    )

    assert isinstance(manifest.runtime, ProviderImageIdentity)
    assert manifest.runtime.image == "registry.example.invalid/analyzer"


@pytest.mark.parametrize("digest", ["", "ABC", "a" * 63, "g" * 64])
def test_runtime_identity_rejects_invalid_digest(digest: str) -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _ = ProviderExecutableIdentity(executable="python3", sha256=digest)


def test_manifest_rejects_invalid_provider_id() -> None:
    with pytest.raises(ValueError, match="provider_id"):
        _ = _executable_manifest(provider_id="Unsafe Provider")


def test_availability_rejects_invalid_provider_id() -> None:
    with pytest.raises(ValueError, match="provider_id"):
        _ = ProviderAvailability(
            provider_id="Unsafe Provider",
            status=ProviderAvailabilityStatus.UNAVAILABLE,
            message="Provider is not registered",
        )


def test_manifest_rejects_duplicate_capabilities() -> None:
    with pytest.raises(ValueError, match="supported_capabilities"):
        _ = _executable_manifest(supported_capabilities=("lint", "lint"))


def test_manifest_hash_is_order_independent_for_sets() -> None:
    first = _executable_manifest(
        supported_languages=("python", "go"),
        supported_capabilities=("lint", "security"),
    )
    second = _executable_manifest(
        supported_languages=("go", "python"),
        supported_capabilities=("security", "lint"),
    )

    assert first.manifest_hash == second.manifest_hash


def test_manifest_hash_covers_runtime_digest() -> None:
    first = _executable_manifest()
    second = _executable_manifest(
        runtime=ProviderExecutableIdentity("python3", hashlib.sha256(b"other").hexdigest())
    )

    assert first.manifest_hash != second.manifest_hash


def test_manifest_reports_capability_and_major_version_compatibility() -> None:
    manifest = _executable_manifest()

    assert manifest.supports(("lint",))
    assert not manifest.supports(("format",))
    assert manifest.is_compatible_with("2.0.0")
    assert not manifest.is_compatible_with("3.0.0")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_stdout_chars", 0),
        ("max_stderr_chars", -1),
        ("max_artifact_bytes", True),
    ],
)
def test_output_bounds_require_positive_integers(field: str, value: int | bool) -> None:
    values: dict[str, int] = {
        "max_stdout_chars": 100,
        "max_stderr_chars": 100,
        "max_artifact_bytes": 100,
    }
    values[field] = value

    with pytest.raises(ValueError, match="positive integer"):
        _ = ProviderOutputBounds(
            max_stdout_chars=values["max_stdout_chars"],
            max_stderr_chars=values["max_stderr_chars"],
            max_artifact_bytes=values["max_artifact_bytes"],
        )


def test_full_manifest_preserves_advisory_contracts() -> None:
    manifest = _executable_manifest(
        health_probe_arguments=("--version",),
        coverage_model=CoverageModel.STATEMENT,
        confidence_model=ConfidenceModel.STATIC,
        network_policy="restricted",
        filesystem=ProviderFilesystemRequirement(
            capability="workspace_write",
            allowed_paths=("reports/**",),
        ),
        output_bounds=ProviderOutputBounds(50_000, 5_000, 5_000_000),
        fallback_provider_id="fallback-analyzer",
    )

    assert manifest.health_probe_arguments == ("--version",)
    assert manifest.fallback_provider_id == "fallback-analyzer"


def test_codegraph_options_have_stable_defaults_and_digest() -> None:
    first = CodeGraphOptions()
    second = CodeGraphOptions()

    assert first.init_timeout_seconds == 120
    assert first.sync_timeout_seconds == 60
    assert first.query_timeout_seconds == 15
    assert first.max_changed_paths == 256
    assert first.max_relationships == 1_000
    assert first.max_affected_paths == 1_000
    assert first.max_depth == 5
    assert first.projection_max_files == 20_000
    assert first.projection_max_bytes == 250_000_000
    assert first.canary_timeout_seconds == 180
    assert first.options_digest == second.options_digest
    assert len(first.options_digest) == 64


def test_manifest_accepts_valid_codegraph_enrollment_and_hashes_options() -> None:
    base_options = CodeGraphOptions()
    tuned_options = CodeGraphOptions(query_timeout_seconds=30)
    first = _executable_manifest(
        supported_capabilities=("semantic_graph",),
        filesystem=ProviderFilesystemRequirement(capability="managed_state_write"),
        codegraph=base_options,
    )
    second = _executable_manifest(
        supported_capabilities=("semantic_graph",),
        filesystem=ProviderFilesystemRequirement(capability="managed_state_write"),
        codegraph=tuned_options,
    )

    assert first.codegraph is base_options
    assert first.manifest_hash != second.manifest_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": ProviderKind.CODE_INTELLIGENCE},
        {"runtime": ProviderImageIdentity("example.invalid/codegraph", "b" * 64)},
        {"supported_capabilities": ("lint",)},
        {"network_policy": "restricted"},
        {"filesystem": ProviderFilesystemRequirement(capability="read")},
        {"fallback_provider_id": "fallback-analyzer"},
    ],
)
def test_manifest_rejects_incompatible_codegraph_enrollment(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "provider_id": "codegraph",
        "kind": ProviderKind.ANALYZER,
        "version": "1.5.0",
        "runtime": ProviderExecutableIdentity("/opt/codegraph", "a" * 64),
        "supported_capabilities": ("semantic_graph",),
        "network_policy": "none",
        "filesystem": ProviderFilesystemRequirement(capability="managed_state_write"),
        "codegraph": CodeGraphOptions(),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="CodeGraph"):
        ProviderManifest(**values)  # type: ignore[arg-type]
