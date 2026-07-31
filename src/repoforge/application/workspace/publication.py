"""Workspace publication facade backed by the durable publication coordinator."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ...config import RepositoryConfig
from ...domain.errors import ErrorCode, RepoForgeError, WorkspaceError
from ...ports.git import ResolvedRepositoryRef
from ...ports.workspace_publication import (
    WorkspaceDraftPrPublication,
    WorkspaceDraftPrPublicationEffect,
    WorkspacePublicationService,
    WorkspacePushPublication,
    WorkspacePushPublicationEffect,
)
from ..context import ApplicationContext
from ..publication import PublicationCoordinator, PublicationOutcome, PublicationRequest


class WorkspacePublicationRequestFactory(Protocol):
    """Resolve reviewed repository identity without exposing it in tool inputs."""

    def execute_push(
        self,
        request: WorkspacePushPublication,
        execute: Callable[[PublicationRequest], PublicationOutcome],
    ) -> PublicationOutcome: ...

    def execute_draft_pr(
        self,
        request: WorkspaceDraftPrPublication,
        execute: Callable[[PublicationRequest], PublicationOutcome],
    ) -> PublicationOutcome: ...


class CoordinatedWorkspacePublicationService:
    """Translate workspace facts into one exact durable publication operation."""

    def __init__(
        self,
        coordinator: PublicationCoordinator,
        requests: WorkspacePublicationRequestFactory,
    ) -> None:
        self._coordinator = coordinator
        self._requests = requests

    def push(self, request: WorkspacePushPublication) -> WorkspacePushPublicationEffect:
        outcome = self._requests.execute_push(request, self._coordinator.execute)
        return WorkspacePushPublicationEffect(
            publication_id=outcome.publication_id,
            operation_id=outcome.operation_id,
            receipt_id=outcome.receipt_id,
            result_reference=outcome.result_reference,
            head_sha=outcome.commit_sha,
            remote_head_before=request.remote_head_before,
            remote_head_after=outcome.commit_sha,
            pushed=True,
            reconciled=outcome.reconciled,
            output=(
                "Exact publication reconciled from the reviewed destination ref"
                if outcome.reconciled
                else "Exact publication completed through the reviewed destination ref"
            ),
        )

    def create_draft_pr(
        self,
        request: WorkspaceDraftPrPublication,
    ) -> WorkspaceDraftPrPublicationEffect:
        outcome = self._requests.execute_draft_pr(request, self._coordinator.execute)
        if outcome.url is None:
            raise RuntimeError("Pull-request publication completed without a provider URL")
        return WorkspaceDraftPrPublicationEffect(
            publication_id=outcome.publication_id,
            operation_id=outcome.operation_id,
            receipt_id=outcome.receipt_id,
            result_reference=outcome.result_reference,
            url=outcome.url,
            reconciled=outcome.reconciled,
        )


def require_workspace_publication_service(
    ctx: ApplicationContext,
) -> WorkspacePublicationService:
    """Return the configured exact publication service or deny before effect."""

    service = ctx.publications
    if service is None:
        raise RepoForgeError(
            "Managed publication identity is unresolved; external writes are denied",
            code=ErrorCode.OPERATION_IDENTITY_NOT_FOUND,
            retryable=False,
            safe_next_action=(
                "Configure and bind an exact repository publication profile, then retry the "
                "same operation without switching the ambient Git or GitHub account."
            ),
            unchanged_state=("No Git push or pull-request publication was started.",),
        )
    return service


def exact_workspace_tree_sha(
    ctx: ApplicationContext,
    path: Path,
    repo: RepositoryConfig,
    head_sha: str,
) -> str:
    """Read the exact tree identity for the already-reviewed workspace HEAD."""

    snapshot = ResolvedRepositoryRef(resolved_ref=head_sha, commit_sha=head_sha)
    if ctx.git.head_sha(path) != head_sha:
        raise WorkspaceError(
            "Workspace HEAD changed while publication identity was being prepared",
            code=ErrorCode.STALE_STATE,
            retryable=True,
            unchanged_state=("No external publication was started.",),
        )
    return ctx.git.read_commit_evidence(
        path,
        repo,
        snapshot,
        1,
        False,
    ).tree_sha


__all__ = [
    "CoordinatedWorkspacePublicationService",
    "WorkspacePublicationRequestFactory",
    "exact_workspace_tree_sha",
    "require_workspace_publication_service",
]
