from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.operation_identity import (
    LeaseCapabilityRequest,
    OperationIdentityReference,
)
from repoforge.domain.operations import IdempotencyRecord, IdempotencyState, hash_idempotency_key
from repoforge.domain.repository_auth_broker import ProcessAuthContext
from repoforge.domain.repository_identity import (
    ActorClass,
    AuthLease,
    AuthLeaseState,
    AuthTargetKind,
    IdentityEvidenceKind,
    IdentitySurface,
    IdentitySurfaceEvidence,
    OpaqueCredentialReference,
    OperationIdentityContext,
    PublicationIntent,
    PublicationKind,
    RepositoryProvider,
)

_publication = import_module("repoforge.domain.publication")
_publication_adapter = import_module("repoforge.adapters.publication")
_publication_application = import_module("repoforge.application.publication")
_publication_port = import_module("repoforge.ports.publication")
PublicationEvidence = _publication.PublicationEvidence
RemoteTopology = _publication.RemoteTopology
RepositoryEndpoint = _publication.RepositoryEndpoint
review_publication = _publication.review_publication

_OPERATION_ID = "op-" + "1" * 24
_PUBLICATION_ID = "publication-" + "2" * 24
_COMMIT_SHA = "3" * 40
_TREE_SHA = "4" * 40
_OTHER_TREE_SHA = "f" * 40
_CONFIG = "5" * 64
_POLICY = "6" * 64
_TOPOLOGY_URL = "7" * 64
_REWRITE = "8" * 64
_CAPABILITY = "9" * 64
_PERMISSION = "a" * 64
_REMOTE_VERSION = "b" * 64
_API_EVIDENCE = "c" * 64
_TRANSPORT_EVIDENCE = "d" * 64
_OBSERVED_AT = "2026-07-29T06:30:00+00:00"


def _endpoint(
    repository_id: str = "123456",
    *,
    canonical_name: str = "github.com/acme/widgets",
    boundary_id: str = "company",
    ref: str | None = None,
    url_digests: tuple[str, ...] = (_TOPOLOGY_URL,),
) -> RepositoryEndpoint:
    return RepositoryEndpoint(
        repository_id=repository_id,
        canonical_name=canonical_name,
        boundary_id=boundary_id,
        exact_ref=ref,
        url_digests=url_digests,
    )


def _topology(
    *,
    source_repository_id: str = "123456",
    destination_repository_id: str = "123456",
    source_boundary: str = "company",
    destination_boundary: str = "company",
    push_url_digests: tuple[str, ...] = (_TOPOLOGY_URL,),
    rewrite_digest: str = _REWRITE,
) -> RemoteTopology:
    source_ref = "refs/heads/ai/epic-284-publication"
    destination_ref = "refs/heads/ai/epic-284-publication"
    return RemoteTopology(
        remote_name="origin",
        fetch=_endpoint(
            source_repository_id,
            boundary_id=source_boundary,
            ref="refs/heads/main",
        ),
        push=_endpoint(
            destination_repository_id,
            canonical_name=(
                "github.com/acme/widgets"
                if destination_boundary == "company"
                else "github.com/personal/widgets-fork"
            ),
            boundary_id=destination_boundary,
            ref=destination_ref,
            url_digests=push_url_digests,
        ),
        base=_endpoint(
            source_repository_id,
            boundary_id=source_boundary,
            ref="refs/heads/main",
        ),
        head=_endpoint(
            destination_repository_id,
            canonical_name=(
                "github.com/acme/widgets"
                if destination_boundary == "company"
                else "github.com/personal/widgets-fork"
            ),
            boundary_id=destination_boundary,
            ref=source_ref,
            url_digests=push_url_digests,
        ),
        source_ref=source_ref,
        destination_ref=destination_ref,
        rewrite_digest=rewrite_digest,
        observed_at=_OBSERVED_AT,
    )


