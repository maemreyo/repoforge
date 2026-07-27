"""Durable stable repository identity binding boundary."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.repository_identity import RepositoryIdentityBinding


class RepositoryBindingStore(Protocol):
    def create(
        self, binding: RepositoryIdentityBinding
    ) -> StateEnvelope[RepositoryIdentityBinding]: ...

    def read(
        self, provider_host: str, repository_id: str
    ) -> StateEnvelope[RepositoryIdentityBinding] | None: ...

    def save(
        self,
        binding: RepositoryIdentityBinding,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[RepositoryIdentityBinding]: ...

    def list_bindings(self, *, max_records: int) -> StatePage[RepositoryIdentityBinding]: ...
