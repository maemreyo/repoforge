from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...config import RepositoryConfig
from ...domain.workspace import WorkspaceRecord
from ..context import ApplicationContext
from ..fingerprint_cache import read_fingerprint


@dataclass(frozen=True, slots=True)
class WorkspaceStatusCommand:
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceStatusResult:
    workspace_id: str
    repo_id: str
    path: str
    branch: str
    base: str
    head_sha: str
    workspace_fingerprint: str
    ahead_of_base: int
    status: str
    changed_paths: list[str]
    change_metrics: dict[str, Any]
    clean: bool
    last_verification: dict[str, Any] | None
    issue_ids: list[str]


class WorkspaceStatusReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceStatusCommand) -> WorkspaceStatusResult:
        record, repo, path = self.ctx.workspace(c.workspace_id)
        audit_details: dict[str, object] = {"workspace_id": c.workspace_id}
        return self.ctx.audited(
            "workspace_status",
            audit_details,
            lambda: self._build(c, record, repo, path, audit_details),
        )

    def compute(self, c: WorkspaceStatusCommand) -> WorkspaceStatusResult:
        """Pure application logic with no audit event, for embedding in a larger audited bundle."""
        record, repo, path = self.ctx.workspace(c.workspace_id)
        return self._build(c, record, repo, path, None)

    def _build(
        self,
        c: WorkspaceStatusCommand,
        record: WorkspaceRecord,
        repo: RepositoryConfig,
        path: Path,
        audit_details: dict[str, object] | None,
    ) -> WorkspaceStatusResult:
        with self.ctx.locks.lock(c.workspace_id):
            lookup = read_fingerprint(
                self.ctx.fingerprint_cache,
                c.workspace_id,
                self.ctx.git,
                path,
            )
            fp = lookup.fingerprint
            if audit_details is not None:
                audit_details.update(
                    {
                        "fingerprint_source": lookup.source,
                        "fingerprint_duration_ms": lookup.duration_ms,
                    }
                )
            last = (
                {
                    "profile": record.last_verification.profile,
                    "completed_at": record.last_verification.completed_at,
                    "fingerprint_matches": record.last_verification.fingerprint == fp,
                }
                if record.last_verification
                else None
            )
            return WorkspaceStatusResult(
                c.workspace_id,
                record.repo_id,
                str(path),
                record.branch,
                record.base,
                self.ctx.git.head_sha(path),
                fp,
                self.ctx.git.ahead_of_base(path, record.remote, record.base),
                self.ctx.git.status_short_branch(path),
                self.ctx.git.changed_paths(path, repo),
                self.ctx.git.change_metrics(path, repo),
                not bool(self.ctx.git.status_porcelain(path).strip()),
                last,
                list(record.metadata.get("issue_ids", ())),
            )