def _lease(
    *,
    repository_id: str = "123456",
    state: AuthLeaseState = AuthLeaseState.ACTIVE,
    expires_at: str = "2026-07-29T07:30:00+00:00",
    actor_id: str = "installation:84",
) -> AuthLease:
    return AuthLease(
        lease_id="lease-company-repository",
        profile_id="company-app",
        provider=RepositoryProvider.GITHUB,
        repository_id=repository_id,
        target_kind=AuthTargetKind.REPOSITORY,
        target_id=f"github-repository-{repository_id}",
        actor_id=actor_id,
        credential_ref=OpaqueCredentialReference("github-app", "company-app"),
        issued_at="2026-07-29T06:00:00+00:00",
        expires_at=expires_at,
        state=state,
        config_revision=_CONFIG,
        policy_revision=_POLICY,
        material_digest="e" * 64,
        provider_metadata=(("installation_id", "installation-84"),),
    )


def _surface(
    surface: IdentitySurface,
    *,
    repository_id: str = "123456",
    actor_id: str | None = "installation:84",
) -> IdentitySurfaceEvidence:
    is_actor = surface is IdentitySurface.GITHUB_API
    return IdentitySurfaceEvidence(
        surface=surface,
        evidence_kind=(
            IdentityEvidenceKind.VERIFIED_ACTOR
            if is_actor
            else IdentityEvidenceKind.TRANSPORT_ACCESS_PROOF
        ),
        repository_id=repository_id,
        profile_id="company-app",
        actor_id=actor_id if is_actor else None,
        target="github.com/acme/widgets",
        observed_at=_OBSERVED_AT,
        evidence_digest=_API_EVIDENCE if is_actor else _TRANSPORT_EVIDENCE,
    )


def _intent(
    *,
    source_repository_id: str = "123456",
    destination_repository_id: str = "123456",
    approval_id: str | None = None,
    expected_tree_sha: str = _TREE_SHA,
) -> PublicationIntent:
    return PublicationIntent(
        publication_id=_PUBLICATION_ID,
        operation_id=_OPERATION_ID,
        kind=PublicationKind.GIT_PUSH,
        source_repository_id=source_repository_id,
        destination_repository_id=destination_repository_id,
        remote_name="origin",
        source_ref="refs/heads/ai/epic-284-publication",
        destination_ref="refs/heads/ai/epic-284-publication",
        expected_commit_sha=_COMMIT_SHA,
        expected_tree_sha=expected_tree_sha,
        cross_boundary_approval_id=approval_id,
    )


def _evidence(
    *,
    preflight: RemoteTopology | None = None,
    observed: RemoteTopology | None = None,
    lease: AuthLease | None = None,
    commit_sha: str = _COMMIT_SHA,
    tree_sha: str = _TREE_SHA,
    observed_capability_digest: str = _CAPABILITY,
    observed_permission_digest: str = _PERMISSION,
    observed_remote_version: str = _REMOTE_VERSION,
    approved_cross_boundary_id: str | None = None,
    surfaces: tuple[IdentitySurfaceEvidence, ...] | None = None,
) -> PublicationEvidence:
    current = observed or _topology()
    return PublicationEvidence(
        operation_id=_OPERATION_ID,
        profile_id="company-app",
        actor_class=ActorClass.AUTONOMOUS_AGENT,
        actor_id="installation:84",
        installation_id="installation-84",
        lease=lease or _lease(repository_id=current.push.repository_id),
        identity_surfaces=surfaces
        or (
            _surface(IdentitySurface.GITHUB_API, repository_id=current.push.repository_id),
            _surface(IdentitySurface.GIT_PUSH, repository_id=current.push.repository_id),
        ),
        preflight_topology=preflight or current,
        observed_topology=current,
        observed_commit_sha=commit_sha,
        observed_tree_sha=tree_sha,
        expected_capability_digest=_CAPABILITY,
        observed_capability_digest=observed_capability_digest,
        expected_permission_digest=_PERMISSION,
        observed_permission_digest=observed_permission_digest,
        expected_remote_version=_REMOTE_VERSION,
        observed_remote_version=observed_remote_version,
        approved_cross_boundary_id=approved_cross_boundary_id,
        observed_at=_OBSERVED_AT,
    )


