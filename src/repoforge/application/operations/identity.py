"""Application orchestration for durable operation-scoped identities."""

from __future__ import annotations

from ...domain.durable_state import Revision, StateEnvelope
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_identity import (
    LeaseCapabilityRequest,
    OperationIdentityRecord,
    OperationIdentityReference,
    expire_operation_leases,
    new_operation_identity_record,
    refresh_operation_lease,
    require_operation_lease,
    revoke_operation_leases,
)
from ...domain.operation_task import TERMINAL_OPERATION_STATES, OperationTask
from ...domain.repository_identity import AuthLease, AuthTargetKind, OperationIdentityContext
from ...ports.operation_identity_store import OperationIdentityStore
from ...ports.operation_store import OperationStore


def _error(code: ErrorCode, message: str, *, retryable: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        unchanged_state=("No external repository write was admitted.",),
    )


class OperationIdentityManager:
    def __init__(
        self,
        *,
        operations: OperationStore,
        identities: OperationIdentityStore,
    ) -> None:
        self._operations = operations
        self._identities = identities

    def _operation(self, operation_id: str) -> OperationTask:
        operation = self._operations.read(operation_id)
        if operation is None:
            raise _error(ErrorCode.OPERATION_NOT_FOUND, f"Operation not found: {operation_id}")
        return operation

    def _read(
        self,
        operation_id: str,
    ) -> StateEnvelope[OperationIdentityRecord] | None:
        try:
            return self._identities.read(operation_id)
        except RepoForgeError as exc:
            if exc.code in {
                ErrorCode.OPERATION_IDENTITY_NOT_FOUND,
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
            }:
                raise
            raise _error(
                ErrorCode.CREDENTIAL_BROKER_UNAVAILABLE,
                "Durable operation identity state is unavailable; writes are denied.",
                retryable=True,
            ) from None

    def bind(
        self,
        context: OperationIdentityContext,
        *,
        context_id: str,
        capability_requests: tuple[LeaseCapabilityRequest, ...],
        now: str,
    ) -> OperationIdentityRecord:
        operation = self._operation(context.operation_id)
        if operation.operation_id != context.operation_id:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
                "Operation identity context does not match the durable operation.",
            )
        candidate = new_operation_identity_record(
            context,
            context_id=context_id,
            capability_requests=capability_requests,
            now=now,
        )
        existing = self._read(context.operation_id)
        if existing is not None:
            current = existing.value
            if (
                current.reference == candidate.reference
                and current.context == candidate.context
                and current.capability_requests == candidate.capability_requests
            ):
                return current
            raise _error(
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
                "The durable operation is already bound to another identity decision.",
            )
        try:
            return self._identities.create(candidate).value
        except RepoForgeError as exc:
            if exc.code is ErrorCode.ALREADY_EXISTS:
                raced = self._read(context.operation_id)
                if raced is not None and raced.value.reference == candidate.reference:
                    return raced.value
                raise _error(
                    ErrorCode.OPERATION_IDENTITY_MISMATCH,
                    "Another writer bound the operation to a different identity decision.",
                ) from None
            raise

    def inspect(self, operation_id: str) -> StateEnvelope[OperationIdentityRecord]:
        """Read the sidecar with its revision, for reporting and compare-and-swap revocation.

        Unlike `resume`, this does not require the caller to already hold the identity
        reference: an operator inspecting or revoking a lease is asking what the operation is
        bound to, not asserting it.
        """

        self._operation(operation_id)
        envelope = self._read(operation_id)
        if envelope is None:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_NOT_FOUND,
                "No durable operation identity sidecar exists for this operation.",
            )
        return envelope

    def resume(
        self,
        operation_id: str,
        reference: OperationIdentityReference,
    ) -> OperationIdentityRecord:
        self._operation(operation_id)
        envelope = self._read(operation_id)
        if envelope is None:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_NOT_FOUND,
                "No durable operation identity sidecar exists for this operation.",
            )
        if envelope.value.reference != reference:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
                "Resume or handoff identity reference does not match the original operation.",
            )
        return envelope.value

    def require_write(
        self,
        *,
        operation_id: str,
        reference: OperationIdentityReference,
        target_kind: AuthTargetKind,
        target_id: str,
        capability_id: str,
        now: str,
    ) -> AuthLease:
        operation = self._operation(operation_id)
        if operation.state in TERMINAL_OPERATION_STATES:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_MISMATCH,
                "A terminal operation cannot consume an auth lease for an external write.",
            )
        record = self.resume(operation_id, reference)
        return require_operation_lease(
            record,
            operation_id=operation_id,
            target_kind=target_kind,
            target_id=target_id,
            capability_id=capability_id,
            now=now,
        )

    def _save_lifecycle(
        self,
        record: OperationIdentityRecord,
        *,
        expected_revision: Revision,
        action: str,
    ) -> OperationIdentityRecord:
        try:
            return self._identities.save(
                record,
                expected_revision=expected_revision,
            ).value
        except RepoForgeError as exc:
            if exc.code is ErrorCode.STATE_STALE:
                raise _error(
                    ErrorCode.OPERATION_IDENTITY_STALE,
                    f"Operation identity changed during {action}.",
                    retryable=True,
                ) from None
            raise

    def refresh(
        self,
        operation_id: str,
        reference: OperationIdentityReference,
        replacement: AuthLease,
        *,
        expected_revision: Revision,
        now: str,
    ) -> OperationIdentityRecord:
        record = self.resume(operation_id, reference)
        updated = refresh_operation_lease(record, replacement, now=now)
        return self._save_lifecycle(
            updated,
            expected_revision=expected_revision,
            action="refresh",
        )

    def revoke(
        self,
        operation_id: str,
        *,
        expected_revision: Revision,
        now: str,
        lease_id: str | None = None,
        profile_id: str | None = None,
    ) -> OperationIdentityRecord:
        envelope = self._read(operation_id)
        if envelope is None:
            raise _error(ErrorCode.OPERATION_IDENTITY_NOT_FOUND, "Operation identity not found.")
        if envelope.revision != expected_revision:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_STALE,
                "Operation identity revision is stale.",
                retryable=True,
            )
        updated = revoke_operation_leases(
            envelope.value,
            now=now,
            lease_id=lease_id,
            profile_id=profile_id,
        )
        return self._save_lifecycle(
            updated,
            expected_revision=expected_revision,
            action="revocation",
        )

    def expire(
        self,
        operation_id: str,
        *,
        expected_revision: Revision,
        now: str,
    ) -> OperationIdentityRecord:
        envelope = self._read(operation_id)
        if envelope is None:
            raise _error(ErrorCode.OPERATION_IDENTITY_NOT_FOUND, "Operation identity not found.")
        if envelope.revision != expected_revision:
            raise _error(
                ErrorCode.OPERATION_IDENTITY_STALE,
                "Operation identity revision is stale.",
                retryable=True,
            )
        updated = expire_operation_leases(envelope.value, now=now)
        if updated == envelope.value:
            return updated
        return self._save_lifecycle(
            updated,
            expected_revision=expected_revision,
            action="expiry reconciliation",
        )

    def delete(self, operation_id: str) -> None:
        self._identities.delete(operation_id)
