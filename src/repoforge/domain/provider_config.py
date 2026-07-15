"""Strict TOML parsing for reviewed provider manifests."""

from __future__ import annotations

from .errors import ConfigError
from .provider_manifest import (
    ConfidenceModel,
    CoverageModel,
    ProviderExecutableIdentity,
    ProviderFilesystemRequirement,
    ProviderImageIdentity,
    ProviderKind,
    ProviderManifest,
    ProviderOutputBounds,
    ProviderRuntimeIdentity,
)


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a TOML table")
    return {str(key): item for key, item in value.items()}


def _string(value: object, context: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{context} must be a string")
    return value


def _strings(value: object, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{context} must be an array of strings")
    return tuple(item for item in value if isinstance(item, str))


def _positive(value: object, default: int, context: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{context} must be a positive integer")
    return value


def _provider_kind(value: object, context: str) -> ProviderKind:
    raw = _string(value, context, default=ProviderKind.ANALYZER.value)
    try:
        return ProviderKind(raw)
    except ValueError as exc:
        allowed = sorted(item.value for item in ProviderKind)
        raise ConfigError(f"{context} must be one of {allowed}") from exc


def _coverage_model(value: object, context: str) -> CoverageModel:
    raw = _string(value, context, default=CoverageModel.NONE.value)
    try:
        return CoverageModel(raw)
    except ValueError as exc:
        allowed = sorted(item.value for item in CoverageModel)
        raise ConfigError(f"{context} must be one of {allowed}") from exc


def _confidence_model(value: object, context: str) -> ConfidenceModel:
    raw = _string(value, context, default=ConfidenceModel.NONE.value)
    try:
        return ConfidenceModel(raw)
    except ValueError as exc:
        allowed = sorted(item.value for item in ConfidenceModel)
        raise ConfigError(f"{context} must be one of {allowed}") from exc


def provider_manifest_from_config(raw: object, context: str) -> ProviderManifest:
    table = _mapping(raw, context)
    executable = table.get("executable")
    image = table.get("image")
    if (executable is None) == (image is None):
        raise ConfigError(f"{context} must configure exactly one of executable or image")
    runtime: ProviderRuntimeIdentity
    if executable is not None:
        runtime = ProviderExecutableIdentity(
            _string(executable, f"{context}.executable"),
            _string(table.get("executable_digest"), f"{context}.executable_digest"),
        )
    else:
        runtime = ProviderImageIdentity(
            _string(image, f"{context}.image"),
            _string(table.get("image_digest"), f"{context}.image_digest"),
        )
    filesystem = _mapping(table.get("filesystem", {}), f"{context}.filesystem")
    output_bounds = _mapping(table.get("output_bounds", {}), f"{context}.output_bounds")
    try:
        return ProviderManifest(
            provider_id=_string(table.get("provider_id"), f"{context}.provider_id"),
            kind=_provider_kind(table.get("kind"), f"{context}.kind"),
            version=_string(table.get("version"), f"{context}.version"),
            runtime=runtime,
            supported_languages=_strings(
                table.get("supported_languages"), f"{context}.supported_languages"
            ),
            supported_capabilities=_strings(
                table.get("supported_capabilities"), f"{context}.supported_capabilities"
            ),
            health_probe_arguments=_strings(
                table.get("health_probe_arguments"), f"{context}.health_probe_arguments"
            ),
            coverage_model=_coverage_model(
                table.get("coverage_model"),
                f"{context}.coverage_model",
            ),
            confidence_model=_confidence_model(
                table.get("confidence_model"),
                f"{context}.confidence_model",
            ),
            network_policy=_string(
                table.get("network_policy"), f"{context}.network_policy", default="none"
            ),
            filesystem=ProviderFilesystemRequirement(
                capability=_string(
                    filesystem.get("capability"),
                    f"{context}.filesystem.capability",
                    default="read",
                ),
                allowed_paths=_strings(
                    filesystem.get("allowed_paths"), f"{context}.filesystem.allowed_paths"
                ),
            ),
            output_bounds=ProviderOutputBounds(
                max_stdout_chars=_positive(
                    output_bounds.get("max_stdout_chars"),
                    100_000,
                    f"{context}.output_bounds.max_stdout_chars",
                ),
                max_stderr_chars=_positive(
                    output_bounds.get("max_stderr_chars"),
                    10_000,
                    f"{context}.output_bounds.max_stderr_chars",
                ),
                max_artifact_bytes=_positive(
                    output_bounds.get("max_artifact_bytes"),
                    10_000_000,
                    f"{context}.output_bounds.max_artifact_bytes",
                ),
            ),
            fallback_provider_id=_string(
                table.get("fallback_provider_id"),
                f"{context}.fallback_provider_id",
                default="",
            ),
            schema_version=_positive(table.get("schema_version"), 1, f"{context}.schema_version"),
        )
    except ValueError as exc:
        raise ConfigError(f"{context} is invalid: {exc}") from exc


def load_provider_manifests(raw: object) -> tuple[ProviderManifest, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("providers must be an array of TOML tables")
    return tuple(
        provider_manifest_from_config(item, f"providers[{index}]") for index, item in enumerate(raw)
    )
