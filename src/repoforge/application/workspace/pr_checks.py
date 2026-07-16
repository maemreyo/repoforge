from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..context import ApplicationContext

_DEFAULT_NEXT_STEP = "Review the returned check summary as needed."


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
    next_step: str = _DEFAULT_NEXT_STEP


def _first_failing_selector(checks: list[dict[str, Any]]) -> str | None:
    for item in checks:
        if str(item.get("bucket", "")) == "fail":
            selector = item.get("selector")
            if isinstance(selector, str) and selector:
                return selector
    return None


class WorkspacePrChecksReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def _first_required_failing_selector(
        self,
        path: Path,
        branch: str,
        command: WorkspacePrChecksCommand,
        checks: list[dict[str, Any]],
        buckets: dict[str, int],
    ) -> str | None:
        """Return an exact selector for the first failing *required* check, if any.

        ``gh pr checks`` only distinguishes required checks through its own
        ``--required`` filter, not through a per-item field, so an unfiltered
        call needs one additional bounded lookup to tell a required failure
        from an optional one. That lookup only happens when the primary
        result already shows a failing check, so an all-green result never
        pays for it.
        """
        if buckets.get("fail", 0) <= 0:
            return None
        if command.required_only:
            return _first_failing_selector(checks)
        try:
            required_checks = self.ctx.github.checks(path, branch, required_only=True)
        except Exception:
            return None
        return _first_failing_selector(required_checks)

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
            pending = buckets.get("pending", 0) > 0

            required_failing_selector = self._first_required_failing_selector(
                path, record.branch, command, checks, buckets
            )
            tracker = self.ctx.nudge_tracker
            repeated_pending_poll = False
            if tracker is not None:
                if pending:
                    repeated_pending_poll = tracker.observe_pending_pr_check_poll(
                        command.workspace_id, self.ctx.now_epoch()
                    )
                else:
                    tracker.reset_pr_check_polls(command.workspace_id)

            if required_failing_selector is not None:
                next_step = (
                    "A required check is failing; call "
                    f'workspace_pr_check_details(workspace_id="{command.workspace_id}", '
                    f'check_selector="{required_failing_selector}") for structured detail or '
                    f'workspace_pr_failure_evidence(workspace_id="{command.workspace_id}", '
                    f'check_selector="{required_failing_selector}") for a bounded failure '
                    "excerpt, instead of re-polling workspace_pr_checks or guessing."
                )
            elif repeated_pending_poll:
                next_step = (
                    "Checks have been polled repeatedly while still pending; call "
                    f'workspace_pr_watch(workspace_id="{command.workspace_id}") to wait for '
                    "completion in one call instead of polling workspace_pr_checks again."
                )
            else:
                next_step = _DEFAULT_NEXT_STEP

            return WorkspacePrChecksResult(
                workspace_id=command.workspace_id,
                branch=record.branch,
                required_only=command.required_only,
                checks=checks,
                summary=buckets,
                all_passed=bool(checks) and set(buckets).issubset({"pass", "skipping"}),
                pending=pending,
                head_sha=head_sha,
                pushed_sha=pushed_sha,
                stale=stale,
                next_step=next_step,
            )

        return self.ctx.audited(
            "workspace_pr_checks",
            {"workspace_id": command.workspace_id, "required_only": command.required_only},
            op,
        )