def test_review_publication_returns_digest_bound_safe_exact_intent() -> None:
    reviewed = review_publication(_intent(), _evidence())

    assert reviewed.publication_id == _PUBLICATION_ID
    assert reviewed.operation_id == _OPERATION_ID
    assert reviewed.destination_repository_id == "123456"
    assert reviewed.exact_refspec == (
        "refs/heads/ai/epic-284-publication:refs/heads/ai/epic-284-publication"
    )
    assert reviewed.commit_sha == _COMMIT_SHA
    assert reviewed.tree_sha == _TREE_SHA
    assert reviewed.lease_id == "lease-company-repository"
    assert reviewed.profile_id == "company-app"
    assert reviewed.actor_id == "installation:84"
    assert reviewed.installation_id == "installation-84"
    assert len(reviewed.review_digest) == 64
    rendered = repr(reviewed.payload())
    assert "credential_ref" not in rendered
    assert "token" not in rendered.lower()


@pytest.mark.parametrize(
    ("preflight", "observed", "code"),
    [
        (
            _topology(),
            _topology(destination_repository_id="999999"),
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
        ),
        (
            _topology(),
            _topology(rewrite_digest="f" * 64),
            ErrorCode.REMOTE_REWRITE_DETECTED,
        ),
        (
            _topology(),
            _topology(push_url_digests=(_TOPOLOGY_URL, "1" * 64)),
            ErrorCode.PUBLICATION_TARGET_MISMATCH,
        ),
    ],
)
def test_topology_or_target_drift_fails_before_publication(
    preflight: RemoteTopology,
    observed: RemoteTopology,
    code: ErrorCode,
) -> None:
    with pytest.raises(RepoForgeError) as failure:
        review_publication(_intent(), _evidence(preflight=preflight, observed=observed))
    assert failure.value.code is code
    assert failure.value.unchanged_state == ("No external publication effect was started.",)


def test_wrong_source_sha_permission_and_remote_version_fail_closed() -> None:
    cases = (
        (_evidence(commit_sha="f" * 40), ErrorCode.STALE_STATE),
        (
            _evidence(observed_permission_digest="f" * 64),
            ErrorCode.GITHUB_API_PERMISSION_DENIED,
        ),
        (
            _evidence(observed_remote_version="f" * 64),
            ErrorCode.PR_REMOTE_VERSION_STALE,
        ),
    )
    for evidence, code in cases:
        with pytest.raises(RepoForgeError) as failure:
            review_publication(_intent(), evidence)
        assert failure.value.code is code


def test_wrong_source_tree_sha_fails_closed() -> None:
    with pytest.raises(RepoForgeError) as failure:
        review_publication(_intent(), _evidence(tree_sha=_OTHER_TREE_SHA))
    assert failure.value.code is ErrorCode.STALE_STATE


@pytest.mark.parametrize(
    ("source_ref", "destination_ref"),
    [
        ("HEAD", "refs/heads/main"),
        ("refs/heads/main", "HEAD"),
        ("refs/heads/*", "refs/heads/main"),
        ("refs/heads/main", "refs/heads/*"),
        ("--all", "refs/heads/main"),
        ("--mirror", "refs/heads/main"),
        ("refs/heads/main", ":refs/heads/main"),
    ],
)
def test_ambiguous_or_non_exact_managed_refspec_is_rejected(
    source_ref: str,
    destination_ref: str,
) -> None:
    intent = replace(
        _intent(),
        source_ref=source_ref,
        destination_ref=destination_ref,
    )
    topology = replace(
        _topology(),
        source_ref=source_ref,
        destination_ref=destination_ref,
    )
    with pytest.raises((RepoForgeError, ValueError)):
        review_publication(intent, _evidence(preflight=topology, observed=topology))


