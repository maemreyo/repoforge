from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_operation_identity_store import JsonOperationIdentityStore
from repoforge.application.nested_identity import (
    NestedIdentityCoordinator,
    NestedIdentityPreparationRequest,
)
from repoforge.application.operations.identity import OperationIdentityManager
from repoforge.domain.durable_state import Revision
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.nested_identity import (
    NestedAccess,
    NestedBindingState,
    NestedResourceCandidate,
    NestedResourceKind,
    NestedResourceTarget,
    NestedRoutingStatus,
    canonical_nested_endpoint,
    nested_endpoint_digest,
)
from repoforge.domain.operation_identity import LeaseCapabilityRequest
from repoforge.domain.operation_task import new_operation_task
from repoforge.domain.repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    OpaqueCredentialReference,
    OperationIdentityContext,
    RepositoryProvider,
)
from repoforge.ports.nested_identity import NestedDiscoveryRequest
from repoforge.testing.fakes import FixedClock, InMemoryLockManager, InMemoryOperationStore

_OPERATION_ID = "op-" + "4" * 24
_CONTEXT_ID = "identity-" + "5" * 24
_CONFIG = "a" * 64
_POLICY = "b" * 64
_NOW = "2026-07-30T00:00:00+00:00"


class StaticDiscovery:
    def __init__(self, candidates: tuple[NestedResourceCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls = 0

    def discover(self, request: NestedDiscoveryRequest) -> tuple[NestedResourceCandidate, ...]:
        del request
        self.calls += 1
        return self.candidates


class MappingResolver:
    def __init__(self, targets: dict[str, NestedResourceTarget]) -> None:
        self.targets = targets
        self.calls: list[NestedResourceCandidate] = []

    def resolve(self, candidate: NestedResourceCandidate) -> NestedResourceTarget:
        self.calls.append(candidate)
        return self.targets[candidate.endpoint_digest]


class RecordingLeases:
    def __init__(self, responses: dict[str, AuthLease]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def acquire(
        self,
        *,
        operation_id: str,
        actor_class: ActorClass,
        target: NestedResourceTarget,
        profile_id: str,
        capability_ids: tuple[str, ...],
        config_revision: str,
        policy_revision: str,
        now: str,
    ) -> AuthLease:
        self.calls.append(
            {
                "operation_id": operation_id,
                "actor_class": actor_class,
                "target": target,
                "profile_id": profile_id,
                "capability_ids": capability_ids,
                "config_revision": config_revision,
                "policy_revision": policy_revision,
                "now": now,
            }
        )
        return self.responses[target.target_id]


def _candidate(
    endpoint: str,
    source: str,
    *,
    kind: NestedResourceKind = NestedResourceKind.SUBMODULE,
    access: NestedAccess = NestedAccess.READ,
    depth: int = 1,
) -> NestedResourceCandidate:
    canonical = canonical_nested_endpoint(endpoint)
    return NestedResourceCandidate(
        kind=kind,
        access=access,
        canonical_endpoint=canonical,
        source_location=source,
        depth=depth,
        endpoint_digest=nested_endpoint_digest(canonical),
    )


def _target(
    candidate: NestedResourceCandidate,
    target_id: str,
    *,
    repository_id: str | None = "654321",
    profile_id: str | None = "dependency-reader",
    binding_state: NestedBindingState = NestedBindingState.EXACT,
    public_read: bool = False,
    owner_boundary: str = "company",
    capabilities: tuple[str, ...] = ("git_fetch",),
) -> NestedResourceTarget:
    return NestedResourceTarget(
        kind=candidate.kind,
        access=candidate.access,
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        target_kind={
            NestedResourceKind.SUBMODULE: AuthTargetKind.SUBMODULE,
            NestedResourceKind.LFS: AuthTargetKind.LFS,
            NestedResourceKind.PACKAGE: AuthTargetKind.PACKAGE,
            NestedResourceKind.RELEASE: AuthTargetKind.RELEASE,
        }[candidate.kind],
        target_id=target_id,
        repository_id=repository_id,
        owner_boundary=owner_boundary,
        primary_owner_boundary="company",
        capability_ids=capabilities,
        endpoint_digest=candidate.endpoint_digest,
        binding_state=binding_state,
        profile_id=profile_id,
        public_read=public_read,
    )


def _lease(
    target: NestedResourceTarget | None = None,
    *,
    lease_id: str = "lease-primary",
    profile_id: str = "company",
    repository_id: str = "123456",
    target_kind: AuthTargetKind = AuthTargetKind.REPOSITORY,
    target_id: str = "github-repository-123456",
    provider: RepositoryProvider = RepositoryProvider.GITHUB,
    state: AuthLeaseState = AuthLeaseState.ACTIVE,
    config_revision: str = _CONFIG,
    policy_revision: str = _POLICY,
    expires_at: str = "2026-07-30T02:00:00+00:00",
) -> AuthLease:
    if target is not None:
        profile_id = target.profile_id or "anonymous-is-invalid"
        repository_id = target.repository_id or "nested-non-repository"
        target_kind = target.target_kind
        target_id = target.target_id
        lease_id = f"lease-{target_id}"
        provider = target.provider
    return AuthLease(
        lease_id=lease_id,
        profile_id=profile_id,
        provider=provider,
        repository_id=repository_id,
        target_kind=target_kind,
        target_id=target_id,
        actor_id="github-app-42",
        credential_ref=OpaqueCredentialReference("github-app", f"ref-{profile_id}"),
        issued_at=_NOW,
        expires_at=expires_at,
        state=state,
        config_revision=config_revision,
        policy_revision=policy_revision,
        material_digest="c" * 64,
    )


def _context() -> OperationIdentityContext:
    return OperationIdentityContext(
        operation_id=_OPERATION_ID,
        primary_repository_id="123456",
        actor_class=ActorClass.AUTONOMOUS_AGENT,
        auth_leases=(_lease(),),
        selected_at=_NOW,
        config_revision=_CONFIG,
        policy_revision=_POLICY,
    )


def _manager(tmp_path: Path) -> tuple[OperationIdentityManager, JsonOperationIdentityStore]:
    operations = InMemoryOperationStore()
    operations.create(
        new_operation_task(
            operation_id=_OPERATION_ID,
            kind="nested_identity",
            phase="queued",
            now=_NOW,
            cancel_supported=True,
        )
    )
    store = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    return OperationIdentityManager(operations=operations, identities=store), store


def _discovery_request(tmp_path: Path) -> NestedDiscoveryRequest:
    return NestedDiscoveryRequest(
        root=tmp_path.resolve(),
        primary_endpoint="https://github.com/acme/platform.git",
    )


def _prepare_request(
    tmp_path: Path,
    **changes: object,
) -> NestedIdentityPreparationRequest:
    values: dict[str, object] = {
        "identity_context": _context(),
        "identity_context_id": _CONTEXT_ID,
        "primary_capability_requests": (LeaseCapabilityRequest("lease-primary", ("git_push",)),),
        "discovery": _discovery_request(tmp_path),
    }
    values.update(changes)
    return NestedIdentityPreparationRequest(**values)  # type: ignore[arg-type]


def _coordinator(
    tmp_path: Path,
    candidates: tuple[NestedResourceCandidate, ...],
    targets: tuple[NestedResourceTarget, ...],
    *,
    lease_responses: dict[str, AuthLease] | None = None,
) -> tuple[
    NestedIdentityCoordinator,
    RecordingLeases,
    JsonOperationIdentityStore,
    OperationIdentityManager,
]:
    manager, store = _manager(tmp_path)
    leases = RecordingLeases(
        lease_responses
        or {
            target.target_id: _lease(target)
            for target in targets
            if target.binding_state is NestedBindingState.EXACT
        }
    )
    coordinator = NestedIdentityCoordinator(
        discovery=StaticDiscovery(candidates),
        resolver=MappingResolver({target.endpoint_digest: target for target in targets}),
        leases=leases,
        identities=manager,
        clock=FixedClock(_NOW),
    )
    return coordinator, leases, store, manager


def test_composes_distinct_child_leases_deduplicates_sources_and_keeps_primary(
    tmp_path: Path,
) -> None:
    first = _candidate("https://github.com/acme/sdk.git", "z/.gitmodules:sdk")
    duplicate = replace(first, source_location="a/.gitmodules:sdk", depth=2)
    public = _candidate(
        "https://github.com/public/docs.git",
        "packages:docs",
        kind=NestedResourceKind.PACKAGE,
        depth=0,
    )
    private_target = _target(first, "submodule-sdk")
    public_target = _target(
        public,
        "package-docs",
        repository_id=None,
        profile_id=None,
        binding_state=NestedBindingState.MISSING,
        public_read=True,
        capabilities=("package_read",),
    )
    coordinator, leases, store, _ = _coordinator(
        tmp_path,
        (first, duplicate),
        (private_target, public_target),
    )

    prepared = coordinator.prepare(
        _prepare_request(
            tmp_path,
            explicit_candidates=(public,),
            allow_anonymous_public_read=True,
        )
    )

    assert len(leases.calls) == 1
    assert prepared.context.auth_leases[0] == _context().auth_leases[0]
    assert (
        dict(prepared.context.auth_leases[1].provider_metadata)["nested_endpoint_digest"]
        == private_target.endpoint_digest
    )
    assert prepared.capability_requests[0] == LeaseCapabilityRequest("lease-primary", ("git_push",))
    assert prepared.record.context == prepared.context
    assert prepared.record.capability_requests == prepared.capability_requests
    assert [receipt.routing_status for receipt in prepared.receipts] == [
        NestedRoutingStatus.ANONYMOUS_READ,
        NestedRoutingStatus.BOUND_PROFILE,
    ]
    bound = next(
        receipt
        for receipt in prepared.receipts
        if receipt.routing_status is NestedRoutingStatus.BOUND_PROFILE
    )
    assert bound.source_locations == ("a/.gitmodules:sdk", "z/.gitmodules:sdk")
    anonymous = next(
        receipt
        for receipt in prepared.receipts
        if receipt.routing_status is NestedRoutingStatus.ANONYMOUS_READ
    )
    assert anonymous.lease_id is None
    restarted = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    assert restarted.read(_OPERATION_ID) == store.read(_OPERATION_ID)
    rendered = json.dumps(prepared.record.safe_payload(), sort_keys=True).lower()
    assert "token" not in rendered
    assert "authorization" not in rendered


@pytest.mark.parametrize(
    "binding_state",
    (
        NestedBindingState.MISSING,
        NestedBindingState.AMBIGUOUS,
        NestedBindingState.STALE,
        NestedBindingState.DISABLED,
        NestedBindingState.TRANSFERRED,
    ),
)
def test_any_denied_target_aborts_before_all_lease_acquisition(
    tmp_path: Path,
    binding_state: NestedBindingState,
) -> None:
    admitted = _candidate("https://github.com/acme/aaa.git", "submodules:aaa")
    denied = _candidate("https://github.com/acme/zzz.git", "submodules:zzz")
    admitted_target = _target(admitted, "submodule-aaa")
    denied_target = _target(
        denied,
        "submodule-zzz",
        profile_id=None,
        binding_state=binding_state,
    )
    coordinator, leases, store, _ = _coordinator(
        tmp_path,
        (admitted, denied),
        (admitted_target, denied_target),
    )

    with pytest.raises(RepoForgeError) as failure:
        coordinator.prepare(_prepare_request(tmp_path))

    assert failure.value.code is ErrorCode.SECURITY_POLICY_VIOLATION
    assert leases.calls == []
    assert store.read(_OPERATION_ID) is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("target_id", "submodule-other"),
        ("target_kind", AuthTargetKind.LFS),
        ("repository_id", "999999"),
        ("profile_id", "personal-profile"),
        ("provider", object()),
        ("provider_metadata", (("nested_endpoint_digest", "f" * 64),)),
        ("config_revision", "d" * 64),
        ("policy_revision", "e" * 64),
        ("state", AuthLeaseState.REVOKED),
        ("state", AuthLeaseState.EXPIRED),
    ),
)
def test_rejects_child_lease_scope_or_lifecycle_mismatch_before_bind(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    candidate = _candidate("https://github.com/acme/sdk.git", "submodules:sdk")
    target = _target(candidate, "submodule-sdk")
    valid = _lease(target)
    if field == "provider":
        invalid = replace(valid, provider=RepositoryProvider.GITHUB)
        invalid = object.__new__(AuthLease)
        for name in valid.__dataclass_fields__:
            object.__setattr__(invalid, name, getattr(valid, name))
        object.__setattr__(invalid, "provider", value)
    else:
        invalid = replace(valid, **{field: value})
    coordinator, leases, store, _ = _coordinator(
        tmp_path,
        (candidate,),
        (target,),
        lease_responses={target.target_id: invalid},
    )

    with pytest.raises(RepoForgeError) as failure:
        coordinator.prepare(_prepare_request(tmp_path))

    assert failure.value.code in {
        ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
        ErrorCode.CREDENTIAL_EXPIRED,
        ErrorCode.CREDENTIAL_REVOKED,
    }
    assert len(leases.calls) == 1
    assert store.read(_OPERATION_ID) is None


@pytest.mark.parametrize("decision_change", ("target", "endpoint", "profile", "capability"))
def test_retry_is_idempotent_but_changed_nested_decision_is_rejected(
    tmp_path: Path,
    decision_change: str,
) -> None:
    candidate = _candidate("https://github.com/acme/sdk.git", "submodules:sdk")
    target = _target(candidate, "submodule-sdk")
    coordinator, leases, _, manager = _coordinator(tmp_path, (candidate,), (target,))
    request = _prepare_request(tmp_path)

    first = coordinator.prepare(request)
    second = coordinator.prepare(request)

    assert second.record == first.record
    assert second.context == first.context
    assert len(leases.calls) == 2

    changed_candidate = candidate
    changed_target = target
    if decision_change == "target":
        changed_target = replace(target, target_id="submodule-other")
    elif decision_change == "endpoint":
        changed_candidate = _candidate(
            "https://github.com/acme/sdk-mirror.git",
            "submodules:sdk",
        )
        changed_target = replace(
            target,
            endpoint_digest=changed_candidate.endpoint_digest,
        )
    elif decision_change == "profile":
        changed_target = replace(target, profile_id="another-profile")
    else:
        changed_target = replace(target, capability_ids=("git_fetch", "git_push"))
    changed_coordinator = NestedIdentityCoordinator(
        discovery=StaticDiscovery((changed_candidate,)),
        resolver=MappingResolver({changed_candidate.endpoint_digest: changed_target}),
        leases=RecordingLeases({changed_target.target_id: _lease(changed_target)}),
        identities=manager,
        clock=FixedClock(_NOW),
    )
    with pytest.raises(RepoForgeError) as changed:
        changed_coordinator.prepare(request)
    assert changed.value.code is ErrorCode.OPERATION_IDENTITY_MISMATCH


def test_approval_and_intent_maps_are_exact_unique_and_discovered(tmp_path: Path) -> None:
    candidate = _candidate("https://github.com/acme/sdk.git", "submodules:sdk")
    target = _target(candidate, "submodule-sdk")
    coordinator, _, _, _ = _coordinator(tmp_path, (candidate,), (target,))

    for changes in (
        {"cross_boundary_approvals": (("submodule-sdk", "approval-1"),) * 2},
        {"publication_intent_ids": (("submodule-sdk", "intent-1"),) * 2},
    ):
        with pytest.raises(ValueError):
            _prepare_request(tmp_path, **changes)

    with pytest.raises(RepoForgeError) as unknown:
        coordinator.prepare(
            _prepare_request(
                tmp_path,
                cross_boundary_approvals=(("unknown-target", "approval-1"),),
            )
        )
    assert unknown.value.code is ErrorCode.SECURITY_POLICY_VIOLATION


def test_bound_child_lease_enforces_exact_capability_and_revocation(tmp_path: Path) -> None:
    candidate = _candidate(
        "https://github.com/acme/sdk.git",
        "submodules:sdk",
        access=NestedAccess.WRITE,
    )
    target = _target(candidate, "submodule-sdk", capabilities=("git_push",))
    coordinator, _, store, manager = _coordinator(tmp_path, (candidate,), (target,))
    prepared = coordinator.prepare(
        _prepare_request(
            tmp_path,
            publication_intent_ids=(("submodule-sdk", "publication-1"),),
        )
    )

    assert (
        manager.require_write(
            operation_id=_OPERATION_ID,
            reference=prepared.record.reference,
            target_kind=AuthTargetKind.SUBMODULE,
            target_id="submodule-sdk",
            capability_id="git_push",
            now="2026-07-30T01:00:00+00:00",
        ).target_id
        == "submodule-sdk"
    )
    with pytest.raises(RepoForgeError) as capability:
        manager.require_write(
            operation_id=_OPERATION_ID,
            reference=prepared.record.reference,
            target_kind=AuthTargetKind.SUBMODULE,
            target_id="submodule-sdk",
            capability_id="package_write",
            now="2026-07-30T01:00:00+00:00",
        )
    assert capability.value.code is ErrorCode.CREDENTIAL_CAPABILITY_DENIED

    manager.revoke(
        _OPERATION_ID,
        expected_revision=Revision(1),
        lease_id="lease-submodule-sdk",
        now="2026-07-30T01:10:00+00:00",
    )
    assert store.read(_OPERATION_ID) is not None
    with pytest.raises(RepoForgeError) as revoked:
        manager.require_write(
            operation_id=_OPERATION_ID,
            reference=prepared.record.reference,
            target_kind=AuthTargetKind.SUBMODULE,
            target_id="submodule-sdk",
            capability_id="git_push",
            now="2026-07-30T01:20:00+00:00",
        )
    assert revoked.value.code is ErrorCode.CREDENTIAL_REVOKED
