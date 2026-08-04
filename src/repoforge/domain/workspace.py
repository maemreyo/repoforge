"""Workspace domain records, independent of persistence and locking adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import WorkspaceError

_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REFRESH_PREVIEW_RE = re.compile(r"refresh-v1:([0-9a-f]{40}(?:[0-9a-f]{24})?):([0-9a-f]{64})")

MAX_ISSUE_IDS = 100
MAX_ISSUE_ID_LENGTH = 64

WORKSPACE_REFRESH_RECEIPTS = (
    "verification",
    "assessment",
    "architecture",
    "execution_plan",
)

_REFRESH_RECEIPT_METADATA: dict[str, tuple[str, ...]] = {
    "verification": (
        "verified_commit_sha",
        "verification_profile",
        "verification_completed_at",
    ),
    "assessment": (
        "assessment_receipt",
        "assessment_snapshot_id",
        "evidence_snapshot_id",
    ),
    "architecture": (
        "architecture_receipt",
        "architecture_policy_hash",
    ),
    "execution_plan": (
        "accepted_plan_id",
        "execution_plan_id",
        "verification_plan_id",
        "plan_receipt",
    ),
}


@dataclass
class VerificationReceipt:
    profile: str
    fingerprint: str
    completed_at: str
    commands: list[dict[str, Any]]
    environment_identity_hash: str | None = None
    command_source_dirty: bool = False
    command_source_dirty_paths: list[str] = field(default_factory=list)
    requested_policy_hash: str = ""
    effective_policy_hash: str = ""
    execution_evidence: dict[str, Any] = field(default_factory=dict)


class WorkspaceKind(str, Enum):
    """Who created the worktree, and who owns its branch and filesystem lifecycle.

    A discriminator, not a bundle of independently-settable flags: lifecycle ownership,
    naming-convention enforcement, and branch/worktree deletability are all *consequences*
    of which kind a workspace is, so they are exposed as properties derived from the kind
    rather than as separate persisted fields that could drift out of sync with it.
    """

    #: RepoForge cut a fresh branch and created the worktree. Owns both.
    MANAGED_WORKTREE = "managed_worktree"
    #: RepoForge created the worktree, but the branch already existed and belongs to the
    #: operator. Owns the worktree, not the branch.
    ADOPTED_WORKTREE = "adopted_worktree"
    #: RepoForge did not create the worktree at all -- it operates on a checkout the
    #: operator already has. Owns neither the branch nor the worktree/filesystem lifecycle.
    ATTACHED_SHARED = "attached_shared"

    @property
    def owns_branch_lifecycle(self) -> bool:
        """Whether removing this workspace may ever delete its branch."""
        return self is WorkspaceKind.MANAGED_WORKTREE

    @property
    def owns_worktree_lifecycle(self) -> bool:
        """Whether removing this workspace may touch the worktree/filesystem at all."""
        return self is not WorkspaceKind.ATTACHED_SHARED

    @property
    def naming_convention_enforced(self) -> bool:
        """Whether the branch must match the repository's ai/* prefix and base allowlist."""
        return self is WorkspaceKind.MANAGED_WORKTREE

    @property
    def consistency_mode(self) -> ConsistencyMode:
        """Whether exact-state preconditions gate mutation, or become observations (#374).

        managed_worktree and adopted_worktree stay EXACT: RepoForge is the only actor
        expected to touch that specific worktree directory, so a fingerprint or HEAD
        mismatch really does mean "something is wrong, stop." attached_shared is SHARED
        by definition -- the whole point of attaching is that the operator's own editor,
        or anything else, may be touching the same files at the same time, so a mismatch
        there is an expected observation, not evidence of a problem.

        OPTIMISTIC exists as a domain concept for a future kind or explicit override; no
        current WorkspaceKind defaults to it.
        """
        if self is WorkspaceKind.ATTACHED_SHARED:
            return ConsistencyMode.SHARED
        return ConsistencyMode.EXACT


