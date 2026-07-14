"""Tests for provider manifest domain model, registry port, and config adapter."""

from __future__ import annotations

import hashlib

import pytest

from repoforge.domain.provider_manifest import (
    ConfidenceModel,
    CoverageModel,
    ProviderFilesystemRequirement,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderKind,
    ProviderManifest,
    ProviderOutputBounds,
)


class TestProviderOutputBounds:
    def test_defaults(self) -> None:
        bounds = ProviderOutputBounds()
        assert bounds.max_stdout_chars == 100_000
        assert bounds.max_stderr_chars == 10_000
        assert bounds.max_artifact_bytes == 10_000_000

    def test_invalid_stdout(self) -> None:
        with pytest.raises(ValueError, match="must be a positive integer"):
            ProviderOutputBounds(max_stdout_chars=0)

    def test_negative_stderr(self) -> None:
        with pytest.raises(ValueError, match="must be a positive integer"):
            ProviderOutputBounds(max_stderr_chars=-1)


class TestProviderFilesystemRequirement:
    def test_default(self) -> None:
        req = ProviderFilesystemRequirement()
        assert req.capability == "read"

    def test_invalid_capability(self) -> None:
        with pytest.raises(ValueError, match="Invalid filesystem capability"):
            ProviderFilesystemRequirement(capability="write_all")


class TestProviderManifest:
    def test_minimal_valid(self) -> None:
        manifest = ProviderManifest(
            provider_id="my-analyzer",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/usr/bin/my-analyzer",
        )
        assert manifest.provider_id == "my-analyzer"
        assert manifest.kind is ProviderKind.ANALYZER
        assert manifest.manifest_hash
        assert len(manifest.manifest_hash) == 64

    def test_invalid_schema_version(self) -> None:
        with pytest.raises(ValueError, match="Unsupported manifest schema version"):
            ProviderManifest(
                provider_id="test",
                kind=ProviderKind.ANALYZER,
                version="1.0.0",
                executable="/bin/test",
                schema_version=999,
            )

    def test_invalid_provider_id_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid provider_id"):
            ProviderManifest(
                provider_id="",
                kind=ProviderKind.ANALYZER,
                version="1.0.0",
                executable="/bin/test",
            )

    def test_invalid_provider_id_uppercase(self) -> None:
        with pytest.raises(ValueError, match="Invalid provider_id"):
            ProviderManifest(
                provider_id="My-Analyzer",
                kind=ProviderKind.ANALYZER,
                version="1.0.0",
                executable="/bin/test",
            )

    def test_invalid_kind(self) -> None:
        with pytest.raises(ValueError, match="kind must be a ProviderKind"):
            ProviderManifest(
                provider_id="test",
                kind="invalid",  # type: ignore[arg-type]
                version="1.0.0",
                executable="/bin/test",
            )

    def test_empty_executable(self) -> None:
        with pytest.raises(ValueError, match="executable must be non-empty"):
            ProviderManifest(
                provider_id="test",
                kind=ProviderKind.ANALYZER,
                version="1.0.0",
                executable="",
            )

    def test_invalid_digest(self) -> None:
        with pytest.raises(ValueError, match="Invalid executable_digest"):
            ProviderManifest(
                provider_id="test",
                kind=ProviderKind.ANALYZER,
                version="1.0.0",
                executable="/bin/test",
                executable_digest="short",
            )

    def test_valid_digest(self) -> None:
        sha = "a" * 64
        manifest = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/test",
            executable_digest=sha,
        )
        assert manifest.executable_digest == sha

    def test_health_check_enabled(self) -> None:
        manifest = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/test",
            health_probe_command=("/bin/test", "status"),
        )
        assert manifest.health_check_enabled

    def test_health_check_disabled(self) -> None:
        manifest = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/test",
        )
        assert not manifest.health_check_enabled

    def test_has_fallback(self) -> None:
        manifest = ProviderManifest(
            provider_id="primary",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/primary",
            fallback_provider_id="secondary",
        )
        assert manifest.has_fallback

    def test_no_fallback(self) -> None:
        manifest = ProviderManifest(
            provider_id="primary",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/primary",
        )
        assert not manifest.has_fallback

    def test_manifest_hash_deterministic(self) -> None:
        m1 = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.EXECUTION,
            version="2.0.0",
            executable="/bin/test",
        )
        m2 = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.EXECUTION,
            version="2.0.0",
            executable="/bin/test",
        )
        assert m1.manifest_hash == m2.manifest_hash

    def test_manifest_hash_changes_with_version(self) -> None:
        m1 = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/test",
        )
        m2 = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.ANALYZER,
            version="2.0.0",
            executable="/bin/test",
        )
        assert m1.manifest_hash != m2.manifest_hash

    def test_manifest_hash_order_independent(self) -> None:
        m1 = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/test",
            supported_languages=("python", "go"),
            supported_capabilities=("lint", "format"),
        )
        m2 = ProviderManifest(
            provider_id="test",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/test",
            supported_languages=("go", "python"),
            supported_capabilities=("format", "lint"),
        )
        assert m1.manifest_hash == m2.manifest_hash

    def test_full_manifest(self) -> None:
        manifest = ProviderManifest(
            provider_id="codeql",
            kind=ProviderKind.ANALYZER,
            version="2.17.0",
            executable="/usr/local/bin/codeql",
            executable_digest=hashlib.sha256(b"codeql").hexdigest(),
            supported_languages=("python", "javascript", "go"),
            supported_capabilities=("security", "quality"),
            health_probe_command=("codeql", "version"),
            coverage_model=CoverageModel.STATEMENT,
            confidence_model=ConfidenceModel.STATIC,
            network_policy="restricted",
            filesystem=ProviderFilesystemRequirement(
                capability="workspace_write",
                allowed_paths=("/workspace",),
            ),
            output_bounds=ProviderOutputBounds(
                max_stdout_chars=50_000,
                max_stderr_chars=5_000,
                max_artifact_bytes=5_000_000,
            ),
            fallback_provider_id="semgrep",
        )
        assert manifest.manifest_hash
        assert manifest.health_check_enabled
        assert manifest.has_fallback
        assert manifest.provider_id == "codeql"


