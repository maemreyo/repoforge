"""Config-backed provider registry — loaded from reviewed configuration."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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
from repoforge.domain.errors import RepoForgeError
from repoforge.ports.provider_registry import ProviderRegistry


@dataclass
class ConfigProviderRegistry:
    """Provider registry backed by reviewed configuration.

    Registration is read-only after construction. Provider discovery cannot
    silently grant capability — only providers explicitly listed in the
    configuration are registered.
    """

    providers: tuple[ProviderManifest, ...] = field(default_factory=tuple)
    _provider_index: dict[str, ProviderManifest] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._provider_index = {p.provider_id: p for p in self.providers}

    def list_providers(self) -> tuple[ProviderManifest, ...]:
        return tuple(sorted(self.providers, key=lambda p: p.provider_id))

    def get_provider(self, provider_id: str) -> ProviderManifest | None:
        return self._provider_index.get(provider_id)

    def get_providers_by_kind(self, kind: str) -> tuple[ProviderManifest, ...]:
        try:
            target = ProviderKind(kind)
        except ValueError:
            return ()
        return tuple(
            sorted(
                (p for p in self.providers if p.kind is target),
                key=lambda p: p.provider_id,
            )
        )

    def check_health(self, provider_id: str) -> ProviderHealth:
        manifest = self.get_provider(provider_id)
        if manifest is None:
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.UNREACHABLE,
                message="Provider not found in registry",
            )
        if not manifest.health_check_enabled:
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.UNKNOWN,
                message="No health probe configured",
            )
        try:
            completed = subprocess.run(
                list(manifest.health_probe_command),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode == 0:
                return ProviderHealth(
                    provider_id=provider_id,
                    status=ProviderHealthStatus.HEALTHY,
                    message=completed.stdout.strip()[:256] or "Healthy",
                )
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.DEGRADED,
                message=(completed.stderr or completed.stdout or "").strip()[:256],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.UNREACHABLE,
                message=str(exc)[:256],
            )


def provider_manifest_from_config(
    raw: dict[str, object],
) -> ProviderManifest:
    """Build a ProviderManifest from a reviewed config dict."""
    try:
        kind = ProviderKind(str(raw.get("kind", "analyzer")))
    except ValueError:
        kind = ProviderKind.ANALYZER
    filesystem_raw = raw.get("filesystem", {})
    if not isinstance(filesystem_raw, dict):
        filesystem_raw = {}
    output_raw = raw.get("output_bounds", {})
    if not isinstance(output_raw, dict):
        output_raw = {}
    return ProviderManifest(
        provider_id=str(raw.get("provider_id", "")),
        kind=kind,
        version=str(raw.get("version", "")),
        executable=str(raw.get("executable", "")),
        executable_digest=str(raw.get("executable_digest", "")),
        supported_languages=tuple(str(x) for x in raw.get("supported_languages", ())),
        supported_capabilities=tuple(str(x) for x in raw.get("supported_capabilities", ())),
        health_probe_command=tuple(str(x) for x in raw.get("health_probe_command", ())),
        coverage_model=CoverageModel(str(raw.get("coverage_model", "none"))),
        confidence_model=ConfidenceModel(str(raw.get("confidence_model", "none"))),
        network_policy=str(raw.get("network_policy", "none")),
        filesystem=ProviderFilesystemRequirement(
            capability=str(filesystem_raw.get("capability", "read")),
            allowed_paths=tuple(str(x) for x in filesystem_raw.get("allowed_paths", ())),
        ),
        output_bounds=ProviderOutputBounds(
            max_stdout_chars=int(output_raw.get("max_stdout_chars", 100_000)),
            max_stderr_chars=int(output_raw.get("max_stderr_chars", 10_000)),
            max_artifact_bytes=int(output_raw.get("max_artifact_bytes", 10_000_000)),
        ),
        fallback_provider_id=str(raw.get("fallback_provider_id", "")),
    )
