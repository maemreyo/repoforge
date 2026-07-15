"""Read-only provider registration and static availability boundary."""

from __future__ import annotations

from typing import Protocol

from ..domain.provider_manifest import (
    ProviderAvailability,
    ProviderKind,
    ProviderManifest,
)


class ProviderRegistry(Protocol):
    def list_providers(self) -> tuple[ProviderManifest, ...]: ...

    def get_provider(self, provider_id: str) -> ProviderManifest | None: ...

    def get_providers_by_kind(self, kind: ProviderKind) -> tuple[ProviderManifest, ...]: ...

    def check_availability(self, provider_id: str) -> ProviderAvailability: ...