class TestProviderHealth:
    def test_valid_health(self) -> None:
        health = ProviderHealth(
            provider_id="test",
            status=ProviderHealthStatus.HEALTHY,
            message="All good",
            checked_at="2026-07-15T00:00:00Z",
        )
        assert health.provider_id == "test"
        assert health.status is ProviderHealthStatus.HEALTHY

    def test_invalid_provider_id(self) -> None:
        with pytest.raises(ValueError, match="Invalid provider_id"):
            ProviderHealth(provider_id="", status=ProviderHealthStatus.UNKNOWN)

    def test_invalid_status(self) -> None:
        with pytest.raises(ValueError, match="status must be a ProviderHealthStatus"):
            ProviderHealth(provider_id="test", status="bad")  # type: ignore[arg-type]


class TestProviderKind:
    def test_all_kinds(self) -> None:
        assert ProviderKind.CODE_INTELLIGENCE.value == "code_intelligence"
        assert ProviderKind.ANALYZER.value == "analyzer"
        assert ProviderKind.POLICY.value == "policy"
        assert ProviderKind.EXECUTION.value == "execution"


class TestConfigProviderRegistry:
    def test_empty_registry(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        registry = ConfigProviderRegistry()
        assert registry.list_providers() == ()
        assert registry.get_provider("nonexistent") is None

    def test_single_provider(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        manifest = ProviderManifest(
            provider_id="my-analyzer",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/analyzer",
        )
        registry = ConfigProviderRegistry(providers=(manifest,))
        providers = registry.list_providers()
        assert len(providers) == 1
        assert providers[0].provider_id == "my-analyzer"

    def test_get_provider_by_id(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        m1 = ProviderManifest(
            provider_id="analyzer-a",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/a",
        )
        m2 = ProviderManifest(
            provider_id="analyzer-b",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/b",
        )
        registry = ConfigProviderRegistry(providers=(m1, m2))
        assert registry.get_provider("analyzer-a") is m1
        assert registry.get_provider("analyzer-b") is m2

    def test_get_provider_not_found(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        registry = ConfigProviderRegistry()
        assert registry.get_provider("missing") is None

    def test_get_providers_by_kind(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        m1 = ProviderManifest(
            provider_id="analyzer",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/analyzer",
        )
        m2 = ProviderManifest(
            provider_id="executor",
            kind=ProviderKind.EXECUTION,
            version="1.0.0",
            executable="/bin/executor",
        )
        registry = ConfigProviderRegistry(providers=(m1, m2))
        analyzers = registry.get_providers_by_kind("analyzer")
        assert len(analyzers) == 1
        assert analyzers[0].provider_id == "analyzer"

    def test_get_providers_by_invalid_kind(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        registry = ConfigProviderRegistry()
        assert registry.get_providers_by_kind("invalid_kind") == ()

    def test_deterministic_ordering(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        m1 = ProviderManifest(
            provider_id="z-analyzer",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/z",
        )
        m2 = ProviderManifest(
            provider_id="a-analyzer",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/a",
        )
        registry = ConfigProviderRegistry(providers=(m1, m2))
        ids = [p.provider_id for p in registry.list_providers()]
        assert ids == ["a-analyzer", "z-analyzer"]

    def test_check_health_nonexistent(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        registry = ConfigProviderRegistry()
        health = registry.check_health("missing")
        assert health.status is ProviderHealthStatus.UNREACHABLE

    def test_check_health_no_probe(self) -> None:
        from repoforge.adapters.provider.config_registry import ConfigProviderRegistry

        manifest = ProviderManifest(
            provider_id="no-probe",
            kind=ProviderKind.ANALYZER,
            version="1.0.0",
            executable="/bin/noprobe",
        )
        registry = ConfigProviderRegistry(providers=(manifest,))
        health = registry.check_health("no-probe")
        assert health.status is ProviderHealthStatus.UNKNOWN


class TestProviderManifestFromConfig:
    def test_minimal_config(self) -> None:
        from repoforge.adapters.provider.config_registry import provider_manifest_from_config

        manifest = provider_manifest_from_config(
            {
                "provider_id": "my-tool",
                "kind": "analyzer",
                "version": "1.0.0",
                "executable": "/usr/bin/my-tool",
            }
        )
        assert manifest.provider_id == "my-tool"
        assert manifest.kind is ProviderKind.ANALYZER
        assert manifest.version == "1.0.0"
        assert manifest.executable == "/usr/bin/my-tool"

    def test_full_config(self) -> None:
        from repoforge.adapters.provider.config_registry import provider_manifest_from_config

        manifest = provider_manifest_from_config(
            {
                "provider_id": "codeql",
                "kind": "analyzer",
                "version": "2.17.0",
                "executable": "/usr/bin/codeql",
                "executable_digest": hashlib.sha256(b"codeql").hexdigest(),
                "supported_languages": ["python", "go"],
                "supported_capabilities": ["security"],
                "health_probe_command": ["codeql", "version"],
                "coverage_model": "statement",
                "confidence_model": "static",
                "network_policy": "restricted",
                "filesystem": {
                    "capability": "workspace_write",
                    "allowed_paths": ["/workspace"],
                },
                "output_bounds": {
                    "max_stdout_chars": 50000,
                    "max_stderr_chars": 5000,
                    "max_artifact_bytes": 5000000,
                },
                "fallback_provider_id": "semgrep",
            }
        )
        assert manifest.provider_id == "codeql"
        assert len(manifest.supported_languages) == 2
        assert manifest.has_fallback
        assert manifest.fallback_provider_id == "semgrep"
        assert manifest.manifest_hash

    def test_invalid_kind_falls_back(self) -> None:
        from repoforge.adapters.provider.config_registry import provider_manifest_from_config

        manifest = provider_manifest_from_config(
            {
                "provider_id": "test",
                "kind": "unknown_kind",
                "version": "1.0.0",
                "executable": "/bin/test",
            }
        )
        assert manifest.kind is ProviderKind.ANALYZER
