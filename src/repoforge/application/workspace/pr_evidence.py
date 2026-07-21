"""Read-only composite pull-request and CI evidence for Forge v2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ...domain.errors import ConfigError
from ..context import ApplicationContext
from .pr import _pull_request, _remote_version
from .pr_check_details import WorkspacePrCheckDetailsCommand, WorkspacePrCheckDetailsReader
from .pr_checks import WorkspacePrChecksCommand, WorkspacePrChecksReader
from .pr_failure_evidence import (
    WorkspacePrFailureEvidenceCommand,
    WorkspacePrFailureEvidenceReader,
)
from .pr_status import WorkspacePrStatusCommand, WorkspacePrStatusReader

_DETAILS = frozenset({"overview", "check", "failure"})


@dataclass(frozen=True, slots=True)
class WorkspacePrEvidenceCommand:
    workspace_id: str
    detail: str = "overview"
    check_selector: str | None = None
    since: str | None = None
    max_excerpt_lines: int = 80


@dataclass(frozen=True, slots=True)
class WorkspacePrEvidenceResult:
    summary: str
    workspace_id: str
    pull_request: dict[str, object]
    checks: list[dict[str, object]]
    failure_excerpt: list[str]
    failure_provider: str | None
    selector_coverage: str
    selectors_unavailable_reason: str | None
    failed_selectors: list[str]
    failure_locations: list[dict[str, object]]
    output_artifact_reference: str | None
    output_artifact_status: str
    remote_version: str
    delta_token: str
    changed_since: bool
    truncated: bool


def _status(bucket: str) -> str:
    normalized = bucket.lower()
    if normalized == "pass":
        return "pass"
    if normalized == "fail":
        return "fail"
    if normalized in {"skipping", "skipped", "cancelled", "canceled"}:
        return "skipped"
    if normalized not in {"", "none", "pending", "unknown"}:
        return "fail"
    return "pending"


def _check(item: dict[str, Any], *, annotations: list[str] | None = None) -> dict[str, object]:
    selector = item.get("selector")
    name = item.get("name")
    return {
        "selector": selector if isinstance(selector, str) and selector else "check-run:unknown",
        "name": name if isinstance(name, str) and name else "unknown check",
        "status": _status(str(item.get("bucket", item.get("failure_class", "pending")))),
        "required": bool(item.get("required", False)),
        "annotations": list(annotations or ()),
    }


def _delta(payload: dict[str, object]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"pr-evidence:{digest}"


class WorkspacePrEvidenceReader:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        status: WorkspacePrStatusReader,
        checks: WorkspacePrChecksReader,
        details: WorkspacePrCheckDetailsReader,
        failure: WorkspacePrFailureEvidenceReader,
    ) -> None:
        self.ctx = ctx
        self.status = status
        self.checks = checks
        self.details = details
        self.failure = failure

    def execute(self, command: WorkspacePrEvidenceCommand) -> WorkspacePrEvidenceResult:
        if command.detail not in _DETAILS:
            raise ConfigError(f"Unknown workspace_pr_evidence detail {command.detail!r}")
        if command.detail in {"check", "failure"} and command.check_selector is None:
            raise ConfigError(f"workspace_pr_evidence {command.detail} requires check_selector")
        if command.detail == "overview" and command.check_selector is not None:
            raise ConfigError("check_selector is only valid for check or failure detail")
        line_limit = max(1, min(command.max_excerpt_lines, 200))
        return self.ctx.audited(
            "workspace_pr_evidence",
            {
                "workspace_id": command.workspace_id,
                "detail": command.detail,
                "check_selector": command.check_selector,
                "since_supplied": command.since is not None,
                "max_excerpt_lines": line_limit,
            },
            lambda: self._execute(command, line_limit),
            mutating=False,
        )

    def _execute(
        self, command: WorkspacePrEvidenceCommand, line_limit: int
    ) -> WorkspacePrEvidenceResult:
        record, _repo, _path = self.ctx.workspace(command.workspace_id)
        status_result = self.status.execute(WorkspacePrStatusCommand(command.workspace_id))
        pull_request = _pull_request(status_result.payload, base_ref=record.base)
        remote_version = _remote_version(status_result.payload, repo_id=record.repo_id)
        checks: list[dict[str, object]] = []
        failure_excerpt: list[str] = []
        failure_provider: str | None = None
        selector_coverage = "not_applicable"
        selectors_unavailable_reason: str | None = None
        failed_selectors: list[str] = []
        failure_locations: list[dict[str, object]] = []
        output_artifact_reference: str | None = None
        output_artifact_status = "not_applicable"
        truncated = False

        if command.detail == "overview":
            result = self.checks.execute(WorkspacePrChecksCommand(command.workspace_id, False))
            checks = [_check(item) for item in result.checks[:500]]
            truncated = len(result.checks) > 500
        elif command.detail == "check":
            assert command.check_selector is not None
            detail = self.details.execute(
                WorkspacePrCheckDetailsCommand(command.workspace_id, command.check_selector)
            )
            annotations = [
                json.dumps(item, sort_keys=True, ensure_ascii=False)[:4_000]
                for item in detail.annotations[:200]
            ]
            checks = [
                _check(
                    {
                        "selector": detail.selector,
                        "name": detail.name,
                        "failure_class": detail.failure_class,
                        "required": False,
                    },
                    annotations=annotations,
                )
            ]
            truncated = detail.truncated or detail.annotations_truncated
        else:
            assert command.check_selector is not None
            failure = self.failure.execute(
                WorkspacePrFailureEvidenceCommand(
                    command.workspace_id,
                    command.check_selector,
                    line_limit,
                )
            )
            checks = [
                _check(
                    {
                        "selector": failure.selector,
                        "name": failure.name,
                        "failure_class": failure.failure_class,
                        "required": False,
                    }
                )
            ]
            failure_excerpt = failure.excerpt.splitlines()[:line_limit]
            failure_provider = failure.failure_provider
            selector_coverage = failure.selector_coverage
            selectors_unavailable_reason = failure.selectors_unavailable_reason
            failed_selectors = failure.failed_selectors
            failure_locations = failure.failure_locations
            output_artifact_reference = failure.output_artifact_reference
            output_artifact_status = failure.output_artifact_status
            truncated = failure.truncated or len(failure.excerpt.splitlines()) > line_limit

        snapshot: dict[str, object] = {
            "pull_request": pull_request,
            "checks": checks,
            "failure_excerpt": failure_excerpt,
            "failure_provider": failure_provider,
            "selector_coverage": selector_coverage,
            "selectors_unavailable_reason": selectors_unavailable_reason,
            "failed_selectors": failed_selectors,
            "failure_locations": failure_locations,
            "output_artifact_reference": output_artifact_reference,
            "output_artifact_status": output_artifact_status,
            "detail": command.detail,
            "truncated": truncated,
        }
        token = _delta(snapshot)
        changed = command.since != token
        if not changed:
            checks = []
            failure_excerpt = []
            failure_provider = None
            selector_coverage = "not_applicable"
            selectors_unavailable_reason = None
            failed_selectors = []
            failure_locations = []
            output_artifact_reference = None
            output_artifact_status = "not_applicable"
        return WorkspacePrEvidenceResult(
            summary=(
                "Pull-request evidence is unchanged since the supplied delta token"
                if not changed
                else f"Read {command.detail} pull-request evidence"
            ),
            workspace_id=command.workspace_id,
            pull_request=pull_request,
            checks=checks,
            failure_excerpt=failure_excerpt,
            failure_provider=failure_provider,
            selector_coverage=selector_coverage,
            selectors_unavailable_reason=selectors_unavailable_reason,
            failed_selectors=failed_selectors,
            failure_locations=failure_locations,
            output_artifact_reference=output_artifact_reference,
            output_artifact_status=output_artifact_status,
            remote_version=remote_version,
            delta_token=token,
            changed_since=changed,
            truncated=truncated,
        )


__all__ = [
    "WorkspacePrEvidenceCommand",
    "WorkspacePrEvidenceReader",
    "WorkspacePrEvidenceResult",
]
