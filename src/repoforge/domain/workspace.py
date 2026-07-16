"""Workspace domain records, independent of persistence and locking adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import WorkspaceError

_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REFRESH_PREVIEW_RE = re.compile(r"refresh-v1:([0-9a-f]{40}(?:[0-9a-f]{24})?):([0-9a-f]{64})")

MAX_ISSUE_IDS = 16
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
