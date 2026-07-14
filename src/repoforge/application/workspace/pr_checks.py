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
    head_sha: str
    pushed_sha: str | None
    stale: bool


class WorkspacePrChecksReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspacePrChecksCommand) -> WorkspacePrChecksResult:
        record, _, path = self.ctx.workspace(command.workspace_id)

        def op() -> WorkspacePrChecksResult:
            checks = self.ctx.github.checks(
                path,
                record.branch,
                required_only=command.required_only,
            )
            buckets: dict[str, int] = {}
            for item in checks:
                bucket = str(item.get("bucket", "unknown"))
                buckets[bucket] = buckets.get(bucket, 0) + 1
            head_sha = self.ctx.git.head_sha(path).lower()
            pushed_raw = record.metadata.get("last_pushed_sha")
            pushed_sha = pushed_raw.lower() if isinstance(pushed_raw, str) else None
            stale = bool(
                pushed_sha
                and (
                    head_sha != pushed_sha
                    or any(
                        bool(item.get("stale"))
                        or (
                            isinstance(item.get("head_sha"), str)
                            and str(item["head_sha"]).lower() != pushed_sha
                        )
                        for item in checks
                    )
                )
            )
            return WorkspacePrChecksResult(
                workspace_id=command.workspace_id,
                branch=record.branch,
                required_only=command.required_only,
                checks=checks,
                summary=buckets,
                all_passed=bool(checks) and set(buckets).issubset({"pass", "skipping"}),
                pending=buckets.get("pending", 0) > 0,
                head_sha=head_sha,
                pushed_sha=pushed_sha,
                stale=stale,
            )

        return self.ctx.audited(
            "workspace_pr_checks",
            {"workspace_id": command.workspace_id, "required_only": command.required_only},
            op,
        )
