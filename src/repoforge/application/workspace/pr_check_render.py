"""Secret-safe rendering of typed GitHub Check Run evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.ci_evidence import CiFailureClassification, classify_ci_failure, sanitize_ci_text
from ...domain.errors import SecurityError
from ...domain.policy import assert_path_allowed
from .pr_check_context import WorkspaceCheckContext

_FAILURE_STEP_CONCLUSIONS = {"failure", "timed_out", "cancelled", "canceled", "action_required"}


@dataclass(frozen=True, slots=True)
class RenderedCheckMaterial:
    name: str
    app_name: str
    output_title: str
    output_summary: str
    output_text: str
    annotations: list[dict[str, Any]]
    failed_step: str | None
    classification: CiFailureClassification
    redacted: bool
    withheld_lines: int
    truncated: bool
    evidence_parts: tuple[str, ...]


def _sanitize(
    value: str,
    context: WorkspaceCheckContext,
    *,
    max_chars: int,
) -> tuple[str, bool, int, bool]:
    rendered = sanitize_ci_text(value, context.repo, max_chars=max_chars)
    return rendered.text, rendered.redacted, rendered.withheld_lines, rendered.truncated


def _failed_step(context: WorkspaceCheckContext) -> str | None:
    if context.job is None:
        return None
    failed = next(
        (
            step
            for step in context.job.steps
            if (step.conclusion or "").lower() in _FAILURE_STEP_CONCLUSIONS
        ),
        None,
    )
    if failed is None:
        return None
    text, _, _, _ = _sanitize(failed.name, context, max_chars=1_000)
    return text or None


def render_check_material(context: WorkspaceCheckContext) -> RenderedCheckMaterial:
    """Return only bounded, policy-filtered text from one exact Check Run context."""
    redacted = False
    withheld_lines = 0
    truncated = context.annotations_truncated

    def clean(value: str, limit: int) -> str:
        nonlocal redacted, withheld_lines, truncated
        text, changed, withheld, was_truncated = _sanitize(value, context, max_chars=limit)
        redacted = redacted or changed
        withheld_lines += withheld
        truncated = truncated or was_truncated
        return text

    name = clean(context.check.name, 1_000)
    app_name = clean(context.check.app_name, 1_000)
    output_title = clean(context.check.output_title, 4_000)
    output_summary = clean(context.check.output_summary, 16_000)
    output_text = clean(context.check.output_text, 32_000)
    failed_step = _failed_step(context)

    annotations: list[dict[str, Any]] = []
    annotation_budget = 40_000
    for annotation in context.annotations:
        try:
            path = assert_path_allowed(annotation.path, context.repo)
        except SecurityError:
            path = "<withheld:denied-path>"
            redacted = True
            withheld_lines += 1
        rendered_annotation: dict[str, Any] = {
            "path": path,
            "start_line": annotation.start_line,
            "end_line": annotation.end_line,
            "level": clean(annotation.level, 200),
            "title": clean(annotation.title, 2_000),
            "message": clean(annotation.message, 8_000),
            "raw_details": clean(annotation.raw_details, 8_000),
        }
        annotation_size = sum(
            len(value) for value in rendered_annotation.values() if isinstance(value, str)
        )
        if annotation_size > annotation_budget:
            truncated = True
            break
        annotations.append(rendered_annotation)
        annotation_budget -= annotation_size

    parts = tuple(
        part
        for part in (
            name,
            failed_step or "",
            output_title,
            output_summary,
            output_text,
            *(item["title"] for item in annotations),
            *(item["message"] for item in annotations),
            *(item["raw_details"] for item in annotations),
        )
        if isinstance(part, str) and part.strip()
    )
    classification = classify_ci_failure(
        list(parts),
        status=context.check.status,
        conclusion=context.check.conclusion,
    )
    return RenderedCheckMaterial(
        name=name,
        app_name=app_name,
        output_title=output_title,
        output_summary=output_summary,
        output_text=output_text,
        annotations=annotations,
        failed_step=failed_step,
        classification=classification,
        redacted=redacted,
        withheld_lines=withheld_lines,
        truncated=truncated,
        evidence_parts=parts,
    )
