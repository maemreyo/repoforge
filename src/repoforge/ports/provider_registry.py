"""Provider registry port — abstract boundary for provider lifecycle."""

from __future__ import annotations

from typing import Protocol

from repoforge.domain.provider_manifest import (
    ProviderHealth,
    ProviderManifest,
)


class ProviderRegistry(Protocol):
    """Typed provider registry for lifecycle, discovery, and health.

    Registration is reviewed configuration — provider discovery cannot
    silently grant capability. All operations are read-only after the
    registry is loaded from reviewed configuration.
    """

    def list_providers(self) -> tuple[ProviderManifest, ...]:
        """Return all registered providers in deterministic order."""
        ...

    def get_provider(self, provider_id: str) -> ProviderManifest | None:
        """Look up a provider by ID."""
        ...

    def get_providers_by_kind(self, kind: str) -> tuple[ProviderManifest, ...]:
        """Return all providers of a given kind."""
        ...

    def check_health(self, provider_id: str) -> ProviderHealth:
        """Run the provider's health probe and return status."""
        ...
