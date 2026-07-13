from dataclasses import dataclass
from typing import Any
from ..context import ApplicationContext
from ...domain.publishing import validate_pr_update


@dataclass(frozen=True, slots=True)
class WorkspaceUpdateDraftPrCommand:
    workspace_id: str
    title: str | None = None
    body: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceUpdateDraftPrResult:
    payload: dict[str, Any]


class DraftPullRequestUpdater:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceUpdateDraftPrCommand) -> WorkspaceUpdateDraftPrResult:
        record, _, path = self.ctx.workspace(c.workspace_id)
        title, body = validate_pr_update(c.title, c.body)
        return self.ctx.audited(
            "workspace_update_draft_pr",
            {"workspace_id": c.workspace_id},
            lambda: WorkspaceUpdateDraftPrResult(
                self.ctx.github.update(path, record.branch, title=title, body=body)
            ),
        )