def test_expired_or_mismatched_lease_cannot_be_consumed() -> None:
    expired = _lease(expires_at="2026-07-29T06:15:00+00:00")
    with pytest.raises(RepoForgeError) as expiry:
        review_publication(_intent(), _evidence(lease=expired))
    assert expiry.value.code is ErrorCode.CREDENTIAL_EXPIRED

    wrong_actor = _lease(actor_id="installation:999")
    with pytest.raises(RepoForgeError) as mismatch:
        review_publication(_intent(), _evidence(lease=wrong_actor))
    assert mismatch.value.code is ErrorCode.OPERATION_IDENTITY_MISMATCH


def test_cross_boundary_publication_requires_the_same_reviewed_approval() -> None:
    intent = _intent(
        source_repository_id="123456",
        destination_repository_id="987654",
        approval_id="approval-company-to-personal",
    )
    topology = _topology(
        source_repository_id="123456",
        destination_repository_id="987654",
        source_boundary="company",
        destination_boundary="personal",
    )
    evidence = _evidence(preflight=topology, observed=topology)

    with pytest.raises(RepoForgeError) as denied:
        review_publication(intent, evidence)
    assert denied.value.code is ErrorCode.CROSS_BOUNDARY_PUBLICATION_DENIED

    reviewed = review_publication(
        intent,
        replace(evidence, approved_cross_boundary_id="approval-company-to-personal"),
    )
    assert reviewed.cross_boundary_approval_id == "approval-company-to-personal"


def test_actor_and_transport_surface_evidence_are_both_required() -> None:
    for surfaces in (
        (_surface(IdentitySurface.GITHUB_API),),
        (_surface(IdentitySurface.GIT_PUSH),),
    ):
        with pytest.raises(RepoForgeError) as failure:
            review_publication(_intent(), _evidence(surfaces=surfaces))
        assert failure.value.code is ErrorCode.EVIDENCE_INVALID


class _CoordinatorIdempotency:
    def __init__(self) -> None:
        self.record: IdempotencyRecord | None = None

    def load(self, action: str, key_hash: str) -> IdempotencyRecord | None:
        if self.record is None:
            return None
        assert self.record.action == action
        assert self.record.key_hash == key_hash
        return self.record


class _CoordinatorContext:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.idempotency = _CoordinatorIdempotency()
        self.boundary = None
        self.clock = SimpleNamespace(now_iso=lambda: _OBSERVED_AT)

    def idempotent(self, action, key, request, operation, **kwargs):
        self.events.append("idempotent")
        self.boundary = kwargs["effect_boundary"]
        result = operation()
        self.idempotency.record = IdempotencyRecord(
            action=action,
            key_hash=hash_idempotency_key(key),
            request_fingerprint="f" * 64,
            state=IdempotencyState.COMPLETED,
            updated_at=_OBSERVED_AT,
            updated_at_epoch=0.0,
            correlation_id="correlation-publication",
            result=result.safe_payload(),
            receipt_id="receipt-" + "a" * 24,
            operation_id=kwargs["operation_id"],
        )
        return result


class _CoordinatorIdentities:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def bind(self, context, *, context_id, capability_requests, now):
        self.events.append("bind")
        assert context.operation_id == _OPERATION_ID
        assert context_id == "identity-" + "e" * 24
        assert capability_requests[0].capability_ids == ("git.push",)
        return SimpleNamespace(
            reference=OperationIdentityReference("identity-" + "e" * 24, "f" * 64)
        )

    def require_write(self, **kwargs):
        self.events.append("require_write")
        assert kwargs["operation_id"] == _OPERATION_ID
        assert kwargs["capability_id"] == "git.push"
        return _lease()


class _CoordinatorGateway:
    def __init__(self, events: list[str], *, fail_revalidation: bool = False) -> None:
        self.events = events
        self.fail_revalidation = fail_revalidation

    def inspect(self, cwd: Path, intent: PublicationIntent):
        self.events.append("inspect")
        assert cwd == Path("/workspace")
        assert intent == _intent()
        return _topology()

    def revalidate(self, cwd, intent, preflight, expected_authorization):
        self.events.append("revalidate")
        if self.fail_revalidation:
            raise RepoForgeError("topology drift", code=ErrorCode.PUBLICATION_TARGET_MISMATCH)
        return review_publication(intent, _evidence(preflight=preflight, observed=preflight))

    def publish(self, cwd, reviewed, topology, **kwargs):
        self.events.append("publish")
        assert reviewed.operation_id == _OPERATION_ID
        return _publication_port.PublicationEffect(
            publication_id=reviewed.publication_id,
            kind=reviewed.kind,
            destination_repository_id=reviewed.destination_repository_id,
            destination_ref=topology.destination_ref,
            commit_sha=reviewed.commit_sha,
            external_id="effect-publication-292",
            url=None,
            reconciled=False,
        )

    def reconcile(self, *args, **kwargs):
        self.events.append("reconcile")
        return None


