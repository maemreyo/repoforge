"""Deterministic repository-auth material provider fakes."""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.repository_auth_broker import AuthMaterial
from ..domain.repository_identity import OpaqueCredentialReference


class DeterministicAuthMaterialProvider:
    def __init__(
        self,
        materials: Mapping[str, AuthMaterial] | None = None,
        *,
        refreshes: Mapping[str, AuthMaterial] | None = None,
        unavailable: bool = False,
    ) -> None:
        self.materials = dict(materials or {})
        self.refreshes = dict(refreshes or {})
        self.unavailable = unavailable
        self.resolve_calls: list[str] = []
        self.refresh_calls: list[str] = []
        self.release_calls: list[str] = []

    def resolve(self, reference: OpaqueCredentialReference) -> AuthMaterial | None:
        self.resolve_calls.append(reference.reference_id)
        if self.unavailable:
            raise RuntimeError("provider unavailable secret=must-not-leak")
        return self.materials.get(reference.reference_id)

    def refresh(
        self,
        reference: OpaqueCredentialReference,
        previous: AuthMaterial,
    ) -> AuthMaterial | None:
        del previous
        self.refresh_calls.append(reference.reference_id)
        if self.unavailable:
            raise RuntimeError("refresh unavailable secret=must-not-leak")
        return self.refreshes.get(reference.reference_id)

    def release(self, material: AuthMaterial) -> None:
        self.release_calls.append(material.material_id)
        material.release()