class ConsistencyMode(str, Enum):
    #: Exact-state preconditions (HEAD, workspace fingerprint) must match or the call is
    #: refused outright. Unchanged behavior for managed and adopted workspaces (#374 AC).
    EXACT = "exact"
    #: Reserved for a future kind or explicit override -- not yet the default for any
    #: WorkspaceKind. Intended granularity: relax the whole-tree fingerprint but keep HEAD
    #: exact, or vice versa, rather than SHARED's "both become observations."
    OPTIMISTIC = "optimistic"
    #: Exact-state preconditions become recorded observations rather than mandatory
    #: preconditions: a mismatch is reported, not refused. Per-target guards (each
    #: mutation operation's own expected_sha256) are untouched by this and still fail
    #: closed on a genuine collision -- this only relaxes the whole-tree gate.
    SHARED = "shared"


@dataclass
class WorkspaceRecord:
    workspace_id: str
    repo_id: str
    path: str
    branch: str
    base: str
    remote: str
    created_at: str
    last_verification: VerificationReceipt | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: WorkspaceKind = WorkspaceKind.MANAGED_WORKTREE

    def __post_init__(self) -> None:
        # A record loaded from disk unpacks the raw JSON string directly into this field
        # (see JsonWorkspaceStore.load); coerce and validate here so a registry entry
        # predating this field, or one carrying a corrupt value, fails closed instead of
        # silently comparing unequal to every WorkspaceKind member.
        try:
            self.kind = WorkspaceKind(self.kind)
        except ValueError as exc:
            raise WorkspaceError(
                f"Workspace registry record has an invalid kind: {self.kind!r}"
            ) from exc


@dataclass(frozen=True, slots=True)
class WorkspaceRefreshBinding:
    workspace_id: str
    configured_base: str
    workspace_base_sha: str
    target_base_sha: str
    head_sha: str
    workspace_fingerprint: str
    strategy: str
    predicted_conflict_paths: tuple[str, ...]
    workspace_clean: bool

    def preview_id(self) -> str:
        payload = {
            "configured_base": self.configured_base,
            "head_sha": self.head_sha,
            "predicted_conflict_paths": list(self.predicted_conflict_paths),
            "strategy": self.strategy,
            "target_base_sha": self.target_base_sha,
            "version": 1,
            "workspace_base_sha": self.workspace_base_sha,
            "workspace_clean": self.workspace_clean,
            "workspace_fingerprint": self.workspace_fingerprint,
            "workspace_id": self.workspace_id,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return f"refresh-v1:{self.target_base_sha}:{digest}"


def normalize_issue_ids(values: Sequence[str] | None) -> tuple[str, ...]:
    """Validate and normalize the free-form, non-authoritative issue links for a workspace."""
    if not values:
        return ()
    if len(values) > MAX_ISSUE_IDS:
        raise WorkspaceError(
            f"issue_ids accepts at most {MAX_ISSUE_IDS} entries: got {len(values)}"
        )
    normalized: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value:
            raise WorkspaceError("issue_ids entries must be non-empty")
        if len(value) > MAX_ISSUE_ID_LENGTH:
            raise WorkspaceError(
                f"issue_ids entries must be at most {MAX_ISSUE_ID_LENGTH} characters: {value!r}"
            )
        normalized.append(value)
    return tuple(normalized)


def is_commit_sha(value: object) -> bool:
    return isinstance(value, str) and _COMMIT_SHA_RE.fullmatch(value) is not None


def refresh_preview_target(preview_id: str) -> str:
    matched = _REFRESH_PREVIEW_RE.fullmatch(preview_id)
    if matched is None:
        raise ValueError("Refresh preview id is invalid")
    return matched.group(1)


def invalidate_workspace_refresh_receipts(record: WorkspaceRecord) -> tuple[str, ...]:
    invalidated: list[str] = []
    for category, keys in _REFRESH_RECEIPT_METADATA.items():
        present = category == "verification" and record.last_verification is not None
        for key in keys:
            if key in record.metadata:
                present = True
                record.metadata.pop(key, None)
        if present:
            invalidated.append(category)
    record.last_verification = None
    record.metadata.pop("refresh_commit_sha", None)
    return tuple(invalidated)
