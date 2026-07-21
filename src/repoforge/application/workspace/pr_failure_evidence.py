"""Build bounded, secret-safe failure evidence for one exact workspace Check Run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ...adapters.persistence.failure_output_artifact_store import persist_failure_output
from ...domain.ci_evidence import sanitize_ci_text
from ...domain.errors import CommandError
from ...domain.failure_artifacts import extract_failure
from ..context import ApplicationContext
from .pr_check_context import (
    load_workspace_check_context,
    source_error_label,
)
from .pr_check_render import RenderedCheckMaterial, render_check_material


@dataclass(frozen=True, slots=True)
class WorkspacePrFailureEvidenceCommand:
    workspace_id: str
    check_selector: str
    max_excerpt_lines: int = 80


@dataclass(frozen=True, slots=True)
class WorkspacePrFailureEvidenceResult:
    workspace_id: str
    branch: str
    selector: str
    pushed_sha: str
    head_sha: str
    stale: bool
    check_run_id: int
    name: str
    status: str
    conclusion: str | None
    run_id: int | None
    job_id: int | None
    attempt: int | None
    retried: bool
    failed_step: str | None
    failure_class: str
    retryable: bool
    excerpt: str
    excerpt_sha256: str
    source_url: str
    coverage: str
    uncertainty: list[str]
    source_errors: list[str]
    redacted: bool
    withheld_lines: int
    truncated: bool
    annotation_count: int
    failure_provider: str | None
    selector_coverage: str
    selectors_unavailable_reason: str | None
    failed_selectors: list[str]
    failure_locations: list[dict[str, object]]
    output_artifact_reference: str | None
    output_artifact_status: str


def _annotation_lines(material: RenderedCheckMaterial) -> list[str]:
    lines: list[str] = []
    for annotation in material.annotations:
        path = str(annotation.get("path", ""))
        start_line = annotation.get("start_line")
        location = f"{path}:{start_line}" if start_line else path
        title = str(annotation.get("title", "")).strip()
        message = str(annotation.get("message", "")).strip()
        raw_details = str(annotation.get("raw_details", "")).strip()
        headline = ": ".join(part for part in (location, title) if part)
        if headline:
            lines.append(headline)
        if message:
            lines.extend(message.splitlines())
        if raw_details:
            lines.extend(raw_details.splitlines())
    return lines


def _nonempty_lines(*values: str) -> list[str]:
    lines: list[str] = []
    for value in values:
        lines.extend(line for line in value.splitlines() if line.strip())
    return lines


class WorkspacePrFailureEvidenceReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(
        self, command: WorkspacePrFailureEvidenceCommand
    ) -> WorkspacePrFailureEvidenceResult:
        line_limit = max(1, min(command.max_excerpt_lines, 200))

        def op() -> WorkspacePrFailureEvidenceResult:
            context = load_workspace_check_context(
                self.ctx,
                command.workspace_id,
                command.check_selector,
            )
            material = render_check_material(context)
            classification = material.classification
            source_errors = list(context.source_errors)
            redacted = material.redacted
            withheld_lines = material.withheld_lines
            truncated = material.truncated

            is_failure = classification.failure_class not in {
                "pass",
                "pending",
                "skipped",
                "cancellation",
            }
            excerpt_lines: list[str] = []
            if is_failure:
                excerpt_lines.extend(_annotation_lines(material))
                if material.failed_step:
                    excerpt_lines.append(f"Failed step: {material.failed_step}")
                excerpt_lines.extend(
                    _nonempty_lines(
                        material.output_title,
                        material.output_summary,
                        material.output_text,
                    )
                )

                annotations_unavailable = any(
                    error.startswith("annotations_") for error in source_errors
                )
                initial_extraction = extract_failure(
                    (material.failed_step or material.name,),
                    "\n".join(excerpt_lines),
                    returncode=1,
                )
                actionable_annotation_evidence = bool(
                    initial_extraction.selectors or initial_extraction.locations
                )
                should_read_log = context.check.job_id is not None and (
                    not material.annotations
                    or annotations_unavailable
                    or not excerpt_lines
                    or not actionable_annotation_evidence
                )
                if should_read_log and context.check.job_id is not None:
                    try:
                        job_log = self.ctx.github.actions_job_log(
                            context.path,
                            context.check.job_id,
                            max_chars=64_000,
                        )
                    except CommandError as exc:
                        source_errors.append(source_error_label("job_log", exc))
                    else:
                        rendered_log = sanitize_ci_text(
                            job_log.text,
                            context.repo,
                            max_chars=64_000,
                        )
                        excerpt_lines.extend(_nonempty_lines(rendered_log.text))
                        redacted = redacted or rendered_log.redacted
                        withheld_lines += rendered_log.withheld_lines
                        truncated = truncated or rendered_log.truncated or job_log.truncated

            complete_evidence = "\n".join(excerpt_lines)
            extraction = extract_failure(
                (material.failed_step or material.name,),
                complete_evidence,
                returncode=1 if is_failure else 0,
            )
            artifact_reference: str | None = None
            artifact_status = "not_applicable"
            if is_failure:
                artifact = persist_failure_output(
                    self.ctx.config.server.state_root,
                    complete_evidence,
                )
                artifact_reference = artifact.reference
                artifact_status = artifact.status
                if artifact_reference is not None and truncated:
                    artifact_status = "source_truncated"
                elif artifact_reference is not None and any(
                    error.startswith("job_log_") for error in source_errors
                ):
                    artifact_status = "source_unavailable"

            excerpt_truncated = len(excerpt_lines) > line_limit
            selected_lines = excerpt_lines[:line_limit]
            excerpt = "\n".join(selected_lines)
            if len(excerpt) > 64_000:
                omitted = len(excerpt) - 64_000
                excerpt = f"{excerpt[:64_000]} ... <{omitted} characters omitted>"
                excerpt_truncated = True
            truncated = truncated or excerpt_truncated
            excerpt_sha256 = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()

            if not excerpt:
                coverage = "none"
            elif source_errors:
                coverage = "partial"
            else:
                coverage = "complete"

            uncertainty: list[str] = []
            if coverage == "none" and is_failure:
                uncertainty.append("No bounded failure excerpt was available from GitHub.")
            elif coverage == "none":
                uncertainty.append("The selected check does not currently expose failure evidence.")
            if source_errors:
                uncertainty.append("One or more optional GitHub evidence sources were unavailable.")
            if truncated:
                uncertainty.append("Evidence was truncated to configured output limits.")

            source = sanitize_ci_text(
                context.check.source_url,
                context.repo,
                max_chars=4_000,
            )
            redacted = redacted or source.redacted
            withheld_lines += source.withheld_lines
            truncated = truncated or source.truncated
            run_id = context.job.run_id if context.job is not None else context.check.run_id
            attempt = context.job.attempt if context.job is not None else None
            return WorkspacePrFailureEvidenceResult(
                workspace_id=command.workspace_id,
                branch=context.branch,
                selector=context.selector,
                pushed_sha=context.pushed_sha,
                head_sha=context.check.head_sha,
                stale=False,
                check_run_id=context.check.check_run_id,
                name=material.name,
                status=context.check.status,
                conclusion=context.check.conclusion,
                run_id=run_id,
                job_id=context.check.job_id,
                attempt=attempt,
                retried=bool(attempt and attempt > 1),
                failed_step=material.failed_step,
                failure_class=classification.failure_class,
                retryable=classification.retryable,
                excerpt=excerpt,
                excerpt_sha256=excerpt_sha256,
                source_url=source.text,
                coverage=coverage,
                uncertainty=uncertainty,
                source_errors=source_errors,
                redacted=redacted,
                withheld_lines=withheld_lines,
                truncated=truncated,
                annotation_count=len(material.annotations),
                failure_provider=extraction.provider,
                selector_coverage=extraction.selector_coverage,
                selectors_unavailable_reason=extraction.selectors_unavailable_reason,
                failed_selectors=list(extraction.selectors),
                failure_locations=[
                    {
                        "path": item.path,
                        "line": item.line,
                        "column": item.column,
                        "code": item.code,
                    }
                    for item in extraction.locations
                ],
                output_artifact_reference=artifact_reference,
                output_artifact_status=artifact_status,
            )

        return self.ctx.audited(
            "workspace_pr_failure_evidence",
            {
                "workspace_id": command.workspace_id,
                "check_selector": command.check_selector,
                "max_excerpt_lines": line_limit,
            },
            op,
        )
