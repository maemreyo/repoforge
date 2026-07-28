"""Private durable operation identity sidecar store."""

from __future__ import annotations

from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateEnvelope, StatePage
from ...domain.operation_identity import (
    LeaseCapabilityRequest,
    OperationIdentityRecord,
    OperationIdentityReference,
)
from ...domain.operation_task import validate_operation_id
from ...domain.repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    OpaqueCredentialReference,
    OperationIdentityContext,
    RepositoryProvider,
)
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

OPERATION_IDENTITIES_COLLECTION = "operation-identities"


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _objects(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [_object(item, name) for item in value]


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a string list")
    return tuple(value)


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _exact_fields(payload: dict[str, object], expected: set[str], name: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"{name} fields do not match the schema")


def _lease(payload: dict[str, object]) -> AuthLease:
    _exact_fields(
        payload,
        {
            "lease_id",
            "profile_id",
            "provider",
            "repository_id",
            "target_kind",
            "target_id",
            "actor_id",
            "credential_ref",
            "issued_at",
            "expires_at",
            "state",
            "config_revision",
            "policy_revision",
            "material_digest",
            "provider_metadata",
        },
        "auth lease",
    )
    reference = _object(payload["credential_ref"], "credential_ref")
    _exact_fields(reference, {"scheme", "reference_id"}, "credential_ref")
    metadata = _object(payload["provider_metadata"], "provider_metadata")
    if not all(isinstance(value, str) for value in metadata.values()):
        raise ValueError("provider_metadata values must be strings")
    return AuthLease(
        lease_id=str(payload["lease_id"]),
        profile_id=str(payload["profile_id"]),
        provider=RepositoryProvider(str(payload["provider"])),
        repository_id=str(payload["repository_id"]),
        target_kind=AuthTargetKind(str(payload["target_kind"])),
        target_id=str(payload["target_id"]),
        actor_id=_optional_string(payload["actor_id"], "actor_id"),
        credential_ref=OpaqueCredentialReference(
            scheme=str(reference["scheme"]),
            reference_id=str(reference["reference_id"]),
        ),
        issued_at=str(payload["issued_at"]),
        expires_at=str(payload["expires_at"]),
        state=AuthLeaseState(str(payload["state"])),
        config_revision=str(payload["config_revision"]),
        policy_revision=str(payload["policy_revision"]),
        material_digest=_optional_string(payload["material_digest"], "material_digest"),
        provider_metadata=tuple(sorted((key, str(value)) for key, value in metadata.items())),
    )


class OperationIdentityCodec:
    schema_version = SchemaVersion(1)

    def encode(self, value: OperationIdentityRecord) -> dict[str, object]:
        return value.safe_payload()

    def decode(self, payload: dict[str, object]) -> OperationIdentityRecord:
        _exact_fields(
            payload,
            {
                "reference",
                "operation_id",
                "context",
                "capability_requests",
                "superseded_lease_ids",
                "created_at",
                "updated_at",
            },
            "operation identity",
        )
        reference_payload = _object(payload["reference"], "reference")
        _exact_fields(reference_payload, {"context_id", "context_digest"}, "reference")
        context_payload = _object(payload["context"], "context")
        _exact_fields(
            context_payload,
            {
                "operation_id",
                "primary_repository_id",
                "actor_class",
                "auth_leases",
                "selected_at",
                "config_revision",
                "policy_revision",
            },
            "identity context",
        )
        requests: list[LeaseCapabilityRequest] = []
        for item in _objects(payload["capability_requests"], "capability_requests"):
            _exact_fields(item, {"lease_id", "capability_ids"}, "capability request")
            requests.append(
                LeaseCapabilityRequest(
                    lease_id=str(item["lease_id"]),
                    capability_ids=_strings(item["capability_ids"], "capability_ids"),
                )
            )
        context = OperationIdentityContext(
            operation_id=str(context_payload["operation_id"]),
            primary_repository_id=str(context_payload["primary_repository_id"]),
            actor_class=ActorClass(str(context_payload["actor_class"])),
            auth_leases=tuple(
                _lease(item) for item in _objects(context_payload["auth_leases"], "auth_leases")
            ),
            selected_at=str(context_payload["selected_at"]),
            config_revision=str(context_payload["config_revision"]),
            policy_revision=str(context_payload["policy_revision"]),
        )
        return OperationIdentityRecord(
            reference=OperationIdentityReference(
                context_id=str(reference_payload["context_id"]),
                context_digest=str(reference_payload["context_digest"]),
            ),
            operation_id=str(payload["operation_id"]),
            context=context,
            capability_requests=tuple(requests),
            superseded_lease_ids=_strings(payload["superseded_lease_ids"], "superseded_lease_ids"),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
        )


class JsonOperationIdentityStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._repository = JsonStateRepository[OperationIdentityRecord](
            state_root,
            collection=OPERATION_IDENTITIES_COLLECTION,
            locks=locks,
            codec=OperationIdentityCodec(),
            id_validator=validate_operation_id,
            max_record_bytes=1_000_000,
        )
        self.root = self._repository.root

    def create(self, record: OperationIdentityRecord) -> StateEnvelope[OperationIdentityRecord]:
        return self._repository.create(record.operation_id, record)

    def read(self, operation_id: str) -> StateEnvelope[OperationIdentityRecord] | None:
        return self._repository.read(operation_id)

    def save(
        self,
        record: OperationIdentityRecord,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[OperationIdentityRecord]:
        return self._repository.save(
            record.operation_id,
            record,
            expected_revision=expected_revision,
        )

    def list_records(self, *, max_records: int) -> StatePage[OperationIdentityRecord]:
        return self._repository.list_records(max_records=max_records)

    def delete(self, operation_id: str) -> None:
        self._repository.delete(operation_id)
