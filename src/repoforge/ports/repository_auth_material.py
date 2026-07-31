"""Ephemeral repository-auth material resolution boundary."""

from __future__ import annotations

from typing import Protocol

from ..domain.repository_auth_broker import AuthMaterial
from ..domain.repository_identity import OpaqueCredentialReference


class RepositoryAuthMaterialProvider(Protocol):
    def resolve(self, reference: OpaqueCredentialReference) -> AuthMaterial | None: ...

    def refresh(
        self,
        reference: OpaqueCredentialReference,
        previous: AuthMaterial,
    ) -> AuthMaterial | None: ...

    def release(self, material: AuthMaterial) -> None: ...
