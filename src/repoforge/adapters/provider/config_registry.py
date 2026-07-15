"""Config-backed provider registry — loaded from reviewed configuration."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

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
        ids = tuple(provider.provider_id for provider in self.providers)
        if len(ids) != len(set(ids)):
            raise ValueError("Provider registry contains duplicate provider_id values")
        self._provider_index = {provider.provider_id: provider for provider in self.providers}

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


def _string_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(value) for value in raw)


def _config_map(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def _positive_int(raw: object, default: int) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return default


def provider_manifest_from_config(raw: dict[str, object]) -> ProviderManifest:
    try:
        kind = ProviderKind(str(raw.get("kind", "analyzer")))
    except ValueError:
        kind = ProviderKind.ANALYZER
    filesystem_raw = _config_map(raw.get("filesystem"))
    output_raw = _config_map(raw.get("output_bounds"))
    return ProviderManifest(
        provider_id=str(raw.get("provider_id", "")),
        kind=kind,
        version=str(raw.get("version", "")),
        executable=str(raw.get("executable", "")),
        executable_digest=str(raw.get("executable_digest", "")),
        supported_languages=_string_tuple(raw.get("supported_languages")),
        supported_capabilities=_string_tuple(raw.get("supported_capabilities")),
        health_probe_command=_string_tuple(raw.get("health_probe_command")),
        coverage_model=CoverageModel(str(raw.get("coverage_model", "none"))),
        confidence_model=ConfidenceModel(str(raw.get("confidence_model", "none"))),
        network_policy=str(raw.get("network_policy", "none")),
        filesystem=ProviderFilesystemRequirement(
            capability=str(filesystem_raw.get("capability", "read")),
            allowed_paths=_string_tuple(filesystem_raw.get("allowed_paths")),
        ),
        output_bounds=ProviderOutputBounds(
            max_stdout_chars=_positive_int(output_raw.get("max_stdout_chars"), 100_000),
            max_stderr_chars=_positive_int(output_raw.get("max_stderr_chars"), 10_000),
            max_artifact_bytes=_positive_int(
                output_raw.get("max_artifact_bytes"),
                10_000_000,
            ),
        ),
        fallback_provider_id=str(raw.get("fallback_provider_id", "")),
    )
