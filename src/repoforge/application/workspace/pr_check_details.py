"""Read bounded structured details for one exact workspace Check Run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.ci_evidence import sanitize_ci_text
from ..context import ApplicationContext
from .pr_check_context import load_workspace_check_context
from .pr_check_render import render_check_material


@dataclass(frozen=True, slots=True)
class WorkspacePrCheckDetailsCommand:
    workspace_id: str
    check_selector: str


@dataclass(frozen=True, slots=True)
class WorkspacePrCheckDetailsResult:
    workspace_id: str
    branch: str
    selector: str
    pushed_sha: str
    head_sha: str
    stale: bool
    check_run_id: int
    name: str
    app_name: str
    status: str
    conclusion: str | None
    run_id: int | None
    job_id: int | None
    attempt: int | None
    retried: bool
    failed_step: str | None
    annotations: list[dict[str, Any]]
    annotations_truncated: bool
    output_title: str
    output_summary: str
    output_text: str
    source_url: str
    failure_class: str
    retryable: bool
    redacted: bool
    withheld_lines: int
    truncated: bool
    source_errors: list[str]


class WorkspacePrCheckDetailsReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspacePrCheckDetailsCommand) -> WorkspacePrCheckDetailsResult:
        def op() -> WorkspacePrCheckDetailsResult:
            context = load_workspace_check_context(
                self.ctx,
                command.workspace_id,
                command.check_selector,
            )
            rendered = render_check_material(context)
            source = sanitize_ci_text(
                context.check.source_url,
                context.repo,
                max_chars=4_000,
            )
            run_id = context.job.run_id if context.job is not None else context.check.run_id
            attempt = context.job.attempt if context.job is not None else None
            return WorkspacePrCheckDetailsResult(
                workspace_id=command.workspace_id,
                branch=context.branch,
                selector=context.selector,
                pushed_sha=context.pushed_sha,
                head_sha=context.check.head_sha,
                stale=False,
                check_run_id=context.check.check_run_id,
                name=rendered.name,
                app_name=rendered.app_name,
                status=context.check.status,
                conclusion=context.check.conclusion,
                run_id=run_id,
                job_id=context.check.job_id,
                attempt=attempt,
                retried=bool(attempt and attempt > 1),
                failed_step=rendered.failed_step,
                annotations=rendered.annotations,
                annotations_truncated=context.annotations_truncated,
                output_title=rendered.output_title,
                output_summary=rendered.output_summary,
                output_text=rendered.output_text,
                source_url=source.text,
                failure_class=rendered.classification.failure_class,
                retryable=rendered.classification.retryable,
                redacted=rendered.redacted or source.redacted,
                withheld_lines=rendered.withheld_lines + source.withheld_lines,
                truncated=rendered.truncated or source.truncated,
                source_errors=list(context.source_errors),
            )

        return self.ctx.audited(
            "workspace_pr_check_details",
            {
                "workspace_id": command.workspace_id,
                "check_selector": command.check_selector,
            },
            op,
        )
