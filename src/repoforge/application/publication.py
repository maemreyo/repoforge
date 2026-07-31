"""Durable exact-intent publication coordination."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.errors import ConfigError, ErrorCode, RepoForgeError
from ..domain.git_transport_identity import GitTransportSpec
from ..domain.operation_identity import LeaseCapabilityRequest
from ..domain.operations import hash_idempotency_key
from ..domain.publication import RemoteTopology, ReviewedPublication
from ..domain.repository_auth_broker import ProcessAuthContext
from ..domain.repository_identity import (
    AuthTargetKind,
    OperationIdentityContext,
    PublicationIntent,
    PublicationKind,
)
from ..ports.publication import (
    PublicationAuthorization,
    PublicationEffect,
    PublicationGateway,
    PullRequestPublication,
)
from .idempotency import IdempotencyEffectBoundary

if TYPE_CHECKING:
    from .context import ApplicationContext
    from .operations.identity import OperationIdentityManager


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    workspace_id: str
    cwd: Path
    intent: PublicationIntent
    authorization: PublicationAuthorization
    identity_context: OperationIdentityContext
    identity_context_id: str
    capability_requests: tuple[LeaseCapabilityRequest, ...]
    capability_id: str
    transport_spec: GitTransportSpec
    auth_context: ProcessAuthContext
    idempotency_key: str
    pull_request: PullRequestPublication | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id is required")
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise ValueError("cwd must be an absolute Path")
        if self.intent.operation_id != self.identity_context.operation_id:
            raise ValueError("publication intent and identity context must share one operation ID")
        if self.authorization.lease not in self.identity_context.auth_leases:
            raise ValueError("authorization lease must be pinned by the operation identity context")
        if not self.capability_requests:
            raise ValueError("capability_requests must not be empty")
        if not self.capability_id:
            raise ValueError("capability_id is required")
        if self.transport_spec.profile_id != self.authorization.profile_id:
            raise ValueError("transport and API authorization profiles must match")
        if self.auth_context.profile_id != self.authorization.profile_id:
            raise ValueError("process auth and API authorization profiles must match")
        if self.auth_context.target_kind is not self.authorization.lease.target_kind:
            raise ValueError("process auth target kind must match the pinned lease")
        if self.auth_context.target_id != self.authorization.lease.target_id:
            raise ValueError("process auth target must match the pinned lease")
        if self.intent.kind is PublicationKind.PULL_REQUEST and self.pull_request is None:
            raise ValueError("pull-request publication requires reviewed title and body")
        if self.intent.kind is not PublicationKind.PULL_REQUEST and self.pull_request is not None:
            raise ValueError("non-PR publication cannot include pull-request content")
        if self.intent.kind is PublicationKind.RELEASE:
            lease = self.authorization.lease
            if lease.target_kind is not AuthTargetKind.RELEASE:
                raise ValueError("release publication requires an exact release-target lease")
            if (
                lease.repository_id != self.intent.destination_repository_id
                or lease.profile_id != self.authorization.profile_id
                or lease.config_revision != self.identity_context.config_revision
                or lease.policy_revision != self.identity_context.policy_revision
                or self.authorization.actor_class is not self.identity_context.actor_class
            ):
                raise ValueError("release lease must match the exact operation identity")
            if (
                not self.intent.source_ref.startswith("refs/tags/")
                or self.intent.destination_ref != self.intent.source_ref
                or self.intent.expected_tree_sha is None
            ):
                raise ValueError("release publication requires one exact tag, commit, and tree")
            if self.capability_id != "github.releases.write":
                raise ValueError("release publication requires github.releases.write")
            matching = tuple(
                item
                for item in self.capability_requests
                if item.lease_id == self.authorization.lease.lease_id
            )
            if len(matching) != 1 or matching[0].capability_ids != ("github.releases.write",):
                raise ValueError(
                    "release publication requires one exact release capability request"
                )


@dataclass(frozen=True, slots=True)
class _PublicationResult:
    publication_id: str
    kind: str
    operation_id: str
    source_repository_id: str
    destination_repository_id: str
    source_ref: str
    destination_ref: str
    commit_sha: str
    tree_sha: str
    profile_id: str
    actor_id: str
    installation_id: str | None
    lease_id: str
    topology_digest: str
    capability_digest: str
    permission_digest: str
    preflight_evidence_digest: str
    config_revision: str
    policy_revision: str
    remote_version: str
    review_digest: str
    external_id: str
    url: str | None
    reconciled: bool

    def safe_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "kind": self.kind,
            "operation_id": self.operation_id,
            "source_repository_id": self.source_repository_id,
            "destination_repository_id": self.destination_repository_id,
            "source_ref": self.source_ref,
            "destination_ref": self.destination_ref,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "profile_id": self.profile_id,
            "actor_id": self.actor_id,
            "installation_id": self.installation_id,
            "lease_id": self.lease_id,
            "topology_digest": self.topology_digest,
            "capability_digest": self.capability_digest,
            "permission_digest": self.permission_digest,
            "preflight_evidence_digest": self.preflight_evidence_digest,
            "config_revision": self.config_revision,
            "policy_revision": self.policy_revision,
            "remote_version": self.remote_version,
            "review_digest": self.review_digest,
            "external_id": self.external_id,
            "url": self.url,
            "reconciled": self.reconciled,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> _PublicationResult:
        if not isinstance(payload, dict):
            raise ValueError("publication result must be an object")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    publication_id: str
    kind: PublicationKind
    operation_id: str
    receipt_id: str
    result_reference: str
    source_repository_id: str
    destination_repository_id: str
    source_ref: str
    destination_ref: str
    commit_sha: str
    tree_sha: str
    preflight_evidence_digest: str
    review_digest: str
    external_id: str
    url: str | None
    reconciled: bool

    def safe_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "kind": self.kind.value,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "result_reference": self.result_reference,
            "source_repository_id": self.source_repository_id,
            "destination_repository_id": self.destination_repository_id,
            "source_ref": self.source_ref,
            "destination_ref": self.destination_ref,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "preflight_evidence_digest": self.preflight_evidence_digest,
            "review_digest": self.review_digest,
            "external_id": self.external_id,
            "url": self.url,
            "reconciled": self.reconciled,
        }


class PublicationCoordinator:
    """Compose identity leases, exact review, effect receipts, and reconciliation."""

    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        gateway: PublicationGateway,
        identities: OperationIdentityManager,
    ) -> None:
        self._ctx = ctx
        self._gateway = gateway
        self._identities = identities

    @staticmethod
    def _action(kind: PublicationKind) -> str:
        if kind is PublicationKind.GIT_PUSH:
            return "workspace_push"
        if kind is PublicationKind.PULL_REQUEST:
            return "workspace_create_draft_pr"
        if kind is PublicationKind.RELEASE:
            return "workspace_publish_release"
        raise RepoForgeError(
            "Publication kind is not supported by the durable coordinator",
            code=ErrorCode.PUBLICATION_TARGET_MISMATCH,
        )

    @staticmethod
    def _github_capability_ids(request: PublicationRequest) -> tuple[str, ...]:
        matching = tuple(
            item
            for item in request.capability_requests
            if item.lease_id == request.authorization.lease.lease_id
        )
        if len(matching) != 1:
            raise RepoForgeError(
                "Publication requires one exact capability request for its auth lease",
                code=ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                unchanged_state=("No external publication effect was started.",),
            )
        values = tuple(
            capability
            for capability in matching[0].capability_ids
            if capability.startswith("github.")
        )
        if not values or len(set(values)) != len(values):
            raise RepoForgeError(
                "Publication requires unique exact GitHub operation capabilities",
                code=ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
                unchanged_state=("No external publication effect was started.",),
            )
        return values

    @staticmethod
    def _request_payload(
        request: PublicationRequest,
        preflight: RemoteTopology,
    ) -> dict[str, object]:
        return {
            "workspace_id": request.workspace_id,
            "intent": request.intent.payload(),
            "preflight_topology_digest": preflight.topology_digest,
            "identity_context_id": request.identity_context_id,
            "capability_id": request.capability_id,
            "github_capability_ids": list(PublicationCoordinator._github_capability_ids(request)),
            "profile_id": request.authorization.profile_id,
            "lease_id": request.authorization.lease.lease_id,
            "capability_digest": request.authorization.capability_digest,
            "permission_digest": request.authorization.permission_digest,
            "remote_version": request.authorization.remote_version,
            "transport_repository_id": request.transport_spec.repository_id,
            "transport_target_id": request.transport_spec.target_id,
            "transport_kind": request.transport_spec.kind.value,
            "pull_request": (
                {
                    "title": request.pull_request.title,
                    "body_digest": __import__("hashlib")
                    .sha256(request.pull_request.body.encode("utf-8"))
                    .hexdigest(),
                }
                if request.pull_request is not None
                else None
            ),
        }

    @staticmethod
    def _details(request: PublicationRequest) -> dict[str, Any]:
        lease = request.authorization.lease
        return {
            "effect_identity_scope": "publication",
            "workspace_id": request.workspace_id,
            "publication_id": request.intent.publication_id,
            "source_repository_id": request.intent.source_repository_id,
            "destination_repository_id": request.intent.destination_repository_id,
            "source_ref": request.intent.source_ref,
            "destination_ref": request.intent.destination_ref,
            "commit_sha": request.intent.expected_commit_sha,
            "tree_sha": request.intent.expected_tree_sha,
            "profile_id": request.authorization.profile_id,
            "actor_id": request.authorization.actor_id,
            "installation_id": request.authorization.installation_id,
            "lease_id": lease.lease_id,
            "capability_digest": request.authorization.capability_digest,
            "permission_digest": request.authorization.permission_digest,
            "config_revision": request.identity_context.config_revision,
            "policy_revision": request.identity_context.policy_revision,
            "remote_version": request.authorization.remote_version,
        }

    @staticmethod
    def _result(
        request: PublicationRequest,
        reviewed: ReviewedPublication,
        effect: PublicationEffect,
    ) -> _PublicationResult:
        return _PublicationResult(
            publication_id=reviewed.publication_id,
            kind=reviewed.kind.value,
            operation_id=reviewed.operation_id,
            source_repository_id=reviewed.source_repository_id,
            destination_repository_id=reviewed.destination_repository_id,
            source_ref=request.intent.source_ref,
            destination_ref=request.intent.destination_ref,
            commit_sha=reviewed.commit_sha,
            tree_sha=reviewed.tree_sha,
            profile_id=reviewed.profile_id,
            actor_id=reviewed.actor_id,
            installation_id=reviewed.installation_id,
            lease_id=reviewed.lease_id,
            topology_digest=reviewed.topology_digest,
            capability_digest=reviewed.capability_digest,
            permission_digest=reviewed.permission_digest,
            preflight_evidence_digest=reviewed.preflight_evidence_digest,
            config_revision=request.identity_context.config_revision,
            policy_revision=request.identity_context.policy_revision,
            remote_version=reviewed.remote_version,
            review_digest=reviewed.review_digest,
            external_id=effect.external_id,
            url=effect.url,
            reconciled=effect.reconciled,
        )

    def execute(self, request: PublicationRequest) -> PublicationOutcome:
        preflight = self._gateway.inspect(request.cwd, request.intent)
        github_capability_ids = self._github_capability_ids(request)
        boundary = IdempotencyEffectBoundary()
        action = self._action(request.intent.kind)

        def mutate() -> _PublicationResult:
            now = self._ctx.clock.now_iso()
            record = self._identities.bind(
                request.identity_context,
                context_id=request.identity_context_id,
                capability_requests=request.capability_requests,
                now=now,
            )
            lease = self._identities.require_write(
                operation_id=request.intent.operation_id,
                reference=record.reference,
                target_kind=request.authorization.lease.target_kind,
                target_id=request.authorization.lease.target_id,
                capability_id=request.capability_id,
                now=now,
            )
            if lease != request.authorization.lease:
                raise RepoForgeError(
                    "Publication authorization lease changed before effect",
                    code=ErrorCode.OPERATION_IDENTITY_MISMATCH,
                    unchanged_state=("No external publication effect was started.",),
                )
            reviewed = self._gateway.revalidate(
                request.cwd,
                request.intent,
                preflight,
                request.authorization,
                requested_capability_ids=github_capability_ids,
                auth_context=request.auth_context,
                transport_spec=request.transport_spec,
            )
            boundary.begin()
            effect = self._gateway.publish(
                request.cwd,
                reviewed,
                preflight,
                transport_spec=request.transport_spec,
                auth_context=request.auth_context,
                pull_request=request.pull_request,
            )
            result = self._result(request, reviewed, effect)
            boundary.record_result(result)
            return result

        def reconcile() -> _PublicationResult:
            reviewed = self._gateway.revalidate(
                request.cwd,
                request.intent,
                preflight,
                request.authorization,
                requested_capability_ids=github_capability_ids,
                auth_context=request.auth_context,
                transport_spec=request.transport_spec,
            )
            effect = self._gateway.reconcile(
                request.cwd,
                reviewed,
                preflight,
                transport_spec=request.transport_spec,
                auth_context=request.auth_context,
            )
            if effect is None:
                raise ConfigError(
                    "EFFECT_OUTCOME_UNKNOWN: exact publication state could not be reconciled",
                    code=ErrorCode.EFFECT_OUTCOME_UNKNOWN,
                    retryable=False,
                    unchanged_state=(
                        "The original publication outcome remains unknown; no retry was started.",
                    ),
                )
            return self._result(request, reviewed, effect)

        result = self._ctx.idempotent(
            action,
            request.idempotency_key,
            self._request_payload(request, preflight),
            mutate,
            details=self._details(request),
            serialize=lambda value: value.safe_payload(),
            deserialize=_PublicationResult.from_payload,
            effect_boundary=boundary,
            reconcile_uncertain=reconcile,
            operation_id=request.intent.operation_id,
        )
        store = self._ctx.idempotency
        if store is None:
            raise ConfigError("Idempotency storage is not configured")
        record = store.load(action, hash_idempotency_key(request.idempotency_key))
        if record is None or record.operation_id is None or record.receipt_id is None:
            raise ConfigError("Durable publication outcome identity is missing")
        result_reference = f"operation-result:{record.operation_id}"
        return PublicationOutcome(
            publication_id=result.publication_id,
            kind=PublicationKind(result.kind),
            operation_id=record.operation_id,
            receipt_id=record.receipt_id,
            result_reference=result_reference,
            source_repository_id=result.source_repository_id,
            destination_repository_id=result.destination_repository_id,
            source_ref=result.source_ref,
            destination_ref=result.destination_ref,
            commit_sha=result.commit_sha,
            tree_sha=result.tree_sha,
            preflight_evidence_digest=result.preflight_evidence_digest,
            review_digest=result.review_digest,
            external_id=result.external_id,
            url=result.url,
            reconciled=result.reconciled,
        )


__all__ = ["PublicationCoordinator", "PublicationOutcome", "PublicationRequest"]
