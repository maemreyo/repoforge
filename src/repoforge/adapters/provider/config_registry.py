"""Immutable registry derived only from reviewed provider manifests."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ...domain.provider_manifest import (
    ProviderAvailability,
    ProviderAvailabilityStatus,
    ProviderImageIdentity,
    ProviderKind,
    ProviderManifest,
)
from ...ports.capabilities import ExecutableLocator

_MAX_EXECUTABLE_BYTES: Final = 1_000_000_000


@dataclass(frozen=True, slots=True)
class ConfigProviderRegistry:
    providers: tuple[ProviderManifest, ...]
    executables: ExecutableLocator
    _provider_index: dict[str, ProviderManifest] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        index = {provider.provider_id: provider for provider in self.providers}
        if len(index) != len(self.providers):
            raise ValueError("Provider registry contains duplicate provider_id values")
        self._validate_fallbacks(index)
        object.__setattr__(self, "_provider_index", index)

    @staticmethod
    def _validate_fallbacks(index: dict[str, ProviderManifest]) -> None:
        for provider in index.values():
            fallback_id = provider.fallback_provider_id
            if not fallback_id:
                continue
            fallback = index.get(fallback_id)
            if fallback is None:
                raise ValueError(f"Provider {provider.provider_id!r} references unknown fallback")
            if fallback.kind is not provider.kind:
                raise ValueError("Provider fallback kind is incompatible")
            if not fallback.is_compatible_with(provider.version):
                raise ValueError("Provider fallback major version is incompatible")
            if not fallback.supports(provider.supported_capabilities):
                raise ValueError("Provider fallback is missing required capabilities")
            if not set(provider.supported_languages).issubset(fallback.supported_languages):
                raise ValueError("Provider fallback is missing required languages")
        for provider_id in index:
            seen: set[str] = set()
            current = provider_id
            while current:
                if current in seen:
                    raise ValueError("Provider fallback cycle is not allowed")
                seen.add(current)
                current = index[current].fallback_provider_id

    def list_providers(self) -> tuple[ProviderManifest, ...]:
        return tuple(sorted(self.providers, key=lambda provider: provider.provider_id))

    def get_provider(self, provider_id: str) -> ProviderManifest | None:
        return self._provider_index.get(provider_id)

    def get_providers_by_kind(self, kind: ProviderKind) -> tuple[ProviderManifest, ...]:
        return tuple(provider for provider in self.list_providers() if provider.kind is kind)

    def check_availability(self, provider_id: str) -> ProviderAvailability:
        provider = self.get_provider(provider_id)
        if provider is None:
            return ProviderAvailability(
                provider_id,
                ProviderAvailabilityStatus.UNAVAILABLE,
                "Provider is not registered",
            )
        runtime = provider.runtime
        if isinstance(runtime, ProviderImageIdentity):
            return ProviderAvailability(
                provider_id,
                ProviderAvailabilityStatus.UNVERIFIED,
                "Image availability requires a configured execution adapter",
            )
        executable = self.executables.which(runtime.executable)
        if executable is None:
            return ProviderAvailability(
                provider_id,
                ProviderAvailabilityStatus.UNAVAILABLE,
                "Configured executable is unavailable",
            )
        resolved = Path(executable).resolve()
        try:
            actual_digest = self._sha256_file(resolved)
        except OSError:
            return ProviderAvailability(
                provider_id,
                ProviderAvailabilityStatus.UNAVAILABLE,
                "Configured executable cannot be read",
            )
        if actual_digest != runtime.sha256:
            return ProviderAvailability(
                provider_id,
                ProviderAvailabilityStatus.UNAVAILABLE,
                "Configured executable digest does not match",
            )
        return ProviderAvailability(
            provider_id,
            ProviderAvailabilityStatus.AVAILABLE,
            "Configured executable identity verified",
            str(resolved),
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        with os.fdopen(os.open(path, flags), "rb") as handle:
            file_status = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_status.st_mode):
                raise OSError("Provider executable must be a regular file")
            if file_status.st_size > _MAX_EXECUTABLE_BYTES:
                raise OSError("Provider executable exceeds the digest verification limit")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
