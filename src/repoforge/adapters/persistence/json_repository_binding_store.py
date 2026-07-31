"""Private atomic persistence for stable repository identity bindings."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.repository_identity import RepositoryIdentityBinding, RepositoryProvider
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_REPOSITORY_BINDING_SCHEMA_VERSION = 1
_RECORD_ID = re.compile(r"^repository-binding-[a-f0-9]{24}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def repository_binding_record_id(provider_host: str, repository_id: str) -> str:
    if not isinstance(provider_host, str) or _HOST.fullmatch(provider_host) is None:
        raise ValueError("provider_host must be a bounded lowercase host")
    if not isinstance(repository_id, str) or _SAFE_ID.fullmatch(repository_id) is None:
        raise ValueError("repository_id must be a bounded safe identifier")
    digest = hashlib.sha256(f"{provider_host}\0{repository_id}".encode()).hexdigest()
    return f"repository-binding-{digest[:24]}"


def _validate_record_id(value: str) -> str:
    if not isinstance(value, str) or _RECORD_ID.fullmatch(value) is None:
        raise ValueError("repository binding record ID is invalid")
    return value


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


class _RepositoryBindingCodec(StateCodec[RepositoryIdentityBinding]):
    schema_version = SchemaVersion(_REPOSITORY_BINDING_SCHEMA_VERSION)

    def encode(self, value: RepositoryIdentityBinding) -> dict[str, object]:
        return value.payload()

    def decode(self, payload: dict[str, object]) -> RepositoryIdentityBinding:
        expected = {
            "provider",
            "provider_host",
            "repository_id",
            "canonical_name",
            "human_profile_id",
            "agent_profile_id",
            "config_revision",
        }
        if set(payload) != expected:
            raise ValueError("repository binding payload fields are invalid")
        return RepositoryIdentityBinding(
            provider=RepositoryProvider(_required_string(payload, "provider")),
            provider_host=_required_string(payload, "provider_host"),
            repository_id=_required_string(payload, "repository_id"),
            canonical_name=_required_string(payload, "canonical_name"),
            human_profile_id=_optional_string(payload, "human_profile_id"),
            agent_profile_id=_optional_string(payload, "agent_profile_id"),
            config_revision=_required_string(payload, "config_revision"),
        )


class JsonRepositoryBindingStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records: JsonStateRepository[RepositoryIdentityBinding] = JsonStateRepository(
            state_root,
            collection="repository-identity-bindings",
            locks=locks,
            codec=_RepositoryBindingCodec(),
            id_validator=_validate_record_id,
            max_record_bytes=16_384,
        )
        self.root = self._records.root

    @staticmethod
    def _record_id(binding: RepositoryIdentityBinding) -> str:
        return repository_binding_record_id(binding.provider_host, binding.repository_id)

    def create(
        self, binding: RepositoryIdentityBinding
    ) -> StateEnvelope[RepositoryIdentityBinding]:
        return self._records.create(self._record_id(binding), binding)

    def read(
        self, provider_host: str, repository_id: str
    ) -> StateEnvelope[RepositoryIdentityBinding] | None:
        return self._records.read(repository_binding_record_id(provider_host, repository_id))

    def save(
        self,
        binding: RepositoryIdentityBinding,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[RepositoryIdentityBinding]:
        return self._records.save(
            self._record_id(binding),
            binding,
            expected_revision=expected_revision,
        )

    def list_bindings(self, *, max_records: int) -> StatePage[RepositoryIdentityBinding]:
        return self._records.list_records(max_records=max_records)
