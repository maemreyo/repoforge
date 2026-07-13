from dataclasses import dataclass

from ...domain.errors import WorkspaceError
from ...domain.policy import validate_branch
from ...domain.publishing import render_pr_body, validate_pr_create
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceCreateDraftPrCommand:
    workspace_id: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class WorkspaceCreateDraftPrResult:
    workspace_id: str | None = None
    url: str | None = None
    draft: bool | None = None
    branch: str | None = None
    base: str | None = None
    labels: list[str] | None = None
    reviewers: list[str] | None = None
    already_existed: bool = False
    payload: dict[str, object] | None = None


class DraftPullRequestCreator:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceCreateDraftPrCommand) -> WorkspaceCreateDraftPrResult:
        record, repo, path = self.ctx.workspace(c.workspace_id)
        title, body = validate_pr_create(c.title, c.body)

        def op() -> WorkspaceCreateDraftPrResult:
            with self.ctx.locks.lock(c.workspace_id):
                fresh = self.ctx.store.load(c.workspace_id)
                self.ctx.git.changed_paths(path, repo)
                self.ctx.git.ensure_clean(path, context="creating a pull request")
                validate_branch(fresh.branch, repo)
                if self.ctx.git.upstream_name(path) is None:
                    raise WorkspaceError("Branch has no upstream; call workspace_push first")
                head = self.ctx.git.head_sha(path)
                if self.ctx.git.upstream_sha(path) != head:
                    raise WorkspaceError(
                        "Local branch is not synchronized with its upstream; call workspace_push first"
                    )
                if fresh.metadata.get("last_pushed_sha") != head:
                    raise WorkspaceError(
                        "Workspace registry has no matching successful push for the current HEAD"
                    )
                if self.ctx.git.ahead_of_base(path, fresh.remote, fresh.base) <= 0:
                    raise WorkspaceError("Branch has no commits ahead of the base branch")
                existing = self.ctx.github.find_pr(path, fresh.branch)
                if existing is not None:
                    existing["already_existed"] = True
                    fresh.metadata["pr_url"] = existing.get("url")
                    fresh.metadata["pr_number"] = existing.get("number")
                    self.ctx.store.save(fresh)
                    return WorkspaceCreateDraftPrResult(payload=existing, already_existed=True)
                final = render_pr_body(
                    body,
                    branch=fresh.branch,
                    head_sha=head,
                    verification_profile=fresh.metadata.get("verification_profile"),
                    verification_completed_at=fresh.metadata.get("verification_completed_at"),
                )
                url = self.ctx.github.create_draft(
                    path,
                    repo,
                    branch=fresh.branch,
                    base=fresh.base,
                    title=title,
                    body=final,
                )
                fresh.metadata["pr_url"] = url
                try:
                    self.ctx.store.save(fresh)
                except Exception as exc:
                    raise WorkspaceError(
                        f"Draft PR {url} was created but workspace registry update failed; retry will discover the existing PR"
                    ) from exc
                return WorkspaceCreateDraftPrResult(
                    c.workspace_id,
                    url,
                    True,
                    fresh.branch,
                    fresh.base,
                    list(repo.pr_labels),
                    list(repo.pr_reviewers),
                    False,
                )

        return self.ctx.audited(
            "workspace_create_draft_pr",
            {
                "workspace_id": c.workspace_id,
                "branch": record.branch,
                "base": record.base,
            },
            op,
        )
