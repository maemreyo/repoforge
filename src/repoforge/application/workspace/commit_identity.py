"""Shared fail-closed loading for workspace-pinned commit identity."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.commit_identity import CommitIdentityPolicy, commit_identity_policy_from_payload
from ...domain.errors import ErrorCode, WorkspaceError
from ...domain.workspace import WorkspaceRecord
from ...ports.commit_identity import CommitIdentityGateway
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class PinnedCommitIdentity:
    gateway: CommitIdentityGateway
    policy: CommitIdentityPolicy
    config_digest: str


def require_pinned_commit_identity(
    ctx: ApplicationContext,
    record: WorkspaceRecord,
) -> PinnedCommitIdentity:
    gateway = ctx.commit_identities
    raw_policy = record.metadata.get("commit_identity_policy")
    config_digest = record.metadata.get("commit_identity_config_digest")
    if (
        gateway is None
        or not isinstance(raw_policy, dict)
        or not all(isinstance(key, str) for key in raw_policy)
        or not isinstance(config_digest, str)
        or len(config_digest) != 64
    ):
        raise WorkspaceError(
            "Workspace commit identity is unresolved; migrate or recreate the workspace",
            code=ErrorCode.COMMIT_IDENTITY_UNRESOLVED,
            unchanged_state=("No commit was created.",),
        )
    try:
        policy = commit_identity_policy_from_payload(
            {str(key): value for key, value in raw_policy.items()}
        )
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(
            "Workspace commit identity metadata is malformed",
            code=ErrorCode.COMMIT_IDENTITY_UNRESOLVED,
            unchanged_state=("No commit was created.",),
        ) from exc
    return PinnedCommitIdentity(gateway, policy, config_digest)
