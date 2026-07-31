"""Scoped production workspace publication request construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...config import AppConfig, AuthProfileConfig
from ...domain.auth_profile import AuthProfileSelector
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.github_capability_preflight import (
    GitHubOperationCapability,
    github_capability_requirements,
)
from ...domain.operation_identity import LeaseCapabilityRequest
from ...domain.repository_identity import (
    AuthLease,
    AuthTargetKind,
    IdentityEvidenceKind,
    IdentitySurface,
    IdentitySurfaceEvidence,
    OperationIdentityContext,
    PublicationIntent,
    PublicationKind,
)
from ...ports.clock import Clock
from ...ports.command import CommandExecutor
from ...ports.ids import IdGenerator
from ...ports.publication import PublicationAuthorization, PullRequestPublication
from ...ports.workspace_publication import (
    WorkspaceDraftPrPublication,
    WorkspacePushPublication,
)
from ..publication import PublicationOutcome, PublicationRequest
from ..repository_identity_runtime import (
    RepositoryIdentityAdmission,
    RepositoryIdentityRuntime,
)

_GITHUB_CONTENTS_WRITE = "github.contents.write"
_GITHUB_PULL_REQUESTS_WRITE = "github.pull_requests.write"
_GIT_PUSH = "git.push"


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error(code: ErrorCode, message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No external publication effect was started.",),
        safe_next_action="Resolve and bind the exact repository identity, then retry.",
    )


def _metadata(lease: AuthLease, name: str) -> str:
    value = dict(lease.provider_metadata).get(name)
    if not isinstance(value, str) or not value:
        raise _error(
            ErrorCode.EVIDENCE_INVALID,
            f"Publication auth lease is missing pinned {name} evidence.",
        )
    return value


@dataclass(frozen=True, slots=True)
class _PublicationShape:
    kind: PublicationKind
    source_ref: str
    destination_ref: str
    head_sha: str
    tree_sha: str
    capability_id: str
    broker_capabilities: tuple[str, ...]
    operation_capabilities: tuple[str, ...]
    base_ref: str | None = None
    head_ref: str | None = None
    pull_request: PullRequestPublication | None = None
    remote_head: str | None = None
    remote_version_ref: str | None = None


class ScopedWorkspacePublicationRequestFactory:
    """Create and consume one publication request inside the selected broker session."""

    def __init__(
        self,
        *,
        config: AppConfig,
        runtime: RepositoryIdentityRuntime,
        commands: CommandExecutor,
        clock: Clock,
        ids: IdGenerator,
        config_revision: str,
        policy_revision: str,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._commands = commands
        self._clock = clock
        self._ids = ids
        self._config_revision = config_revision
        self._policy_revision = policy_revision

    def _configured(self, admission: RepositoryIdentityAdmission) -> AuthProfileConfig:
        configured = self._config.auth_profiles.get(admission.profile.profile_id)
        if configured is None or configured.profile != admission.profile:
            raise _error(
                ErrorCode.CONFIG_STALE,
                "Selected publication profile no longer matches active configuration.",
            )
        return configured

    @staticmethod
    def _remote_version(
        *,
        repository_id: str,
        destination_ref: str,
        remote_head: str | None,
    ) -> str:
        return _digest(
            {
                "repository_id": repository_id,
                "destination_ref": destination_ref,
                "remote_head": remote_head,
            }
        )

    @staticmethod
    def _operation_digests(
        capability_ids: tuple[str, ...],
    ) -> tuple[str, str]:
        capabilities = tuple(sorted(GitHubOperationCapability(value) for value in capability_ids))
        requirements = github_capability_requirements()
        permissions = tuple(
            sorted({requirements[capability].permission_id for capability in capabilities})
        )
        return (
            _digest([capability.value for capability in capabilities]),
            _digest(list(permissions)),
        )

    def _authorization(
        self,
        *,
        admission: RepositoryIdentityAdmission,
        lease: AuthLease,
        remote_version: str,
        observed_at: str,
        effect_surface: IdentitySurface,
        github_capability_ids: tuple[str, ...],
    ) -> PublicationAuthorization:
        actor_id = lease.actor_id
        if actor_id is None:
            raise _error(
                ErrorCode.GITHUB_API_ACTOR_MISMATCH,
                "Publication requires one verified GitHub actor.",
            )
        api_evidence = _metadata(lease, "github_preflight_evidence_digest")
        _metadata(lease, "github_capability_digest")
        _metadata(lease, "github_permission_digest")
        capability_digest, permission_digest = self._operation_digests(github_capability_ids)
        transport_evidence = _digest(
            {
                "profile_id": lease.profile_id,
                "repository_id": lease.repository_id,
                "target_id": lease.target_id,
                "configured": True,
            }
        )
        surfaces = (
            IdentitySurfaceEvidence(
                surface=IdentitySurface.GITHUB_API,
                evidence_kind=IdentityEvidenceKind.VERIFIED_ACTOR,
                repository_id=lease.repository_id,
                profile_id=lease.profile_id,
                actor_id=actor_id,
                target=admission.observation.canonical_name,
                observed_at=observed_at,
                evidence_digest=api_evidence,
            ),
            IdentitySurfaceEvidence(
                surface=effect_surface,
                evidence_kind=IdentityEvidenceKind.CONFIGURED_METADATA,
                repository_id=lease.repository_id,
                profile_id=lease.profile_id,
                actor_id=None,
                target=lease.target_id,
                observed_at=observed_at,
                evidence_digest=transport_evidence,
            ),
        )
        return PublicationAuthorization(
            profile_id=lease.profile_id,
            actor_class=admission.profile.actor_class,
            actor_id=actor_id,
            installation_id=dict(lease.provider_metadata).get("installation_id"),
            lease=lease,
            identity_surfaces=surfaces,
            capability_digest=capability_digest,
            permission_digest=permission_digest,
            remote_version=remote_version,
            observed_at=observed_at,
            approved_cross_boundary_id=None,
        )

    def _idempotency_key(
        self,
        supplied: str | None,
        *,
        workspace_id: str,
        shape: _PublicationShape,
        repository_id: str,
    ) -> str:
        if supplied is not None:
            return supplied
        return "publication-" + _digest(
            {
                "workspace_id": workspace_id,
                "kind": shape.kind.value,
                "repository_id": repository_id,
                "source_ref": shape.source_ref,
                "destination_ref": shape.destination_ref,
                "head_sha": shape.head_sha,
                "tree_sha": shape.tree_sha,
            }
        )

    def _execute(
        self,
        *,
        workspace_id: str,
        repo_id: str,
        cwd: Path,
        remote: str,
        selector: AuthProfileSelector,
        idempotency_key: str | None,
        shape: _PublicationShape,
        execute: Callable[[PublicationRequest], PublicationOutcome],
    ) -> PublicationOutcome:
        admission = self._runtime.resolve(repo_id=repo_id, selector=selector)
        configured = self._configured(admission)
        repository_id = admission.observation.repository_id
        transport = configured.transport
        if (
            transport.profile_id != admission.profile.profile_id
            or transport.repository_id != repository_id
            or transport.target_id != repository_id
        ):
            raise _error(
                ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
                "Selected Git transport does not match the admitted stable repository identity.",
            )
        resolved_idempotency_key = self._idempotency_key(
            idempotency_key,
            workspace_id=workspace_id,
            shape=shape,
            repository_id=repository_id,
        )
        operation_seed = _digest(
            {
                "idempotency_key": resolved_idempotency_key,
                "kind": shape.kind.value,
                "repository_id": repository_id,
            }
        )
        operation_id = f"op-{operation_seed[:24]}"
        publication_id = f"publication-{_digest([operation_seed, 'publication'])[:24]}"
        identity_context_id = f"identity-{_digest([operation_seed, 'identity'])[:24]}"
        lease_id = f"lease-{_digest([operation_seed, 'lease'])[:24]}"
        observed_at = self._clock.now_iso()
        remote_version = self._remote_version(
            repository_id=repository_id,
            destination_ref=shape.remote_version_ref or shape.destination_ref,
            remote_head=shape.remote_head,
        )
        with self._runtime.session(
            admission,
            target_kind=AuthTargetKind.REPOSITORY,
            target_id=repository_id,
            required_capability_ids=shape.broker_capabilities,
        ) as session:
            auth_context = session.process_context(self._commands.environment())
            lease = session.auth_lease(
                lease_id=lease_id,
                config_revision=self._config_revision,
                policy_revision=self._policy_revision,
            )
            authorization = self._authorization(
                admission=admission,
                lease=lease,
                remote_version=remote_version,
                observed_at=observed_at,
                effect_surface=(
                    IdentitySurface.PULL_REQUEST
                    if shape.kind is PublicationKind.PULL_REQUEST
                    else IdentitySurface.GIT_PUSH
                ),
                github_capability_ids=shape.broker_capabilities,
            )
            identity_context = OperationIdentityContext(
                operation_id=operation_id,
                primary_repository_id=repository_id,
                actor_class=admission.profile.actor_class,
                auth_leases=(lease,),
                selected_at=observed_at,
                config_revision=self._config_revision,
                policy_revision=self._policy_revision,
            )
            intent = PublicationIntent(
                publication_id=publication_id,
                operation_id=operation_id,
                kind=shape.kind,
                source_repository_id=repository_id,
                destination_repository_id=repository_id,
                remote_name=remote,
                source_ref=shape.source_ref,
                destination_ref=shape.destination_ref,
                expected_commit_sha=shape.head_sha,
                expected_tree_sha=shape.tree_sha,
                base_ref=shape.base_ref,
                head_ref=shape.head_ref,
            )
            request = PublicationRequest(
                workspace_id=workspace_id,
                cwd=cwd,
                intent=intent,
                authorization=authorization,
                identity_context=identity_context,
                identity_context_id=identity_context_id,
                capability_requests=(
                    LeaseCapabilityRequest(
                        lease_id=lease.lease_id,
                        capability_ids=shape.operation_capabilities,
                    ),
                ),
                capability_id=shape.capability_id,
                transport_spec=transport,
                auth_context=auth_context,
                idempotency_key=resolved_idempotency_key,
                pull_request=shape.pull_request,
            )
            return execute(request)

    def execute_push(
        self,
        request: WorkspacePushPublication,
        execute: Callable[[PublicationRequest], PublicationOutcome],
    ) -> PublicationOutcome:
        return self._execute(
            workspace_id=request.workspace_id,
            repo_id=request.repo_id,
            cwd=request.cwd,
            remote=request.remote,
            selector=request.selector,
            idempotency_key=request.idempotency_key,
            shape=_PublicationShape(
                kind=PublicationKind.GIT_PUSH,
                source_ref=request.source_ref,
                destination_ref=request.destination_ref,
                head_sha=request.head_sha,
                tree_sha=request.tree_sha,
                capability_id=_GIT_PUSH,
                broker_capabilities=(_GITHUB_CONTENTS_WRITE,),
                operation_capabilities=(_GIT_PUSH, _GITHUB_CONTENTS_WRITE),
                remote_head=request.remote_head_before,
                remote_version_ref=request.destination_ref,
            ),
            execute=execute,
        )

    def execute_draft_pr(
        self,
        request: WorkspaceDraftPrPublication,
        execute: Callable[[PublicationRequest], PublicationOutcome],
    ) -> PublicationOutcome:
        return self._execute(
            workspace_id=request.workspace_id,
            repo_id=request.repo_id,
            cwd=request.cwd,
            remote=request.remote,
            selector=request.selector,
            idempotency_key=request.idempotency_key,
            shape=_PublicationShape(
                kind=PublicationKind.PULL_REQUEST,
                source_ref=request.head_ref,
                destination_ref=request.base_ref,
                head_sha=request.head_sha,
                tree_sha=request.tree_sha,
                capability_id=_GITHUB_PULL_REQUESTS_WRITE,
                broker_capabilities=(_GITHUB_PULL_REQUESTS_WRITE,),
                operation_capabilities=(_GITHUB_PULL_REQUESTS_WRITE,),
                base_ref=request.base_ref,
                head_ref=request.head_ref,
                pull_request=PullRequestPublication(request.title, request.body),
                remote_head=request.head_sha,
                remote_version_ref=request.head_ref,
            ),
            execute=execute,
        )


__all__ = ["ScopedWorkspacePublicationRequestFactory"]
