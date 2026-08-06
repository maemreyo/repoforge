"""Durable storage for operator-issued host-bypass capability leases (#383)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ...domain.durable_state import Revision, StateEnvelope, StatePage
from ...domain.host_bypass_lease import (
    HOST_BYPASS_LEASE_SCHEMA_VERSION,
    HostBypassLease,
    validate_lease_id,
)
from ...domain.versioning import SchemaVersion
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"lease {name} must be a string")
    return datetime.fromisoformat(value)


class HostBypassLeaseCodec:
    schema_version = SchemaVersion(HOST_BYPASS_LEASE_SCHEMA_VERSION)

    def encode(self, value: HostBypassLease) -> dict[str, object]:
        return {
            "lease_id": value.lease_id,
            "repository_identity": value.repository_identity,
            "checkout_identity": value.checkout_identity,
            "workspace_kind": value.workspace_kind,
            "branch_or_ref": value.branch_or_ref,
            "allowed_effects": list(value.allowed_effects),
            "host_effect_scope": list(value.host_effect_scope),
            "execution_environment_id": value.execution_environment_id,
            "credential_profile_ids": list(value.credential_profile_ids),
            "granted_by": value.granted_by,
            "principal_token_hash": value.principal_token_hash,
            "config_generation": value.config_generation,
            "policy_digest": value.policy_digest,
            "issued_at": _iso(value.issued_at),
            "expires_at": _iso(value.expires_at),
            "revoked_at": _iso(value.revoked_at) if value.revoked_at is not None else None,
        }

    def decode(self, payload: dict[str, object]) -> HostBypassLease:
        expected_fields = {
            "lease_id",
            "repository_identity",
            "checkout_identity",
            "workspace_kind",
            "branch_or_ref",
            "allowed_effects",
            "host_effect_scope",
            "execution_environment_id",
            "credential_profile_ids",
            "granted_by",
            "principal_token_hash",
            "config_generation",
            "policy_digest",
            "issued_at",
            "expires_at",
            "revoked_at",
        }
        if set(payload) != expected_fields:
            raise ValueError("host-bypass lease fields do not match schema version 1")
        allowed_effects = payload["allowed_effects"]
        host_effect_scope = payload["host_effect_scope"]
        credential_profile_ids = payload["credential_profile_ids"]
        if not isinstance(allowed_effects, list) or not all(
            isinstance(item, str) for item in allowed_effects
        ):
            raise ValueError("lease allowed_effects is invalid")
        if not isinstance(host_effect_scope, list) or not all(
            isinstance(item, str) for item in host_effect_scope
        ):
            raise ValueError("lease host_effect_scope is invalid")
        if not isinstance(credential_profile_ids, list) or not all(
            isinstance(item, str) for item in credential_profile_ids
        ):
            raise ValueError("lease credential_profile_ids is invalid")
        execution_environment_id = payload["execution_environment_id"]
        revoked_at = payload["revoked_at"]
        return HostBypassLease(
            lease_id=str(payload["lease_id"]),
            repository_identity=str(payload["repository_identity"]),
            checkout_identity=str(payload["checkout_identity"]),
            workspace_kind=str(payload["workspace_kind"]),
            branch_or_ref=str(payload["branch_or_ref"]),
            allowed_effects=tuple(allowed_effects),
            host_effect_scope=tuple(host_effect_scope),
            execution_environment_id=(
                str(execution_environment_id) if execution_environment_id is not None else None
            ),
            credential_profile_ids=tuple(credential_profile_ids),
            granted_by=str(payload["granted_by"]),
            principal_token_hash=str(payload["principal_token_hash"]),
            config_generation=str(payload["config_generation"]),
            policy_digest=str(payload["policy_digest"]),
            issued_at=_parse_iso("issued_at", payload["issued_at"]),
            expires_at=_parse_iso("expires_at", payload["expires_at"]),
            revoked_at=_parse_iso("revoked_at", revoked_at) if revoked_at is not None else None,
        )


class JsonHostBypassLeaseStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._repository = JsonStateRepository[HostBypassLease](
            state_root,
            collection="host-bypass-leases",
            locks=locks,
            codec=HostBypassLeaseCodec(),
            id_validator=validate_lease_id,
            max_record_bytes=32_000,
        )
        self.root = self._repository.root

    def create(self, lease: HostBypassLease) -> StateEnvelope[HostBypassLease]:
        return self._repository.create(lease.lease_id, lease)

    def read(self, lease_id: str) -> StateEnvelope[HostBypassLease] | None:
        return self._repository.read(lease_id)

    def save(
        self, lease: HostBypassLease, *, expected_revision: Revision
    ) -> StateEnvelope[HostBypassLease]:
        return self._repository.save(lease.lease_id, lease, expected_revision=expected_revision)

    def list_records(self, *, max_records: int = 500) -> StatePage[HostBypassLease]:
        return self._repository.list_records(max_records=max_records)
