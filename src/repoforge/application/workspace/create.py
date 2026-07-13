from __future__ import annotations
from dataclasses import dataclass
from ..context import ApplicationContext
from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import slugify, validate_branch
from ...domain.workspace import WorkspaceRecord


@dataclass(frozen=True, slots=True)
class WorkspaceCreateCommand:
    repo_id: str
    task_slug: str
    base: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceCreateResult:
    workspace_id: str
    repo_id: str
    path: str
    branch: str
    base: str
    head_sha: str
    next_step: str = (
        "Inspect files, make changes, run a verification profile, then review the diff."
    )


class WorkspaceCreator:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceCreateCommand) -> WorkspaceCreateResult:
        repo = self.ctx.repo(c.repo_id)
        base = c.base or repo.default_base
        if base not in repo.allowed_base_branches:
            raise SecurityError(
                f"Base branch {base!r} is not allowlisted: {repo.allowed_base_branches}"
            )
        slug = slugify(c.task_slug)
        suffix = self.ctx.ids.new_hex(10)
        workspace_id = f"{slug[:24]}-{suffix}"
        branch = f"{repo.branch_prefix}{slug}-{suffix}"
        validate_branch(branch, repo)
        root = self.ctx.config.server.workspace_root.resolve()
        destination = (root / repo.repo_id / workspace_id).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise SecurityError(
                "Generated workspace path escaped workspace_root"
            ) from exc
        if destination.exists():
            raise WorkspaceError(f"Workspace destination already exists: {destination}")

        def op() -> WorkspaceCreateResult:
            head = self.ctx.git.create_worktree(repo, destination, branch, base)
            record = WorkspaceRecord(
                workspace_id,
                repo.repo_id,
                str(destination),
                branch,
                base,
                repo.remote,
                self.ctx.clock.now_iso(),
            )
            try:
                self.ctx.store.save(record)
            except Exception as exc:
                try:
                    self.ctx.git.remove_worktree(repo, destination, branch, True)
                except Exception as cleanup_exc:
                    raise WorkspaceError(
                        f"Workspace registry save failed and compensation failed: {cleanup_exc}"
                    ) from exc
                raise
            return WorkspaceCreateResult(
                workspace_id, repo.repo_id, str(destination), branch, base, head
            )

        return self.ctx.audited(
            "workspace_create",
            {
                "repo_id": c.repo_id,
                "base": base,
                "branch": branch,
                "workspace_id": workspace_id,
            },
            op,
        )