def _coordinator_request():
    lease = _lease()
    authorization = _publication_port.PublicationAuthorization(
        profile_id="company-app",
        actor_class=ActorClass.AUTONOMOUS_AGENT,
        actor_id="installation:84",
        installation_id="installation-84",
        lease=lease,
        identity_surfaces=(
            _surface(IdentitySurface.GITHUB_API),
            _surface(IdentitySurface.GIT_PUSH),
        ),
        capability_digest=_CAPABILITY,
        permission_digest=_PERMISSION,
        remote_version=_REMOTE_VERSION,
        observed_at=_OBSERVED_AT,
        approved_cross_boundary_id=None,
    )
    return _publication_application.PublicationRequest(
        workspace_id="workspace-publication",
        cwd=Path("/workspace"),
        intent=_intent(),
        authorization=authorization,
        identity_context=OperationIdentityContext(
            operation_id=_OPERATION_ID,
            primary_repository_id="123456",
            actor_class=ActorClass.AUTONOMOUS_AGENT,
            auth_leases=(lease,),
            selected_at=_OBSERVED_AT,
            config_revision=_CONFIG,
            policy_revision=_POLICY,
        ),
        identity_context_id="identity-" + "e" * 24,
        capability_requests=(LeaseCapabilityRequest("lease-company-repository", ("git.push",)),),
        capability_id="git.push",
        transport_spec=GitTransportSpec(
            profile_id="company-app",
            repository_id="123456",
            target_id="github-repository-123456",
            provider_host="github.com",
            kind=GitTransportKind.SSH,
            credential_fingerprint="f" * 64,
            allowed_access=(GitTransportAccess.WRITE,),
            ssh_identity_file="/identity/company-app",
        ),
        auth_context=ProcessAuthContext(
            profile_id="company-app",
            material_id="material-company-app",
            target_kind=AuthTargetKind.REPOSITORY,
            target_id="github-repository-123456",
            environment=(),
        ),
        idempotency_key="publication-coordinator-key-292",
    )


def test_publication_coordinator_orders_identity_review_before_effect() -> None:
    events: list[str] = []
    ctx = _CoordinatorContext(events)
    coordinator = _publication_application.PublicationCoordinator(
        ctx,
        gateway=_CoordinatorGateway(events),
        identities=_CoordinatorIdentities(events),
    )

    outcome = coordinator.execute(_coordinator_request())

    assert events == ["inspect", "idempotent", "bind", "require_write", "revalidate", "publish"]
    assert ctx.boundary.started is True
    assert ctx.boundary.authoritative_result is not None
    assert outcome.operation_id == _OPERATION_ID
    assert outcome.receipt_id == "receipt-" + "a" * 24
    assert outcome.result_reference == f"operation-result:{_OPERATION_ID}"


def test_publication_coordinator_stops_before_boundary_on_revalidation_drift() -> None:
    events: list[str] = []
    ctx = _CoordinatorContext(events)
    coordinator = _publication_application.PublicationCoordinator(
        ctx,
        gateway=_CoordinatorGateway(events, fail_revalidation=True),
        identities=_CoordinatorIdentities(events),
    )

    with pytest.raises(RepoForgeError) as failure:
        coordinator.execute(_coordinator_request())

    assert failure.value.code is ErrorCode.PUBLICATION_TARGET_MISMATCH
    assert events == ["inspect", "idempotent", "bind", "require_write", "revalidate"]
    assert ctx.boundary.started is False
