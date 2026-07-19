from __future__ import annotations

from dataclasses import dataclass

from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class RepositoryStatusCommand:
    repo_id: str


@dataclass(frozen=True, slots=True)
class RepositoryStatusResult:
    repo_id: str
    path: str
    git_status: str
    remotes: str
    gh_authenticated: bool
    gh_auth_status: str


class RepositoryStatusReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryStatusCommand) -> RepositoryStatusResult:
        return self.ctx.audited(
            "repo_status",
            {"repo_id": c.repo_id},
            lambda: self.compute(c),
        )

    def compute(self, c: RepositoryStatusCommand) -> RepositoryStatusResult:
        """Read source status without creating a nested audit event."""
        repo = self.ctx.repo(c.repo_id)
        ok, auth = self.ctx.github.auth_status(repo.path)
        return RepositoryStatusResult(
            c.repo_id,
            str(repo.path),
            self.ctx.git.status_short_branch(repo.path),
            self.ctx.git.remote_verbose(repo.path),
            ok,
            auth,
        )
