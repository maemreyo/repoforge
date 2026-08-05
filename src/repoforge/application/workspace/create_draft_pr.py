from dataclasses import dataclass, field

from ...domain.auth_profile import AuthProfileSelector
from ...domain.errors import ErrorCode, WorkspaceError
from ...domain.policy import validate_workspace_branch
from ...domain.publishing import render_pr_body, validate_pr_create
from ...ports.workspace_publication import WorkspaceDraftPrPublication
from ..context import ApplicationContext
from .publication import exact_workspace_tree_sha, require_workspace_publication_service


@dataclass(frozen=True, slots=True)
class WorkspaceCreateDraftPrCommand:
    workspace_id: str
    title: str
    body: str
    idempotency_key: str | None = None
    selector: AuthProfileSelector = field(default_factory=AuthProfileSelector)


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
        _record, repo, path = self.ctx.workspace(c.workspace_id)
        title, body = validate_pr_create(c.title, c.body)

        with self.ctx.locks.lock(c.workspace_id):
            fresh = self.ctx.store.load(c.workspace_id)
            self.ctx.git.changed_paths(path, repo)
            self.ctx.git.ensure_clean(path, context="creating a pull request")
            validate_workspace_branch(fresh.kind, fresh.branch, repo)
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
            final = render_pr_body(
                body,
                branch=fresh.branch,
                head_sha=head,
                verification_profile=fresh.metadata.get("verification_profile"),
                verification_completed_at=fresh.metadata.get("verification_completed_at"),
            )
            publications = require_workspace_publication_service(self.ctx)
            effect = publications.create_draft_pr(
                WorkspaceDraftPrPublication(
                    workspace_id=c.workspace_id,
                    repo_id=fresh.repo_id,
                    cwd=path,
                    remote=fresh.remote,
                    base_ref=f"refs/heads/{fresh.base}",
                    head_ref=f"refs/heads/{fresh.branch}",
                    head_sha=head,
                    tree_sha=exact_workspace_tree_sha(self.ctx, path, repo, head),
                    title=title,
                    body=final,
                    idempotency_key=c.idempotency_key,
                    selector=c.selector,
                )
            )
            authoritative_result = WorkspaceCreateDraftPrResult(
                workspace_id=c.workspace_id,
                url=effect.url,
                draft=True,
                branch=fresh.branch,
                base=fresh.base,
                labels=list(repo.pr_labels),
                reviewers=list(repo.pr_reviewers),
                already_existed=effect.reconciled,
            )
            fresh.metadata["pr_url"] = effect.url
            fresh.metadata["last_pr_publication"] = {
                "publication_id": effect.publication_id,
                "operation_id": effect.operation_id,
                "receipt_id": effect.receipt_id,
                "result_reference": effect.result_reference,
                "reconciled": effect.reconciled,
                "completed_at": self.ctx.clock.now_iso(),
            }
            try:
                self.ctx.store.save(fresh)
            except Exception as exc:
                raise WorkspaceError(
                    f"Draft PR {effect.url} was created but workspace registry update failed; "
                    "retry will reconcile the same publication",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=True,
                    details={
                        "publication_id": effect.publication_id,
                        "operation_id": effect.operation_id,
                        "receipt_id": effect.receipt_id,
                        "result_reference": effect.result_reference,
                    },
                ) from exc
            return authoritative_result
