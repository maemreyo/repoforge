"""Application orchestration for exact nested-resource operation identities."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime

from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.nested_identity import (
    NestedAccess,
    NestedIdentityReceipt,
    NestedResourceCandidate,
    NestedResourceTarget,
    NestedRoutingDecision,
    NestedRoutingStatus,
    route_nested_resource,
)
from ..domain.operation_identity import (
    LeaseCapabilityRequest,
    OperationIdentityRecord,
)
from ..domain.repository_identity import (
    AuthLease,
    AuthLeaseState,
    OperationIdentityContext,
)
from ..ports.clock import Clock
from ..ports.nested_identity import (
    NestedDiscoveryRequest,
    NestedLeaseProvider,
    NestedResourceDiscovery,
    NestedTargetResolver,
)
from .operations.identity import OperationIdentityManager

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_EXPLICIT_CANDIDATES = 256
_MAX_EXACT_ENTRIES = 256


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _exact_map(
    values: tuple[tuple[str, str], ...],
    field_name: str,
) -> dict[str, str]:
    if not isinstance(values, tuple) or len(values) > _MAX_EXACT_ENTRIES:
        raise ValueError(f"{field_name} must be a bounded tuple")
    result: dict[str, str] = {}
    for target_id, evidence_id in values:
        target_id = _safe_id(target_id, f"{field_name} target_id")
        evidence_id = _safe_id(evidence_id, f"{field_name} evidence_id")
        if target_id in result:
            raise ValueError(f"{field_name} target IDs must be unique")
        result[target_id] = evidence_id
    return result


def _error(
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, object] | None = None,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        unchanged_state=(
            "No nested credentialed effect was admitted.",
            "The operation identity sidecar was not changed.",
        ),
        safe_next_action="Correct the exact nested identity decision and start a new operation.",
        details=details,
    )


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class NestedIdentityPreparationRequest:
    identity_context: OperationIdentityContext
    identity_context_id: str
    primary_capability_requests: tuple[LeaseCapabilityRequest, ...]
    discovery: NestedDiscoveryRequest
    explicit_candidates: tuple[NestedResourceCandidate, ...] = ()
    allow_anonymous_public_read: bool = False
    cross_boundary_approvals: tuple[tuple[str, str], ...] = ()
    publication_intent_ids: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity_context, OperationIdentityContext):
            raise ValueError("identity_context must be an OperationIdentityContext")
        _safe_id(self.identity_context_id, "identity_context_id")
        if (
            not isinstance(self.primary_capability_requests, tuple)
            or not self.primary_capability_requests
            or any(
                not isinstance(item, LeaseCapabilityRequest)
                for item in self.primary_capability_requests
            )
        ):
            raise ValueError(
                "primary_capability_requests must be a non-empty tuple of lease requests"
            )
        request_lease_ids = tuple(item.lease_id for item in self.primary_capability_requests)
        context_lease_ids = tuple(item.lease_id for item in self.identity_context.auth_leases)
        if len(set(request_lease_ids)) != len(request_lease_ids) or set(request_lease_ids) != set(
            context_lease_ids
        ):
            raise ValueError(
                "primary capability requests must cover each supplied primary lease exactly once"
            )
        if not isinstance(self.discovery, NestedDiscoveryRequest):
            raise ValueError("discovery must be a NestedDiscoveryRequest")
        if (
            not isinstance(self.explicit_candidates, tuple)
            or len(self.explicit_candidates) > _MAX_EXPLICIT_CANDIDATES
            or any(
                not isinstance(item, NestedResourceCandidate) for item in self.explicit_candidates
            )
        ):
            raise ValueError("explicit_candidates must be a bounded tuple of candidates")
        if not isinstance(self.allow_anonymous_public_read, bool):
            raise ValueError("allow_anonymous_public_read must be boolean")
        _exact_map(self.cross_boundary_approvals, "cross_boundary_approvals")
        _exact_map(self.publication_intent_ids, "publication_intent_ids")


@dataclass(frozen=True, slots=True)
class NestedIdentityPreparation:
    record: OperationIdentityRecord
    context: OperationIdentityContext
    capability_requests: tuple[LeaseCapabilityRequest, ...]
    receipts: tuple[NestedIdentityReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.record, OperationIdentityRecord):
            raise ValueError("record must be an OperationIdentityRecord")
        if not isinstance(self.context, OperationIdentityContext):
            raise ValueError("context must be an OperationIdentityContext")
        if self.record.context != self.context:
            raise ValueError("record and returned context must match")
        if self.record.capability_requests != self.capability_requests:
            raise ValueError("record and returned capability requests must match")
        if not isinstance(self.receipts, tuple) or any(
            not isinstance(item, NestedIdentityReceipt) for item in self.receipts
        ):
            raise ValueError("receipts must contain NestedIdentityReceipt values")


@dataclass(frozen=True, slots=True)
class _ResolvedGroup:
    target: NestedResourceTarget
    decision: NestedRoutingDecision
    source_locations: tuple[str, ...]


def _candidate_groups(
    candidates: tuple[NestedResourceCandidate, ...],
) -> tuple[tuple[NestedResourceCandidate, tuple[str, ...]], ...]:
    grouped: dict[tuple[str, str], list[NestedResourceCandidate]] = {}
    for candidate in candidates:
        if not isinstance(candidate, NestedResourceCandidate):
            raise ValueError("discovery must return NestedResourceCandidate values")
        grouped.setdefault((candidate.kind.value, candidate.endpoint_digest), []).append(candidate)

    result: list[tuple[NestedResourceCandidate, tuple[str, ...]]] = []
    for key in sorted(grouped):
        members = grouped[key]
        representative = min(
            members,
            key=lambda item: (
                0 if item.access is NestedAccess.WRITE else 1,
                item.depth,
                item.source_location,
            ),
        )
        locations = tuple(sorted({item.source_location for item in members}))
        result.append((representative, locations))
    return tuple(result)


def _validate_resolved_target(
    candidate: NestedResourceCandidate,
    target: NestedResourceTarget,
) -> None:
    if not isinstance(target, NestedResourceTarget):
        raise _error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            "Nested target resolver returned an invalid target contract.",
        )
    if (
        target.kind is not candidate.kind
        or target.access is not candidate.access
        or target.endpoint_digest != candidate.endpoint_digest
    ):
        raise _error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            "Resolved nested target does not match its reviewed candidate.",
            details={"endpoint_digest": candidate.endpoint_digest},
        )


def _validate_child_lease(
    lease: AuthLease,
    *,
    target: NestedResourceTarget,
    decision: NestedRoutingDecision,
    context: OperationIdentityContext,
    now: str,
) -> AuthLease:
    if not isinstance(lease, AuthLease):
        raise _error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            "Nested lease provider returned an invalid lease contract.",
        )
    if lease.state is AuthLeaseState.REVOKED or lease.state is AuthLeaseState.RELEASED:
        raise _error(
            ErrorCode.CREDENTIAL_REVOKED,
            "Nested lease is not active.",
            details={"target_id": target.target_id},
        )
    if lease.state is AuthLeaseState.EXPIRED or _timestamp(lease.expires_at) <= _timestamp(now):
        raise _error(
            ErrorCode.CREDENTIAL_EXPIRED,
            "Nested lease expired before the operation identity could be bound.",
            details={"target_id": target.target_id},
        )
    if _timestamp(lease.issued_at) > _timestamp(now):
        raise _error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            "Nested lease issuance time is later than the binding decision.",
            details={"target_id": target.target_id},
        )
    mismatched = (
        lease.target_kind is not target.target_kind
        or lease.target_id != target.target_id
        or lease.provider is not target.provider
        or lease.profile_id != decision.profile_id
        or (target.repository_id is not None and lease.repository_id != target.repository_id)
        or lease.config_revision != context.config_revision
        or lease.policy_revision != context.policy_revision
    )
    if mismatched:
        raise _error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            "Nested lease scope differs from the exact routed target.",
            details={"target_id": target.target_id},
        )
    metadata = dict(lease.provider_metadata)
    observed_endpoint_digest = metadata.get("nested_endpoint_digest")
    if observed_endpoint_digest not in {None, target.endpoint_digest}:
        raise _error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            "Nested lease endpoint evidence differs from the reviewed target.",
            details={"target_id": target.target_id},
        )
    if observed_endpoint_digest is None:
        lease = replace(
            lease,
            provider_metadata=tuple(
                sorted(
                    (*lease.provider_metadata, ("nested_endpoint_digest", target.endpoint_digest))
                )
            ),
        )
    return lease


class NestedIdentityCoordinator:
    def __init__(
        self,
        *,
        discovery: NestedResourceDiscovery,
        resolver: NestedTargetResolver,
        leases: NestedLeaseProvider,
        identities: OperationIdentityManager,
        clock: Clock,
    ) -> None:
        self._discovery = discovery
        self._resolver = resolver
        self._leases = leases
        self._identities = identities
        self._clock = clock

    def prepare(
        self,
        request: NestedIdentityPreparationRequest,
    ) -> NestedIdentityPreparation:
        if not isinstance(request, NestedIdentityPreparationRequest):
            raise ValueError("request must be a NestedIdentityPreparationRequest")
        candidates = (
            *self._discovery.discover(request.discovery),
            *request.explicit_candidates,
        )
        approvals = _exact_map(
            request.cross_boundary_approvals,
            "cross_boundary_approvals",
        )
        intents = _exact_map(request.publication_intent_ids, "publication_intent_ids")

        resolved: list[_ResolvedGroup] = []
        target_ids: set[str] = set()
        target_keys: set[tuple[object, str]] = set()
        for candidate, locations in _candidate_groups(candidates):
            target = self._resolver.resolve(candidate)
            _validate_resolved_target(candidate, target)
            target_key = (target.target_kind, target.target_id)
            if target_key in target_keys or target.target_id in target_ids:
                raise _error(
                    ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                    "Multiple nested candidates resolved to the same target ambiguously.",
                    details={"target_id": target.target_id},
                )
            target_keys.add(target_key)
            target_ids.add(target.target_id)
            decision = route_nested_resource(
                target,
                allow_anonymous_public_read=request.allow_anonymous_public_read,
                exact_cross_boundary_approval_id=approvals.get(target.target_id),
                publication_intent_id=intents.get(target.target_id),
            )
            resolved.append(_ResolvedGroup(target, decision, locations))

        unknown_evidence = (set(approvals) | set(intents)) - target_ids
        if unknown_evidence:
            raise _error(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "Nested approval or publication intent references an undiscovered target.",
                details={"target_ids": sorted(unknown_evidence)},
            )

        for group in resolved:
            if group.decision.status is NestedRoutingStatus.DENIED:
                raise _error(
                    ErrorCode.SECURITY_POLICY_VIOLATION,
                    "Nested target routing was denied before lease acquisition.",
                    details={
                        "target_id": group.target.target_id,
                        "endpoint_digest": group.target.endpoint_digest,
                        "failure_code": (
                            group.decision.failure_code.value
                            if group.decision.failure_code is not None
                            else None
                        ),
                        "recovery_actions": [
                            action.payload() for action in group.decision.recovery_actions
                        ],
                    },
                )

        now = self._clock.now_iso()
        child_leases: list[AuthLease] = []
        child_requests: list[LeaseCapabilityRequest] = []
        receipts: list[NestedIdentityReceipt] = []
        existing_lease_ids = {item.lease_id for item in request.identity_context.auth_leases}
        existing_target_keys = {
            (item.target_kind, item.target_id) for item in request.identity_context.auth_leases
        }
        for group in resolved:
            target = group.target
            decision = group.decision
            if decision.status is NestedRoutingStatus.ANONYMOUS_READ:
                receipts.append(
                    NestedIdentityReceipt(
                        target_kind=target.target_kind,
                        target_id=target.target_id,
                        repository_id=target.repository_id,
                        endpoint_digest=target.endpoint_digest,
                        routing_status=decision.status,
                        profile_id=None,
                        lease_id=None,
                        capability_ids=(),
                        source_locations=group.source_locations,
                    )
                )
                continue
            if decision.profile_id is None:
                raise _error(
                    ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                    "Bound nested routing decision omitted its exact profile.",
                )
            lease = self._leases.acquire(
                operation_id=request.identity_context.operation_id,
                actor_class=request.identity_context.actor_class,
                target=target,
                profile_id=decision.profile_id,
                capability_ids=decision.capability_ids,
                config_revision=request.identity_context.config_revision,
                policy_revision=request.identity_context.policy_revision,
                now=now,
            )
            lease = _validate_child_lease(
                lease,
                target=target,
                decision=decision,
                context=request.identity_context,
                now=now,
            )
            target_key = (lease.target_kind, lease.target_id)
            if lease.lease_id in existing_lease_ids or target_key in existing_target_keys:
                raise _error(
                    ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                    "Nested lease collides with another operation identity target.",
                    details={"target_id": target.target_id},
                )
            existing_lease_ids.add(lease.lease_id)
            existing_target_keys.add(target_key)
            child_leases.append(lease)
            child_requests.append(
                LeaseCapabilityRequest(
                    lease_id=lease.lease_id,
                    capability_ids=decision.capability_ids,
                )
            )
            receipts.append(
                NestedIdentityReceipt(
                    target_kind=target.target_kind,
                    target_id=target.target_id,
                    repository_id=target.repository_id,
                    endpoint_digest=target.endpoint_digest,
                    routing_status=decision.status,
                    profile_id=decision.profile_id,
                    lease_id=lease.lease_id,
                    capability_ids=decision.capability_ids,
                    source_locations=group.source_locations,
                )
            )

        complete_context = replace(
            request.identity_context,
            auth_leases=(*request.identity_context.auth_leases, *child_leases),
        )
        complete_requests = (
            *request.primary_capability_requests,
            *child_requests,
        )
        record = self._identities.bind(
            complete_context,
            context_id=request.identity_context_id,
            capability_requests=complete_requests,
            now=now,
        )
        return NestedIdentityPreparation(
            record=record,
            context=record.context,
            capability_requests=record.capability_requests,
            receipts=tuple(receipts),
        )


__all__ = [
    "NestedIdentityCoordinator",
    "NestedIdentityPreparation",
    "NestedIdentityPreparationRequest",
]
