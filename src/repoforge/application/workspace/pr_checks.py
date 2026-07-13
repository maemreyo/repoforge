from dataclasses import dataclass
from typing import Any
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspacePrChecksCommand:
    workspace_id: str
    required_only: bool = False


@dataclass(frozen=True, slots=True)
class WorkspacePrChecksResult:
    workspace_id: str
    branch: str
    required_only: bool
    checks: list[dict[str, Any]]
    summary: dict[str, int]
    all_passed: bool
    pending: bool


class WorkspacePrChecksReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspacePrChecksCommand) -> WorkspacePrChecksResult:
        record, _, path = self.ctx.workspace(c.workspace_id)

        def op() -> WorkspacePrChecksResult:
            checks = self.ctx.github.checks(
                path, record.branch, required_only=c.required_only
            )
            buckets: dict[str, int] = {}
            for item in checks:
                bucket = str(item.get("bucket", "unknown"))
                buckets[bucket] = buckets.get(bucket, 0) + 1
            return WorkspacePrChecksResult(
                c.workspace_id,
                record.branch,
                c.required_only,
                checks,
                buckets,
                bool(checks) and set(buckets).issubset({"pass", "skipping"}),
                buckets.get("pending", 0) > 0,
            )

        return self.ctx.audited(
            "workspace_pr_checks",
            {"workspace_id": c.workspace_id, "required_only": c.required_only},
            op,
        )
