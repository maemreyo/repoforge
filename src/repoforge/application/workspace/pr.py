"""Consolidated, typed pull-request workflow orchestration for Forge v2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ...config import RepositoryConfig
from ...domain.errors import ConfigError, ErrorCode, RepoForgeError
from ...domain.operations import IdempotencyState, hash_idempotency_key
from ...domain.pr_check_watch import TERMINAL_PR_CHECK_WATCH_OUTCOMES
from ...domain.pr_completion import (
    PrIssueIntent,
    completion_evidence,
    intent_from_payload,
    intent_payload,
    normalize_disposition_requests,
    render_pr_issue_intent,
    require_complete_dispositions,
    workspace_issue_numbers,
)
from ...domain.pr_remote_version import (
    PrRemoteVersion,
    build_pr_remote_version,
    pr_remote_version_recovery_details,
)
from ...domain.redaction import redact_text
from ...domain.workspace import WorkspaceRecord
from ..context import ApplicationContext
from ..dto import to_data
from ..extended_context import external_mutation_ledger
from ..idempotency import IdempotencyEffectBoundary, execute_idempotent
from ..repository.issue_mutation_v2 import (
    RepositoryIssueMutationCommand,
    RepositoryIssueMutatorV2,
)
from .create_draft_pr import DraftPullRequestCreator, WorkspaceCreateDraftPrCommand
from .pr_watch import PrCheckWatchCoordinator, WorkspacePrWatchCommand
from .update_draft_pr import DraftPullRequestUpdater, WorkspaceUpdateDraftPrCommand

_ACTIONS = frozenset({"create_draft", "update", "comment", "watch", "reconcile"})
_MAX_RECONCILIATION_COMMENTS = 100


@dataclass(frozen=True, slots=True)
class WorkspacePrCommand:
    workspace_id: str
    action: str
    title: str | None = None
    body: str | None = None
    evidence_ref: str | None = None
    review_comment_id: int | None = None
    idempotency_key: str | None = None
    expected_remote_version: str | None = None
    until: str = "all_completed"
    timeout_seconds: int = 900
    event_cursor: str | None = None
    issue_dispositions: tuple[dict[str, object], ...] = ()
    apply_closures: bool = False


@dataclass(frozen=True, slots=True)
class WorkspacePrCommentResult:
    result: str
    url: str | None
    marker: str
    idempotent_replay: bool
    review_comment_id: int | None


@dataclass(frozen=True, slots=True)
class WorkspacePrResult:
    summary: str
    workspace_id: str
    action: str
    pull_request: dict[str, object] | None
    comment: dict[str, object] | None
    operation: dict[str, object] | None
    remote_version: str | None
    event_cursor: str | None
    terminal_reason: str | None
    issue_completion: dict[str, object] | None = None
    reconciliation: dict[str, object] | None = None


def _remote_version(payload: dict[str, Any], *, repo_id: str) -> str:
    return build_pr_remote_version(payload, repo_id=repo_id).token


def _pull_request(payload: dict[str, Any], *, base_ref: str) -> dict[str, object]:
    number = payload.get("number")
    title = payload.get("title")
    state = payload.get("state")
    draft = payload.get("isDraft")
    head_sha = payload.get("headRefOid")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise RepoForgeError(
            "GitHub returned no stable pull-request number",
            code=ErrorCode.PR_CHECK_WATCH_UNAVAILABLE,
            retryable=True,
        )
    if not isinstance(title, str) or not title:
        raise RepoForgeError("GitHub returned no pull-request title", code=ErrorCode.STATE_INVALID)
    if not isinstance(state, str) or not state:
        raise RepoForgeError("GitHub returned no pull-request state", code=ErrorCode.STATE_INVALID)
    if not isinstance(draft, bool):
        raise RepoForgeError("GitHub returned no draft state", code=ErrorCode.STATE_INVALID)
    if (
        not isinstance(head_sha, str)
        or len(head_sha) != 40
        or any(character not in "0123456789abcdefABCDEF" for character in head_sha)
    ):
        raise RepoForgeError(
            "GitHub returned no exact pull-request head SHA",
            code=ErrorCode.PR_CHECK_WATCH_UNAVAILABLE,
            retryable=True,
        )
    review = payload.get("reviewDecision")
    return {
        "number": number,
        "title": title[:1_000],
        "state": state[:80],
        "draft": draft,
        "head_sha": head_sha.lower(),
        "base_ref": base_ref,
        "review_decision": review[:80] if isinstance(review, str) and review else None,
        "freshness": "live",
    }


def _operation_evidence(summary: object, *, poll_after_seconds: float) -> dict[str, object]:
    raw = to_data(summary)
    progress = raw.get("progress") if isinstance(raw, dict) else None
    current = progress.get("current") if isinstance(progress, dict) else None
    total = progress.get("total") if isinstance(progress, dict) else None
    return {
        "operation_id": str(raw["operation_id"]),
        "kind": str(raw["kind"]),
        "state": str(raw["state"]),
        "phase": str(raw["phase"]),
        "progress_current": current if isinstance(current, int) else None,
        "progress_total": total if isinstance(total, int) else None,
        "cancellation_reason": None,
        "poll_after_seconds": max(0.1, min(60.0, poll_after_seconds)),
    }


def _event_cursor(operation_id: str, updated_at: str, outcome: str) -> str:
    digest = hashlib.sha256(f"{operation_id}\0{updated_at}\0{outcome}".encode()).hexdigest()
    return f"pr-watch:{operation_id}:{digest}"


def _operation_id(cursor: str) -> str:
    parts = cursor.split(":", 2)
    if len(parts) != 3 or parts[0] != "pr-watch" or not parts[1]:
        raise ConfigError("workspace_pr watch event_cursor is invalid")
    return parts[1]


class WorkspacePrCoordinator:
    def __init__(
        self,
        ctx: ApplicationContext,
        *,
        creator: DraftPullRequestCreator,
        updater: DraftPullRequestUpdater,
        watch: PrCheckWatchCoordinator,
    ) -> None:
        self.ctx = ctx
        self.creator = creator
        self.updater = updater
        self.watch = watch

    def _stored_issue_intents(self, record: WorkspaceRecord) -> tuple[PrIssueIntent, ...]:
        raw = record.metadata.get("pr_issue_intents")
        if raw is None:
            return ()
        if not isinstance(raw, list):
            return (intent_from_payload(raw),)
        return tuple(intent_from_payload(item) for item in raw)

    def _issue_intents(
        self,
        record: WorkspaceRecord,
        path: Path,
        raw_dispositions: tuple[dict[str, object], ...],
    ) -> tuple[PrIssueIntent, ...]:
        workspace_numbers = workspace_issue_numbers(record.metadata.get("issue_ids"))
        requests = normalize_disposition_requests(raw_dispositions)
        if not workspace_numbers:
            if requests:
                require_complete_dispositions((), requests)
            return ()
        if not requests:
            stored = self._stored_issue_intents(record)
            stored_requests = normalize_disposition_requests(
                tuple(
                    {
                        "issue_number": item.issue_number,
                        "disposition": item.disposition,
                        "acceptance_evidence_ref": item.acceptance_evidence_ref,
                    }
                    for item in stored
                )
            )
            require_complete_dispositions(workspace_numbers, stored_requests)
            return stored

        require_complete_dispositions(workspace_numbers, requests)
        intents: list[PrIssueIntent] = []
        for request in requests:
            snapshot = self.ctx.github.issue_read(path, request.issue_number)
            title = snapshot.get("title")
            state = snapshot.get("state")
            url = snapshot.get("url")
            if not isinstance(title, str) or not isinstance(state, str) or not isinstance(url, str):
                raise RepoForgeError(
                    "GitHub returned an incomplete issue snapshot for PR completion intent",
                    code=ErrorCode.STATE_INVALID,
                    details={"issue_number": request.issue_number},
                )
            intents.append(
                PrIssueIntent(
                    issue_number=request.issue_number,
                    disposition=request.disposition,
                    acceptance_evidence_ref=request.acceptance_evidence_ref,
                    snapshot_title=title,
                    snapshot_state=state.lower(),
                    snapshot_url=url,
                )
            )
        record.metadata["pr_issue_intents"] = [intent_payload(item) for item in intents]
        record.metadata["pr_issue_intent_state"] = "accepted"
        self.ctx.store.save(record)
        return tuple(intents)

    def _mark_issue_intent_applied(
        self,
        workspace_id: str,
        intents: tuple[PrIssueIntent, ...],
    ) -> None:
        if not intents:
            return
        record = self.ctx.store.load(workspace_id)
        record.metadata["pr_issue_intent_state"] = "applied"
        self.ctx.store.save(record)

    def execute(self, command: WorkspacePrCommand) -> WorkspacePrResult:
        if command.action not in _ACTIONS:
            raise ConfigError(f"Unknown workspace_pr action {command.action!r}")
        details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "action": command.action,
            "expected_remote_version": command.expected_remote_version is not None,
            "review_reply": command.review_comment_id is not None,
        }
        return self.ctx.audited(
            "workspace_pr",
            details,
            lambda: self._execute(command),
            mutating=True,
        )

    def _status(
        self, workspace_id: str
    ) -> tuple[WorkspaceRecord, RepositoryConfig, Path, dict[str, Any], PrRemoteVersion]:
        record, repo, path = self.ctx.workspace(workspace_id)
        payload = self.ctx.github.status(path, record.branch)
        if payload.get("exists") is False:
            raise RepoForgeError(
                "No pull request exists yet for this workspace branch",
                code=ErrorCode.NOT_FOUND,
            )
        version = build_pr_remote_version(payload, repo_id=record.repo_id)
        return record, repo, path, payload, version

    @staticmethod
    def _assert_remote_version(
        expected: str | None,
        current: PrRemoteVersion,
        payload: dict[str, Any],
    ) -> None:
        if expected is not None and expected != current.token:
            raise RepoForgeError(
                "PR_REMOTE_VERSION_STALE: pull request changed since it was reviewed",
                code=ErrorCode.PR_REMOTE_VERSION_STALE,
                retryable=False,
                safe_next_action=(
                    "Read workspace_pr_evidence overview, copy its remote_version unchanged, "
                    "review the remote delta, and submit a new idempotent write."
                ),
                unchanged_state=("No pull-request write was attempted.",),
                details=pr_remote_version_recovery_details(expected, current, payload),
            )

    def _execute(self, command: WorkspacePrCommand) -> WorkspacePrResult:
        if command.action == "create_draft":
            if command.title is None or command.body is None or command.idempotency_key is None:
                raise ConfigError(
                    "workspace_pr create_draft requires title, body, and idempotency_key"
                )
            draft_record, _repo, draft_path = self.ctx.workspace(command.workspace_id)
            intents = self._issue_intents(
                draft_record,
                draft_path,
                command.issue_dispositions,
            )
            managed_body = render_pr_issue_intent(command.body, intents)
            self.creator.execute(
                WorkspaceCreateDraftPrCommand(
                    command.workspace_id,
                    command.title,
                    managed_body,
                    command.idempotency_key,
                )
            )
            self._mark_issue_intent_applied(command.workspace_id, intents)
            record, _repo, _path, payload, version = self._status(command.workspace_id)
            return WorkspacePrResult(
                summary="Created or reconciled the workspace draft pull request",
                workspace_id=command.workspace_id,
                action=command.action,
                pull_request=_pull_request(payload, base_ref=record.base),
                comment=None,
                operation=None,
                remote_version=version.token,
                event_cursor=None,
                terminal_reason=None,
                issue_completion=completion_evidence(intents),
            )

        if command.action == "update":
            if command.idempotency_key is None or (command.title is None and command.body is None):
                raise ConfigError("workspace_pr update requires title or body and idempotency_key")
            if command.expected_remote_version is None:
                raise ConfigError("workspace_pr update requires expected_remote_version")
            before_record, _repo, before_path, before, before_version = self._status(
                command.workspace_id
            )
            update_key_hash = hash_idempotency_key(command.idempotency_key)
            update_existing = (
                self.ctx.idempotency.load("workspace_update_draft_pr", update_key_hash)
                if self.ctx.idempotency is not None
                else None
            )
            update_replay = (
                update_existing is not None and update_existing.state is IdempotencyState.COMPLETED
            )
            if not update_replay:
                self._assert_remote_version(
                    command.expected_remote_version,
                    before_version,
                    before,
                )
            intents = self._issue_intents(
                before_record,
                before_path,
                command.issue_dispositions,
            )
            update_body = command.body
            if update_body is not None:
                update_body = render_pr_issue_intent(update_body, intents)
            elif command.issue_dispositions:
                current_body = before.get("body")
                if not isinstance(current_body, str):
                    raise RepoForgeError(
                        "GitHub returned no PR body for completion-intent reconciliation",
                        code=ErrorCode.STATE_INVALID,
                    )
                update_body = render_pr_issue_intent(current_body, intents)
            self.updater.execute(
                WorkspaceUpdateDraftPrCommand(
                    command.workspace_id,
                    command.title,
                    update_body,
                    command.idempotency_key,
                )
            )
            self._mark_issue_intent_applied(command.workspace_id, intents)
            record, _repo, _path, payload, version = self._status(command.workspace_id)
            return WorkspacePrResult(
                summary="Updated the workspace draft pull request",
                workspace_id=command.workspace_id,
                action=command.action,
                pull_request=_pull_request(payload, base_ref=record.base),
                comment=None,
                operation=None,
                remote_version=version.token,
                event_cursor=None,
                terminal_reason=None,
                issue_completion=completion_evidence(intents),
            )

        if command.action == "comment":
            return self._comment(command)

        if command.action == "reconcile":
            return self._reconcile(command)

        return self._watch(command)

    def _reconcile(self, command: WorkspacePrCommand) -> WorkspacePrResult:
        if command.expected_remote_version is None:
            raise ConfigError("workspace_pr reconcile requires expected_remote_version")
        if command.apply_closures and command.idempotency_key is None:
            raise ConfigError("workspace_pr reconcile apply_closures requires idempotency_key")
        record, _repo, path, payload, version = self._status(command.workspace_id)
        self._assert_remote_version(command.expected_remote_version, version, payload)
        intents = self._issue_intents(record, path, ())
        merged = (
            str(payload.get("state", "")).lower() == "merged"
            and payload.get("baseRefName") == record.base
        )
        closed_correctly: list[int] = []
        implemented_still_open: list[int] = []
        intentionally_advanced: list[int] = []
        superseded: list[int] = []
        acceptance_review_required: list[int] = []

        if not merged:
            acceptance_review_required.extend(item.issue_number for item in intents)
        else:
            for intent in intents:
                live = self.ctx.github.issue_read(path, intent.issue_number)
                live_state = str(live.get("state", "")).lower()
                if intent.disposition == "closes":
                    target = closed_correctly if live_state == "closed" else implemented_still_open
                    target.append(intent.issue_number)
                elif intent.disposition == "advances":
                    intentionally_advanced.append(intent.issue_number)
                elif intent.disposition == "supersedes":
                    superseded.append(intent.issue_number)
                else:
                    acceptance_review_required.append(intent.issue_number)

        closure_results: list[dict[str, object]] = []
        if command.apply_closures:
            if not merged:
                raise RepoForgeError(
                    "PR completion closures require a merged PR on the configured base branch",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                    unchanged_state=("No issue mutation was attempted.",),
                )
            mutator = RepositoryIssueMutatorV2(self.ctx)
            by_number = {item.issue_number: item for item in intents}
            for issue_number in tuple(implemented_still_open):
                intent = by_number[issue_number]
                digest = hashlib.sha256(
                    f"{command.idempotency_key}:{issue_number}".encode()
                ).hexdigest()[:24]
                applied = mutator.execute(
                    RepositoryIssueMutationCommand(
                        repo_id=record.repo_id,
                        mode="close",
                        issue_number=issue_number,
                        evidence_ref=intent.acceptance_evidence_ref,
                        idempotency_key=f"pr-reconcile-close-{issue_number}-{digest}",
                    )
                )
                closure_results.append(
                    {
                        "issue_number": issue_number,
                        "result": applied.result,
                        "external_writes": applied.external_writes,
                        "marker": applied.marker,
                        "approval_request_id": applied.approval_request_id,
                    }
                )
                live = self.ctx.github.issue_read(path, issue_number)
                if str(live.get("state", "")).lower() == "closed":
                    implemented_still_open.remove(issue_number)
                    closed_correctly.append(issue_number)

        reconciliation: dict[str, object] = {
            "merge_status": "merged" if merged else "not_merged",
            "closed_correctly": sorted(closed_correctly),
            "implemented_still_open": sorted(implemented_still_open),
            "intentionally_advanced": sorted(intentionally_advanced),
            "superseded": sorted(superseded),
            "acceptance_review_required": sorted(acceptance_review_required),
            "closure_results": closure_results,
        }
        return WorkspacePrResult(
            summary="Reconciled merged PR completion intent against live issue state",
            workspace_id=command.workspace_id,
            action=command.action,
            pull_request=_pull_request(payload, base_ref=record.base),
            comment=None,
            operation=None,
            remote_version=version.token,
            event_cursor=None,
            terminal_reason=None,
            issue_completion=completion_evidence(intents),
            reconciliation=reconciliation,
        )

    @staticmethod
    def _comment_marker(request: dict[str, object]) -> str:
        digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"<!-- repoforge-pr-comment:{digest} -->"

    def _comment(self, command: WorkspacePrCommand) -> WorkspacePrResult:
        if (
            command.body is None
            or command.evidence_ref is None
            or command.idempotency_key is None
            or command.expected_remote_version is None
        ):
            raise ConfigError(
                "workspace_pr comment requires body, evidence_ref, idempotency_key, and expected_remote_version"
            )
        record, repo, path, payload, version = self._status(command.workspace_id)
        if repo.read_only or not repo.publish_enabled:
            raise ConfigError("Repository policy does not allow pull-request comments")
        body = redact_text(command.body.strip(), limit=16_000)
        evidence_ref = redact_text(command.evidence_ref.strip(), limit=1_000)
        if not body or not evidence_ref:
            raise ConfigError("workspace_pr comment body and evidence_ref must be non-empty")
        pr_number = int(payload["number"])
        request: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "pr_number": pr_number,
            "body": body,
            "evidence_ref": evidence_ref,
            "review_comment_id": command.review_comment_id,
        }
        marker = self._comment_marker(request)
        rendered = f"{body}\n\nEvidence: {evidence_ref}\n\n{marker}"
        boundary = IdempotencyEffectBoundary()

        def reconcile(*, replay: bool) -> WorkspacePrCommentResult | None:
            comments, _truncated = self.ctx.github.pr_comments(
                path, pr_number, max_comments=_MAX_RECONCILIATION_COMMENTS
            )
            for item in comments:
                if marker in item.body:
                    return WorkspacePrCommentResult(
                        result="reconciled",
                        url=item.url or None,
                        marker=marker,
                        idempotent_replay=replay,
                        review_comment_id=command.review_comment_id,
                    )
            return None

        def operation() -> WorkspacePrCommentResult:
            existing = reconcile(replay=True)
            if existing is not None:
                return existing
            policy = repo.issue_writes
            external_mutation_ledger(self.ctx).reserve(
                repo.repo_id,
                marker,
                count=1,
                now_epoch=self.ctx.now_epoch(),
                max_in_window=policy.max_writes_per_window,
                window_seconds=policy.window_seconds,
            )
            boundary.begin()
            if command.review_comment_id is None:
                created = self.ctx.github.pr_comment(path, pr_number, rendered)
            else:
                created = self.ctx.github.pr_review_reply(path, command.review_comment_id, rendered)
            return WorkspacePrCommentResult(
                result="created",
                url=created.url or None,
                marker=marker,
                idempotent_replay=False,
                review_comment_id=command.review_comment_id,
            )

        key_hash = hash_idempotency_key(command.idempotency_key)
        existing = (
            self.ctx.idempotency.load("workspace_pr_comment", key_hash)
            if self.ctx.idempotency is not None
            else None
        )
        idempotency_replay = existing is not None
        if not idempotency_replay:
            self._assert_remote_version(command.expected_remote_version, version, payload)
        comment = execute_idempotent(
            self.ctx,
            "workspace_pr_comment",
            command.idempotency_key,
            request,
            operation,
            details={
                "workspace_id": command.workspace_id,
                "pr_number": pr_number,
                "review_reply": command.review_comment_id is not None,
            },
            serialize=asdict,
            deserialize=lambda value: WorkspacePrCommentResult(**value),
            effect_boundary=boundary,
            reconcile_uncertain=lambda: reconcile(replay=True),
        )
        if idempotency_replay and not comment.idempotent_replay:
            comment = replace(comment, idempotent_replay=True)
        record, _repo, _path, payload, version = self._status(command.workspace_id)
        return WorkspacePrResult(
            summary="Posted or reconciled a bounded pull-request comment",
            workspace_id=command.workspace_id,
            action=command.action,
            pull_request=_pull_request(payload, base_ref=record.base),
            comment=asdict(comment),
            operation=None,
            remote_version=version.token,
            event_cursor=None,
            terminal_reason=None,
        )

    def _watch(self, command: WorkspacePrCommand) -> WorkspacePrResult:
        if command.event_cursor is None:
            if command.expected_remote_version is None:
                raise ConfigError("workspace_pr watch requires expected_remote_version")
            started = self.watch.start(
                WorkspacePrWatchCommand(
                    command.workspace_id,
                    command.until,
                    command.timeout_seconds,
                    True,
                    command.expected_remote_version,
                )
            )
            operation_id = started.operation.operation_id
        else:
            operation_id = _operation_id(command.event_cursor)
        watch = self.watch.store.read(operation_id)
        if watch is None or watch.workspace_id != command.workspace_id:
            raise RepoForgeError(
                "PR check watch cursor does not belong to this workspace",
                code=ErrorCode.OPERATION_NOT_FOUND,
            )
        if command.event_cursor is not None:
            self.watch.assert_current_identity(watch)
        terminal = watch.outcome in TERMINAL_PR_CHECK_WATCH_OUTCOMES
        cursor = _event_cursor(operation_id, watch.updated_at, watch.outcome.value)
        return WorkspacePrResult(
            summary=(
                f"PR check watch reached {watch.outcome.value}"
                if terminal
                else "Started or resumed the durable PR check watch"
            ),
            workspace_id=command.workspace_id,
            action=command.action,
            pull_request=None,
            comment=None,
            operation=_operation_evidence(
                self.watch.operations.status(operation_id),
                poll_after_seconds=float(watch.next_delay_seconds),
            ),
            remote_version=watch.remote_version,
            event_cursor=cursor,
            terminal_reason=watch.outcome.value if terminal else None,
        )


__all__ = ["WorkspacePrCommand", "WorkspacePrCoordinator", "WorkspacePrResult"]
