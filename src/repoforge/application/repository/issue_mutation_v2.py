"""Governed, idempotent GitHub-native issue mutations for the Forge v2 repo_issue tool."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from ...domain.approval import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalSubject,
)
from ...domain.errors import ConfigError
from ...domain.issue_writes import IssueLinkType
from ...domain.redaction import redact_text
from ..context import ApplicationContext
from ..idempotency import IdempotencyEffectBoundary

_WRITE_MODES = frozenset({"comment", "close", "reopen", "link", "create"})
_MAX_RECONCILIATION_ITEMS = 100


@dataclass(frozen=True, slots=True)
class RepositoryIssueMutationCommand:
    repo_id: str
    mode: str
    issue_number: int | None = None
    body: str | None = None
    title: str | None = None
    evidence_ref: str | None = None
    target_issue: int | None = None
    link_type: str | None = None
    idempotency_key: str | None = None
    approval_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryIssueMutationResult:
    summary: str
    operation: str
    result: str
    issue_number: int | None
    target_issue: int | None
    link_type: str | None
    marker: str
    external_writes: int
    idempotent_replay: bool
    approval_request_id: str | None
    url: str | None


class RepositoryIssueMutatorV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx

    def execute(self, command: RepositoryIssueMutationCommand) -> RepositoryIssueMutationResult:
        repo = self.ctx.repo(command.repo_id)
        policy = repo.issue_writes
        normalized = self._normalize(command)
        operation = str(normalized["mode"])
        if operation not in _WRITE_MODES:
            raise ValueError("repo_issue write mode is invalid")
        if repo.read_only or not repo.publish_enabled:
            raise ConfigError("Repository policy does not allow GitHub issue mutations")
        if not policy.allows(operation):
            raise ConfigError(
                f"repo_issue {operation} is not enabled for repository {repo.repo_id}"
            )
        estimated_writes = 2 if operation in {"close", "reopen"} else 1
        if estimated_writes > policy.max_writes_per_call:
            raise ConfigError(
                "EXTERNAL_MUTATION_RATE_LIMIT: issue mutation exceeds the per-call write limit"
            )
        marker = self._marker(normalized)
        approval_payload: dict[str, object] = {
            "kind": "repo_issue_write_v2",
            "request": normalized,
            "policy": policy.as_table(),
            "marker": marker,
        }
        approval = self._approval(
            command,
            approval_payload,
            marker,
            required=policy.requires_approval(operation),
        )
        if approval is not None:
            return approval

        boundary = IdempotencyEffectBoundary()

        def reserve(effect: str) -> None:
            self.ctx.external_mutation_ledger().reserve(
                repo.repo_id,
                f"{marker}:{effect}",
                count=1,
                now_epoch=self.ctx.now_epoch(),
                max_in_window=policy.max_writes_per_window,
                window_seconds=policy.window_seconds,
            )

        def reconcile() -> RepositoryIssueMutationResult | None:
            return self._reconcile(repo.path, normalized, marker)

        def mutate() -> RepositoryIssueMutationResult:
            existing = reconcile()
            if existing is not None:
                return existing
            gateway = self.ctx.issue_mutation_gateway()
            issue_number = self._optional_int(normalized.get("issue_number"))
            evidence_ref = str(normalized["evidence_ref"])
            writes = 0
            url: str | None = None
            resulting_issue = issue_number
            if operation == "comment":
                assert issue_number is not None
                reserve("comment")
                boundary.begin()
                posted = gateway.issue_comment(
                    repo.path,
                    issue_number,
                    self._comment_body(str(normalized["body"]), evidence_ref, marker),
                )
                writes += 1
                url = posted.url
            elif operation in {"close", "reopen"}:
                assert issue_number is not None
                desired_state = "closed" if operation == "close" else "open"
                marker_comment = self._find_marker_comment(repo.path, issue_number, marker)
                if marker_comment is None:
                    reserve("evidence_comment")
                    boundary.begin()
                    posted = gateway.issue_comment(
                        repo.path,
                        issue_number,
                        self._state_evidence_body(operation, evidence_ref, marker),
                    )
                    writes += 1
                    url = posted.url
                issue = gateway.issue_details(repo.path, issue_number)
                if issue.state.lower() != desired_state:
                    reserve("state")
                    boundary.begin()
                    issue = gateway.set_issue_state(repo.path, issue_number, desired_state)
                    writes += 1
                url = issue.url or url
            elif operation == "create":
                title = str(normalized["title"])
                if not title.startswith(policy.create_title_prefix):
                    title = f"{policy.create_title_prefix} {title}".strip()
                rendered = policy.render_create_body(
                    body=str(normalized["body"]),
                    evidence_ref=evidence_ref,
                )
                reserve("create")
                boundary.begin()
                issue = gateway.create_issue(repo.path, title, f"{rendered}\n\n{marker}")
                writes += 1
                resulting_issue = issue.issue_number
                url = issue.url
            else:
                assert operation == "link"
                assert issue_number is not None
                target_issue = self._required_int(normalized.get("target_issue"), "target_issue")
                link_type = str(normalized["link_type"])
                target = gateway.issue_details(repo.path, target_issue)
                reserve("relationship")
                boundary.begin()
                if link_type == IssueLinkType.SUB_ISSUE.value:
                    linked = gateway.add_sub_issue(repo.path, issue_number, target.database_id)
                    url = linked.url
                elif link_type == IssueLinkType.BLOCKED_BY.value:
                    linked = gateway.add_blocked_by(repo.path, issue_number, target.database_id)
                    url = linked.url
                else:
                    assert link_type == IssueLinkType.SUPERSEDE.value
                    posted = gateway.issue_comment(
                        repo.path,
                        issue_number,
                        self._supersede_body(target_issue, evidence_ref, marker),
                    )
                    url = posted.url
                writes += 1
            return RepositoryIssueMutationResult(
                f"Applied repo_issue {operation}",
                operation,
                "applied",
                resulting_issue,
                self._optional_int(normalized.get("target_issue")),
                self._optional_text(normalized.get("link_type")),
                marker,
                writes,
                False,
                command.approval_request_id,
                url,
            )

        return self.ctx.idempotent(
            "repo_issue",
            command.idempotency_key,
            normalized,
            mutate,
            details={
                "repo_id": repo.repo_id,
                "mode": operation,
                "issue_number": normalized.get("issue_number"),
                "target_issue": normalized.get("target_issue"),
                "marker_hash": marker.removeprefix("<!-- repoforge-issue-write:").removesuffix(
                    " -->"
                )[:16],
                "external_write_ceiling": estimated_writes,
            },
            serialize=asdict,
            deserialize=self._deserialize,
            effect_boundary=boundary,
            reconcile_uncertain=reconcile,
        )

    def _approval(
        self,
        command: RepositoryIssueMutationCommand,
        payload: dict[str, object],
        marker: str,
        *,
        required: bool,
    ) -> RepositoryIssueMutationResult | None:
        if not required:
            if command.approval_request_id is not None:
                raise ConfigError("approval_request_id is not valid for this repository policy")
            return None
        approvals, payloads = self.ctx.approval_stores()
        digest = payloads.digest(payload)
        envelope = None
        if command.approval_request_id is not None:
            envelope = approvals.read(command.approval_request_id)
            if envelope is None:
                raise ConfigError("Unknown approval_request_id")
        else:
            page = approvals.list_records(max_records=100)
            envelope = next(
                (
                    item
                    for item in page.records
                    if item.value.action == "repo_issue_write"
                    and item.value.subject.repo_id == command.repo_id
                    and item.value.binding.payload_digest == digest
                    and item.value.status in {ApprovalStatus.PENDING, ApprovalStatus.ACCEPTED}
                ),
                None,
            )
            if envelope is None and page.scan_truncated:
                raise ConfigError(
                    "Approval reconciliation is incomplete; review the bounded approval store before retrying"
                )
            if envelope is None:
                request_id = f"apr-{self.ctx.ids.new_hex(24)}"
                request = ApprovalRequest(
                    request_id,
                    "repo_issue_write",
                    ApprovalSubject(
                        "issue_mutation",
                        command.repo_id,
                        f"Approve repo_issue {command.mode}",
                        "external_write",
                    ),
                    ApprovalBinding(f"issue-write-{digest[:24]}", digest),
                    "Repository policy requires operator approval for this issue mutation.",
                    self.ctx.clock.now_iso(),
                    None,
                )
                payloads.save(request_id, payload)
                try:
                    envelope = approvals.create(request)
                except Exception:
                    payloads.delete(request_id)
                    raise
        request = envelope.value
        stored_payload = payloads.read(request.request_id)
        if (
            request.action != "repo_issue_write"
            or request.subject.kind != "issue_mutation"
            or request.subject.repo_id != command.repo_id
            or request.binding.payload_digest != digest
            or stored_payload != payload
        ):
            raise ConfigError("Approval request does not match the exact issue mutation payload")
        if request.status is ApprovalStatus.ACCEPTED:
            return None
        if request.status is not ApprovalStatus.PENDING:
            raise ConfigError(f"Issue mutation approval is {request.status.value}")
        result = RepositoryIssueMutationResult(
            f"Operator approval is required for repo_issue {command.mode}",
            command.mode,
            "pending_approval",
            command.issue_number,
            command.target_issue,
            command.link_type,
            marker,
            0,
            False,
            request.request_id,
            None,
        )
        return self.ctx.audited(
            "repo_issue",
            {
                "repo_id": command.repo_id,
                "mode": command.mode,
                "approval_request_id": request.request_id,
                "approval_status": request.status.value,
                "external_write_ceiling": 0,
            },
            lambda: result,
            mutating=False,
        )

    def _reconcile(
        self,
        repo_path: Any,
        request: dict[str, object],
        marker: str,
    ) -> RepositoryIssueMutationResult | None:
        gateway = self.ctx.issue_mutation_gateway()
        operation = str(request["mode"])
        issue_number = self._optional_int(request.get("issue_number"))
        target_issue = self._optional_int(request.get("target_issue"))
        link_type = self._optional_text(request.get("link_type"))
        url: str | None = None
        resulting_issue = issue_number
        if operation == "create":
            issues, truncated = gateway.recent_issues(
                repo_path, max_issues=_MAX_RECONCILIATION_ITEMS
            )
            found = next((item for item in issues if marker in item.body), None)
            if found is None and truncated:
                raise ConfigError(
                    "Issue creation reconciliation is incomplete; refusing a blind retry"
                )
            if found is None:
                return None
            resulting_issue = found.issue_number
            url = found.url
        elif operation == "link" and link_type in {
            IssueLinkType.SUB_ISSUE.value,
            IssueLinkType.BLOCKED_BY.value,
        }:
            assert issue_number is not None and target_issue is not None
            if link_type == IssueLinkType.SUB_ISSUE.value:
                linked, truncated = gateway.sub_issues(
                    repo_path,
                    issue_number,
                    max_issues=_MAX_RECONCILIATION_ITEMS,
                )
            else:
                linked, truncated = gateway.blocked_by(
                    repo_path,
                    issue_number,
                    max_issues=_MAX_RECONCILIATION_ITEMS,
                )
            found_issue = next((item for item in linked if item.issue_number == target_issue), None)
            if found_issue is None and truncated:
                raise ConfigError(
                    "Issue relationship reconciliation is incomplete; refusing a blind retry"
                )
            if found_issue is None:
                return None
            url = found_issue.url
        else:
            assert issue_number is not None
            comment = self._find_marker_comment(repo_path, issue_number, marker)
            if comment is None:
                return None
            url = comment.url
            if operation in {"close", "reopen"}:
                desired = "closed" if operation == "close" else "open"
                issue = gateway.issue_details(repo_path, issue_number)
                if issue.state.lower() != desired:
                    return None
                url = issue.url or url
        return RepositoryIssueMutationResult(
            f"Reconciled existing repo_issue {operation} effect",
            operation,
            "reconciled",
            resulting_issue,
            target_issue,
            link_type,
            marker,
            0,
            True,
            None,
            url,
        )

    def _find_marker_comment(self, repo_path: Any, issue_number: int, marker: str) -> Any | None:
        comments, truncated = self.ctx.issue_mutation_gateway().issue_comments(
            repo_path,
            issue_number,
            max_comments=_MAX_RECONCILIATION_ITEMS,
        )
        found = next((comment for comment in comments if marker in comment.body), None)
        if found is None and truncated:
            raise ConfigError("Issue comment reconciliation is incomplete; refusing a blind retry")
        return found

    @staticmethod
    def _deserialize(payload: Any) -> RepositoryIssueMutationResult:
        if not isinstance(payload, dict):
            raise ConfigError("Stored repo_issue idempotency result is invalid")
        return RepositoryIssueMutationResult(**payload)

    @staticmethod
    def _marker(request: dict[str, object]) -> str:
        digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"<!-- repoforge-issue-write:{digest} -->"

    @staticmethod
    def _normalize(command: RepositoryIssueMutationCommand) -> dict[str, object]:
        if command.mode not in _WRITE_MODES:
            raise ValueError("repo_issue write mode is invalid")
        if command.idempotency_key is None or len(command.idempotency_key) < 8:
            raise ConfigError("Every repo_issue write requires an idempotency_key")
        if command.evidence_ref is None:
            raise ConfigError("Every repo_issue write requires evidence_ref")
        if command.mode != "create" and command.issue_number is None:
            raise ConfigError(f"repo_issue {command.mode} requires issue_number")
        if command.mode == "comment" and command.body is None:
            raise ConfigError("repo_issue comment requires body")
        if command.mode == "create" and (command.title is None or command.body is None):
            raise ConfigError("repo_issue create requires title and body")
        if command.mode == "link":
            if command.target_issue is None or command.link_type not in {
                item.value for item in IssueLinkType
            }:
                raise ConfigError("repo_issue link requires target_issue and a supported link_type")
            if command.target_issue == command.issue_number:
                raise ConfigError("repo_issue cannot link an issue to itself")
        elif command.target_issue is not None or command.link_type is not None:
            raise ConfigError("target_issue and link_type are only valid for repo_issue link")
        if command.mode != "create" and command.title is not None:
            raise ConfigError("title is only valid for repo_issue create")
        if command.mode not in {"comment", "create"} and command.body is not None:
            raise ConfigError("body is only valid for repo_issue comment or create")
        return {
            "repo_id": command.repo_id,
            "mode": command.mode,
            "issue_number": command.issue_number,
            "body": redact_text(command.body or "", limit=16_000) or None,
            "title": redact_text(command.title or "", limit=1_000) or None,
            "evidence_ref": redact_text(command.evidence_ref, limit=1_000),
            "target_issue": command.target_issue,
            "link_type": command.link_type,
        }

    @staticmethod
    def _comment_body(body: str, evidence_ref: str, marker: str) -> str:
        return f"{body}\n\nEvidence: {evidence_ref}\n\n{marker}"

    @staticmethod
    def _state_evidence_body(operation: str, evidence_ref: str, marker: str) -> str:
        return f"RepoForge {operation} evidence: {evidence_ref}\n\n{marker}"

    @staticmethod
    def _supersede_body(target_issue: int, evidence_ref: str, marker: str) -> str:
        return f"Duplicate of #{target_issue}\n\nEvidence: {evidence_ref}\n\n{marker}"

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def _required_int(cls, value: object, name: str) -> int:
        selected = cls._optional_int(value)
        if selected is None:
            raise ConfigError(f"{name} is required")
        return selected

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None
