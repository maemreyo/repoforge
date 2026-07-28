"""Managed commit identity boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.commit_identity import (
    CommitConfigSnapshot,
    CommitIdentityPolicy,
    ManagedCommitResult,
)


class CommitIdentityGateway(Protocol):
    def resolve_policy(
        self,
        path: Path,
        configured: CommitIdentityPolicy | None,
    ) -> CommitIdentityPolicy: ...

    def config_snapshot(self, path: Path) -> CommitConfigSnapshot: ...

    def commit(
        self,
        path: Path,
        message: str,
        policy: CommitIdentityPolicy,
        *,
        expected_config_digest: str,
    ) -> ManagedCommitResult: ...

    def commit_merge(
        self,
        path: Path,
        policy: CommitIdentityPolicy,
        *,
        expected_config_digest: str,
    ) -> ManagedCommitResult: ...
