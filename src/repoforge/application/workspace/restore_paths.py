import re
from dataclasses import dataclass
from typing import Any

from ...domain.errors import WorkspaceError
from ...domain.policy import assert_path_allowed
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint, read_fingerprint

_SHA = re.compile("^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class WorkspaceRestorePathsCommand:
    workspace_id: str
    relative_paths: list[str]
    expected_workspace_fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkspaceRestorePathsResult:
    workspace_id: str
    restored_tracked: list[str]
    removed_untracked: list[str]
    workspace_fingerprint: str
    change_metrics: dict[str, Any]
    head_sha: str


class WorkspacePathsRestorer:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceRestorePathsCommand) -> WorkspaceRestorePathsResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        if not c.relative_paths:
            raise WorkspaceError("relative_paths must contain at least one path")
        if len(c.relative_paths) > self.ctx.config.server.max_batch_files:
            raise WorkspaceError(
                f"relative_paths exceeds max_batch_files={self.ctx.config.server.max_batch_files}"
            )
        if not _SHA.fullmatch(c.expected_workspace_fingerprint):
            raise ValueError("expected_workspace_fingerprint must be a lowercase SHA-256")
        normalized = [assert_path_allowed(x, repo) for x in dict.fromkeys(c.relative_paths)]

        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "path_count": len(normalized),
        }

        def op() -> WorkspaceRestorePathsResult:
            with self.ctx.locks.lock(c.workspace_id):
                before = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    path,
                )
                audit_details.update(
                    {
                        "fingerprint_source": before.source,
                        "fingerprint_duration_ms": before.duration_ms,
                    }
                )
                if before.fingerprint != c.expected_workspace_fingerprint:
                    raise WorkspaceError(
                        "Workspace changed since it was inspected; refresh status before restoring"
                    )
                restored, removed = self.ctx.git.restore_paths(path, repo, normalized)
                after = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    path,
                )
                audit_details["post_mutation_fingerprint_duration_ms"] = after.duration_ms
                return WorkspaceRestorePathsResult(
                    c.workspace_id,
                    restored,
                    removed,
                    after.fingerprint,
                    self.ctx.git.change_metrics(path, repo),
                    self.ctx.git.head_sha(path),
                )

        return self.ctx.audited(
            "workspace_restore_paths",
            audit_details,
            op,
        )
