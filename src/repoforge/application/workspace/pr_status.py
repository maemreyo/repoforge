from dataclasses import dataclass
from typing import Any

from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspacePrStatusCommand:
    workspace_id: str


@dataclass(frozen=True, slots=True)
class WorkspacePrStatusResult:
    payload: dict[str, Any]


class WorkspacePrStatusReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspacePrStatusCommand) -> WorkspacePrStatusResult:
        record, _, path = self.ctx.workspace(c.workspace_id)
        return self.ctx.audited(
            "workspace_pr_status",
            {"workspace_id": c.workspace_id},
            lambda: WorkspacePrStatusResult(self.ctx.github.status(path, record.branch)),
        )
