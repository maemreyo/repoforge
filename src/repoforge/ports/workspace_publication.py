"""Workspace-facing exact publication boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REF = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,4090}$")


def _bounded(value: str, field: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be bounded non-empty text")
    return value


def _sha40(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase Git object ID")
    return value


def _ref(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_REF.fullmatch(value) is None or ".." in value:
        raise ValueError(f"{field} must be one exact branch or tag ref")
    return value


@dataclass(frozen=True, slots=True)
class WorkspacePushPublication:
    workspace_id: str
    repo_id: str
    cwd: Path
    remote: str
    source_ref: str
    destination_ref: str
    head_sha: str
    tree_sha: str
    remote_head_before: str | None
    idempotency_key: str | None

    def __post_init__(self) -> None:
        _bounded(self.workspace_id, "workspace_id", 160)
        _bounded(self.repo_id, "repo_id", 128)
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise ValueError("cwd must be an absolute Path")
        _bounded(self.remote, "remote", 160)
        _ref(self.source_ref, "source_ref")
        _ref(self.destination_ref, "destination_ref")
        _sha40(self.head_sha, "head_sha")
        _sha40(self.tree_sha, "tree_sha")
        if self.remote_head_before is not None:
            _sha40(self.remote_head_before, "remote_head_before")
        if self.idempotency_key is not None:
            _bounded(self.idempotency_key, "idempotency_key", 256)


@dataclass(frozen=True, slots=True)
class WorkspaceDraftPrPublication:
    workspace_id: str
    repo_id: str
    cwd: Path
    remote: str
    base_ref: str
    head_ref: str
    head_sha: str
    tree_sha: str
    title: str
    body: str
    idempotency_key: str | None

    def __post_init__(self) -> None:
        _bounded(self.workspace_id, "workspace_id", 160)
        _bounded(self.repo_id, "repo_id", 128)
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise ValueError("cwd must be an absolute Path")
        _bounded(self.remote, "remote", 160)
        _ref(self.base_ref, "base_ref")
        _ref(self.head_ref, "head_ref")
        _sha40(self.head_sha, "head_sha")
        _sha40(self.tree_sha, "tree_sha")
        _bounded(self.title.strip(), "title", 256)
        if not isinstance(self.body, str) or len(self.body) > 96_000 or "\x00" in self.body:
            raise ValueError("body must be bounded text")
        if self.idempotency_key is not None:
            _bounded(self.idempotency_key, "idempotency_key", 256)


@dataclass(frozen=True, slots=True)
class WorkspacePushPublicationEffect:
    publication_id: str
    operation_id: str
    receipt_id: str
    result_reference: str
    head_sha: str
    remote_head_before: str | None
    remote_head_after: str
    pushed: bool
    reconciled: bool
    output: str

    def __post_init__(self) -> None:
        _bounded(self.publication_id, "publication_id", 256)
        _bounded(self.operation_id, "operation_id", 160)
        _bounded(self.receipt_id, "receipt_id", 160)
        _bounded(self.result_reference, "result_reference", 256)
        _sha40(self.head_sha, "head_sha")
        if self.remote_head_before is not None:
            _sha40(self.remote_head_before, "remote_head_before")
        _sha40(self.remote_head_after, "remote_head_after")
        if not isinstance(self.pushed, bool) or not isinstance(self.reconciled, bool):
            raise ValueError("publication effect booleans are invalid")
        if not isinstance(self.output, str) or len(self.output) > 120_000:
            raise ValueError("output must be bounded text")

    def safe_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "result_reference": self.result_reference,
            "head_sha": self.head_sha,
            "remote_head_before": self.remote_head_before,
            "remote_head_after": self.remote_head_after,
            "pushed": self.pushed,
            "reconciled": self.reconciled,
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceDraftPrPublicationEffect:
    publication_id: str
    operation_id: str
    receipt_id: str
    result_reference: str
    url: str
    reconciled: bool

    def __post_init__(self) -> None:
        _bounded(self.publication_id, "publication_id", 256)
        _bounded(self.operation_id, "operation_id", 160)
        _bounded(self.receipt_id, "receipt_id", 160)
        _bounded(self.result_reference, "result_reference", 256)
        _bounded(self.url, "url", 4096)
        if not isinstance(self.reconciled, bool):
            raise ValueError("reconciled must be boolean")

    def safe_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "result_reference": self.result_reference,
            "url": self.url,
            "reconciled": self.reconciled,
        }


class WorkspacePublicationService(Protocol):
    def push(self, request: WorkspacePushPublication) -> WorkspacePushPublicationEffect: ...

    def create_draft_pr(
        self,
        request: WorkspaceDraftPrPublication,
    ) -> WorkspaceDraftPrPublicationEffect: ...


__all__ = [
    "WorkspaceDraftPrPublication",
    "WorkspaceDraftPrPublicationEffect",
    "WorkspacePublicationService",
    "WorkspacePushPublication",
    "WorkspacePushPublicationEffect",
]
