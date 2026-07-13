from dataclasses import dataclass
from typing import Any, cast

from ...domain.publishing import validate_pr_update
from ..context import ApplicationContext
from ..dto import to_data


@dataclass(frozen=True, slots=True)
class WorkspaceUpdateDraftPrCommand:
    workspace_id: str
    title: str | None = None
    body: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceUpdateDraftPrResult:
    payload: dict[str, Any]


class DraftPullRequestUpdater:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceUpdateDraftPrCommand) -> WorkspaceUpdateDraftPrResult:
        record, _, path = self.ctx.workspace(c.workspace_id)
        title, body = validate_pr_update(c.title, c.body)
        return cast(
            WorkspaceUpdateDraftPrResult,
            self.ctx.idempotent(
                "workspace_update_draft_pr",
                c.idempotency_key,
                {"workspace_id": c.workspace_id, "title": title, "body": body},
                lambda: WorkspaceUpdateDraftPrResult(
                    self.ctx.github.update(path, record.branch, title=title, body=body)
                ),
                details={"workspace_id": c.workspace_id},
                serialize=to_data,
                deserialize=lambda value: WorkspaceUpdateDraftPrResult(**value),
            ),
        )
