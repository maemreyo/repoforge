from dataclasses import dataclass
from typing import Any

from ..context import ApplicationContext


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


class WorkspaceStatusReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceStatusCommand) -> WorkspaceStatusResult:
        record, repo, path = self.ctx.workspace(c.workspace_id)

        def op() -> WorkspaceStatusResult:
            fp = self.ctx.git.fingerprint(path)
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
            )

        return self.ctx.audited("workspace_status", {"workspace_id": c.workspace_id}, op)
