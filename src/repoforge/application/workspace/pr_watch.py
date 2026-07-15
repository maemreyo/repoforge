"""Durable exact-SHA pull-request check watch coordination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    TERMINAL_OPERATION_STATES,
    OperationRetryability,
    OperationSnapshotBinding,
)
from ...domain.pr_check_watch import (
    TERMINAL_PR_CHECK_WATCH_OUTCOMES,
    PrCheckWatch,
    PrCheckWatchOutcome,
    PrCheckWatchUntil,
    new_pr_check_watch,
    update_pr_check_watch,
)
from ...ports.background_tasks import BackgroundTaskRunner
from ...ports.pr_check_watch_store import PrCheckWatchStore
from ...ports.sleeper import Sleeper
from ..context import ApplicationContext
from ..operations.dto import OperationSummary, operation_summary
from ..operations.manager import OperationManager
from .pr_failure_evidence import (
    WorkspacePrFailureEvidenceCommand,
    WorkspacePrFailureEvidenceReader,
)


@dataclass(frozen=True, slots=True)
class WorkspacePrWatchCommand:
    workspace_id: str
    until: str = PrCheckWatchUntil.ALL_COMPLETED.value
    timeout_seconds: int = 900
    include_failure_evidence: bool = True


@dataclass(frozen=True, slots=True)
class WorkspacePrWatchResult:
    operation: OperationSummary
    until: str
    deadline_at: str


class PrCheckWatchCoordinator:
    def __init__(
        self,
        ctx: ApplicationContext,
        operations: OperationManager,
        store: PrCheckWatchStore,
        runner: BackgroundTaskRunner,
        sleeper: Sleeper,
    ) -> None:
        self.ctx = ctx
        self.operations = operations
        self.store = store
        self.runner = runner
        self.sleeper = sleeper

    @staticmethod
    def _until(value: str) -> PrCheckWatchUntil:
        try:
            return PrCheckWatchUntil(value)
        except ValueError as exc:
            raise RepoForgeError(
                "PR check watch until must be all_completed or first_failure",
                code=ErrorCode.PR_CHECK_WATCH_INVALID,
            ) from exc

    @staticmethod
    def _timeout(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 5 <= value <= 7_200:
            raise RepoForgeError(
                "PR check watch timeout_seconds must be between 5 and 7200",
                code=ErrorCode.PR_CHECK_WATCH_INVALID,
            )
        return value

    @staticmethod
    def _pr_number(payload: dict[str, Any]) -> int:
        value = payload.get("number")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RepoForgeError(
                "GitHub returned no stable pull-request number",
                code=ErrorCode.PR_CHECK_WATCH_UNAVAILABLE,
                retryable=True,
            )
        return value

    @staticmethod
    def _pr_head(payload: dict[str, Any]) -> str:
        value = payload.get("headRefOid")
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise RepoForgeError(
                "GitHub returned no exact pull-request head SHA",
                code=ErrorCode.PR_CHECK_WATCH_UNAVAILABLE,
                retryable=True,
            )
        return value.lower()

    @staticmethod
    def _deadline(now: str, timeout_seconds: int) -> str:
        parsed = datetime.fromisoformat(now)
        if parsed.tzinfo is None:
            raise RepoForgeError(
                "Clock returned a timestamp without a timezone offset",
                code=ErrorCode.CONFIG_INVALID,
            )
        return (parsed + timedelta(seconds=timeout_seconds)).isoformat()

    def start(self, command: WorkspacePrWatchCommand) -> WorkspacePrWatchResult:
        until = self._until(command.until)
        timeout = self._timeout(command.timeout_seconds)
        if not isinstance(command.include_failure_evidence, bool):
            raise RepoForgeError(
                "include_failure_evidence must be a boolean",
                code=ErrorCode.PR_CHECK_WATCH_INVALID,
            )
        details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "until": until.value,
            "timeout_seconds": timeout,
            "include_failure_evidence": command.include_failure_evidence,
        }

        def op() -> WorkspacePrWatchResult:
            record, _repo, path = self.ctx.workspace(command.workspace_id)
            with self.ctx.locks.lock(command.workspace_id):
                fresh = self.ctx.store.load(command.workspace_id)
                pushed = fresh.metadata.get("last_pushed_sha")
                head = self.ctx.git.head_sha(path)
                fingerprint = self.ctx.git.fingerprint(path)
                if not isinstance(pushed, str) or pushed != head:
                    raise RepoForgeError(
                        "PR check watch requires the exact current workspace commit to be pushed",
                        code=ErrorCode.PR_CHECK_WATCH_STALE,
                    )
                pr = self.ctx.github.status(path, fresh.branch)
                pr_number = self._pr_number(pr)
                if self._pr_head(pr) != pushed:
                    raise RepoForgeError(
                        "Pull-request head does not match the exact pushed workspace commit",
                        code=ErrorCode.PR_CHECK_WATCH_STALE,
                    )
                now = self.ctx.clock.now_iso()
                deadline = self._deadline(now, timeout)
                task = self.operations.create(
                    kind="pr_check_watch",
                    phase="queued",
                    cancel_supported=True,
                    workspace_id=command.workspace_id,
                    snapshot_binding=OperationSnapshotBinding(
                        head_sha=head,
                        workspace_fingerprint=fingerprint,
                    ),
                    expires_at=deadline,
                    now=now,
                )
                details["operation_id"] = task.operation_id
                details["pr_number"] = pr_number
                details["deadline_at"] = deadline
                watch = new_pr_check_watch(
                    operation_id=task.operation_id,
                    workspace_id=command.workspace_id,
                    branch=record.branch,
                    pr_number=pr_number,
                    pushed_sha=pushed,
                    workspace_fingerprint=fingerprint,
                    until=until,
                    include_failure_evidence=command.include_failure_evidence,
                    timeout_seconds=timeout,
                    created_at=now,
                    deadline_at=deadline,
                )
                try:
                    self.store.create(watch)
                    started = self.operations.start(task.operation_id, now=now)
                except Exception:
                    self.operations.fail(
                        task.operation_id,
                        error_code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT.value,
                        error_message="Durable PR check watch state could not be created",
                        now=now,
                    )
                    raise
            self.schedule(task.operation_id)
            return WorkspacePrWatchResult(
                operation_summary(started),
                until.value,
                deadline,
            )

        return self.ctx.audited("workspace_pr_watch", details, op, mutating=True)

    def schedule(self, operation_id: str) -> bool:
        return self.runner.submit(
            operation_id,
            lambda: self.run_to_terminal(operation_id),
        )

    def resume_active(self) -> tuple[str, ...]:
        scheduled: list[str] = []
        for watch in self.store.list_records(max_records=2_000).records:
            task = self.operations.status(watch.operation_id)
            if (
                task.state not in TERMINAL_OPERATION_STATES
                and watch.outcome not in TERMINAL_PR_CHECK_WATCH_OUTCOMES
                and self.schedule(watch.operation_id)
            ):
                scheduled.append(watch.operation_id)
        return tuple(scheduled)

    def _save(
        self,
        watch: PrCheckWatch,
        *,
        now: str,
        poll_count: int,
        pass_count: int | None = None,
        fail_count: int | None = None,
        pending_count: int | None = None,
        skipping_count: int | None = None,
        selectors: tuple[str, ...] | None = None,
        failed_selectors: tuple[str, ...] | None = None,
        evidence_references: tuple[str, ...] | None = None,
        next_delay_seconds: int | None = None,
        provider_error_code: str | None = None,
        outcome: PrCheckWatchOutcome = PrCheckWatchOutcome.PENDING,
    ) -> PrCheckWatch:
        updated = update_pr_check_watch(
            watch,
            now=now,
            poll_count=poll_count,
            pass_count=watch.pass_count if pass_count is None else pass_count,
            fail_count=watch.fail_count if fail_count is None else fail_count,
            pending_count=(watch.pending_count if pending_count is None else pending_count),
            skipping_count=(watch.skipping_count if skipping_count is None else skipping_count),
            selectors=watch.selectors if selectors is None else selectors,
            failed_selectors=(
                watch.failed_selectors if failed_selectors is None else failed_selectors
            ),
            evidence_references=(
                watch.evidence_references if evidence_references is None else evidence_references
            ),
            next_delay_seconds=(
                watch.next_delay_seconds if next_delay_seconds is None else next_delay_seconds
            ),
            provider_error_code=provider_error_code,
            outcome=outcome,
        )
        return self.store.save(updated, expected_updated_at=watch.updated_at)

    def _terminal_failure(
        self,
        watch: PrCheckWatch,
        *,
        code: ErrorCode,
        message: str,
        outcome: PrCheckWatchOutcome,
    ) -> bool:
        now = self.ctx.clock.now_iso()
        self._save(
            watch,
            now=now,
            poll_count=watch.poll_count,
            provider_error_code=code.value,
            outcome=outcome,
        )
        self.operations.fail(
            watch.operation_id,
            error_code=code.value,
            error_message=message,
            retryability=OperationRetryability.MANUAL,
            now=now,
        )
        return True

    def _identity(self, watch: PrCheckWatch) -> tuple[Path, dict[str, Any]]:
        record, _repo, path = self.ctx.workspace(watch.workspace_id)
        if record.branch != watch.branch:
            raise RepoForgeError(
                "Workspace branch changed during PR check watch",
                code=ErrorCode.PR_CHECK_WATCH_STALE,
            )
        pushed = record.metadata.get("last_pushed_sha")
        if (
            self.ctx.git.head_sha(path) != watch.pushed_sha
            or pushed != watch.pushed_sha
            or self.ctx.git.fingerprint(path) != watch.workspace_fingerprint
        ):
            raise RepoForgeError(
                "Workspace identity changed during PR check watch",
                code=ErrorCode.PR_CHECK_WATCH_STALE,
            )
        pr = self.ctx.github.status(path, watch.branch)
        if self._pr_number(pr) != watch.pr_number or self._pr_head(pr) != watch.pushed_sha:
            raise RepoForgeError(
                "Pull-request identity changed during PR check watch",
                code=ErrorCode.PR_CHECK_WATCH_STALE,
            )
        return path, pr

    def _failure_references(
        self,
        watch: PrCheckWatch,
        selectors: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not watch.include_failure_evidence:
            return ()
        reader = WorkspacePrFailureEvidenceReader(self.ctx)
        references: list[str] = []
        for selector in selectors[:20]:
            try:
                reader.execute(
                    WorkspacePrFailureEvidenceCommand(
                        workspace_id=watch.workspace_id,
                        check_selector=selector,
                    )
                )
            except Exception:
                continue
            references.append(selector)
        return tuple(references)

    def run_once(self, operation_id: str) -> bool:
        watch = self.store.read(operation_id)
        if watch is None:
            task = self.operations.status(operation_id)
            if task.state not in TERMINAL_OPERATION_STATES:
                self.operations.fail(
                    operation_id,
                    error_code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT.value,
                    error_message="Durable PR check watch state is missing",
                )
            return True
        task = self.operations.status(operation_id)
        if task.state in TERMINAL_OPERATION_STATES:
            return True
        now = self.ctx.clock.now_iso()
        if task.cancellation_requested_at is not None:
            self._save(
                watch,
                now=now,
                poll_count=watch.poll_count,
                outcome=PrCheckWatchOutcome.CANCELLED,
            )
            self.operations.cancelled(operation_id, now=now)
            return True
        if datetime.fromisoformat(now) >= datetime.fromisoformat(watch.deadline_at):
            return self._terminal_failure(
                watch,
                code=ErrorCode.PR_CHECK_WATCH_TIMEOUT,
                message="PR checks did not reach the requested condition before the deadline",
                outcome=PrCheckWatchOutcome.TIMED_OUT,
            )
        try:
            path, _pr = self._identity(watch)
            checks = self.ctx.github.checks(path, watch.branch, required_only=False)
        except RepoForgeError as exc:
            if exc.code is ErrorCode.PR_CHECK_WATCH_STALE:
                return self._terminal_failure(
                    watch,
                    code=ErrorCode.PR_CHECK_WATCH_STALE,
                    message=str(exc),
                    outcome=PrCheckWatchOutcome.FAILED,
                )
            delay = min(30, max(1, watch.next_delay_seconds * 2))
            saved = self._save(
                watch,
                now=now,
                poll_count=watch.poll_count + 1,
                next_delay_seconds=delay,
                provider_error_code=ErrorCode.PR_CHECK_WATCH_UNAVAILABLE.value,
            )
            self.operations.progress(
                operation_id,
                phase="retrying",
                current=saved.poll_count,
                unit="polls",
                message="GitHub check evidence is temporarily unavailable",
                now=now,
            )
            return False
        except Exception:
            delay = min(30, max(1, watch.next_delay_seconds * 2))
            saved = self._save(
                watch,
                now=now,
                poll_count=watch.poll_count + 1,
                next_delay_seconds=delay,
                provider_error_code=ErrorCode.PR_CHECK_WATCH_UNAVAILABLE.value,
            )
            self.operations.progress(
                operation_id,
                phase="retrying",
                current=saved.poll_count,
                unit="polls",
                message="GitHub check evidence is temporarily unavailable",
                now=now,
            )
            return False

        counts = {"pass": 0, "fail": 0, "pending": 0, "skipping": 0}
        selectors: list[str] = []
        failed: list[str] = []
        for item in checks[:200]:
            bucket = str(item.get("bucket", "pending")).lower()
            if bucket not in counts:
                bucket = "pending"
            counts[bucket] += 1
            item_head = item.get("head_sha")
            if isinstance(item_head, str) and item_head.lower() != watch.pushed_sha:
                return self._terminal_failure(
                    watch,
                    code=ErrorCode.PR_CHECK_WATCH_STALE,
                    message="A returned Check Run belongs to a different commit",
                    outcome=PrCheckWatchOutcome.FAILED,
                )
            selector = item.get("selector")
            if isinstance(selector, str):
                selectors.append(selector)
                if bucket == "fail":
                    failed.append(selector)
        ordered_selectors = tuple(sorted(set(selectors)))
        ordered_failed = tuple(sorted(set(failed)))[:20]
        outcome = PrCheckWatchOutcome.PENDING
        terminal = False
        if watch.until is PrCheckWatchUntil.FIRST_FAILURE and counts["fail"]:
            outcome = PrCheckWatchOutcome.FIRST_FAILURE
            terminal = True
        elif checks and counts["pending"] == 0:
            outcome = PrCheckWatchOutcome.ALL_COMPLETED
            terminal = True
        references = self._failure_references(watch, ordered_failed) if terminal else ()
        saved = self._save(
            watch,
            now=now,
            poll_count=watch.poll_count + 1,
            pass_count=counts["pass"],
            fail_count=counts["fail"],
            pending_count=counts["pending"],
            skipping_count=counts["skipping"],
            selectors=ordered_selectors,
            failed_selectors=ordered_failed,
            evidence_references=references,
            next_delay_seconds=min(30, max(1, watch.next_delay_seconds * 2)),
            provider_error_code=None,
            outcome=outcome,
        )
        self.operations.progress(
            operation_id,
            phase="completed" if terminal else "polling",
            current=saved.poll_count,
            unit="polls",
            message=(
                f"pass={saved.pass_count} fail={saved.fail_count} "
                f"pending={saved.pending_count} skipping={saved.skipping_count}"
            ),
            now=now,
        )
        if terminal:
            self.operations.succeed(
                operation_id,
                result_reference=f"pr-watch:{operation_id}",
                now=now,
            )
        return terminal

    def run_to_terminal(self, operation_id: str) -> None:
        while not self.run_once(operation_id):
            watch = self.store.read(operation_id)
            if watch is None:
                return
            self.sleeper.sleep(float(watch.next_delay_seconds))
