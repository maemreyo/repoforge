"""Minimal lock-scoped identity preflight for explicit verification."""

from __future__ import annotations

from ...config import RepositoryConfig
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.policy import assert_path_allowed
from ...domain.workspace_snapshot import SnapshotToken, new_snapshot_token
from ..context import ApplicationContext, repository_policy_snapshot
from ..fingerprint_cache import compute_validity_token, read_fingerprint


class WorkspaceSnapshotReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    @staticmethod
    def _policy_hash(repo: RepositoryConfig) -> str:
        value = repository_policy_snapshot(repo).get("sha256")
        if not isinstance(value, str) or len(value) != 64:
            raise RepoForgeError(
                "Repository policy hash is unavailable",
                code=ErrorCode.STALE_STATE,
                retryable=True,
            )
        return value

    def capture(
        self,
        workspace_id: str,
        impact_paths: tuple[str, ...] = (),
    ) -> SnapshotToken:
        """Capture one exact identity without collecting planning/read-model evidence."""

        _record, repo, path = self.ctx.workspace(workspace_id)
        with self.ctx.locks.lock(workspace_id):
            lookup = read_fingerprint(
                self.ctx.fingerprint_cache,
                workspace_id,
                self.ctx.git,
                path,
                persist=True,
            )
            cached = (
                self.ctx.fingerprint_cache.get(workspace_id)
                if self.ctx.fingerprint_cache is not None
                else None
            )
            validity_token = (
                cached.validity_token
                if cached is not None and cached.fingerprint == lookup.fingerprint
                else compute_validity_token(self.ctx.git, path)
            )
            selected_paths = impact_paths or tuple(self.ctx.git.changed_paths(path, repo))
            changed_paths = tuple(
                sorted({assert_path_allowed(item, repo) for item in selected_paths})
            )
            return new_snapshot_token(
                workspace_id=workspace_id,
                head_sha=self.ctx.git.head_sha(path).lower(),
                workspace_fingerprint=lookup.fingerprint,
                validity_token=validity_token,
                changed_paths=changed_paths,
                config_generation=self.ctx.config_generation,
                policy_hash=self._policy_hash(repo),
                captured_at=self.ctx.clock.now_iso(),
            )

    def assert_current(self, token: SnapshotToken) -> None:
        """Cheaply reject a token invalidated before an effect is admitted."""

        _record, repo, path = self.ctx.workspace(token.workspace_id)
        with self.ctx.locks.lock(token.workspace_id):
            current = (
                self.ctx.git.head_sha(path).lower(),
                compute_validity_token(self.ctx.git, path),
                self.ctx.config_generation,
                self._policy_hash(repo),
            )
        expected = (
            token.head_sha,
            token.validity_token,
            token.config_generation,
            token.policy_hash,
        )
        if current != expected:
            raise RepoForgeError(
                "Workspace snapshot changed before verification admission",
                code=ErrorCode.STALE_STATE,
                retryable=True,
                safe_next_action="Capture a fresh workspace snapshot and retry verification.",
            )
