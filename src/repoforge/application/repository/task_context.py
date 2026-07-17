"""Bounded warm-start bundle assembled from existing, independently bounded read use cases.

`RepoTaskContextReader` answers "what do I need to resume or start this task" in one call by
invoking the *pure* application logic of `RepositoryContextReader`, `RepositoryIssueSpecReader`,
`WorkspaceStatusReader`, and `RecentCommitsReader` directly (their `compute()` methods, which
never call `ApplicationContext.audited`) and wrapping the whole assembly in exactly one audit
event. No section reimplements the logic of the use case it reuses.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ...domain.errors import WorkspaceError
from ..context import ApplicationContext
from ..dto import to_data
from ..workspace.status import WorkspaceStatusCommand, WorkspaceStatusReader
from .context import RepositoryContextCommand, RepositoryContextReader
from .issue_spec import RepositoryIssueSpecCommand, RepositoryIssueSpecReader
from .recent_commits import RecentCommitsCommand, RecentCommitsReader

T = TypeVar("T")

# The ticket section's own bound, applied before the overall bundle hard cap.
_TICKET_SECTION_MAX_BYTES = 16 * 1024
# The serialized bundle never exceeds this size; sections are truncated to fit it.
_BUNDLE_HARD_CAP_BYTES = 96 * 1024
# `recent_commits` is bounded to its last five entries regardless of the caller's needs.
_RECENT_COMMITS_LIMIT = 5

# Overflow truncation order: least protected first, most protected (repository) last.
_TRUNCATION_ORDER: tuple[str, ...] = ("recent_commits", "ticket", "workspace", "repository")


@dataclass(frozen=True, slots=True)
class RepoTaskContextCommand:
    repo_id: str
    issue_number: int | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class RepoTaskContextResult:
    repo_id: str
    issue_number: int | None
    workspace_id: str | None
    repository: dict[str, Any]
    ticket: dict[str, Any] | None
    workspace: dict[str, Any] | None
    recent_commits: dict[str, Any]
    truncated: bool


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    )


def _shrink_ticket_section(payload: dict[str, Any]) -> None:
    payload["comments"] = []
    live = payload.get("live")
    if isinstance(live, dict):
        payload["live"] = {k: live.get(k) for k in ("number", "title", "state", "url") if k in live}
    payload["drift"] = []
    payload["node"] = None


def _bound_ticket_section(payload: dict[str, Any]) -> bool:
    """Mutate ``payload`` so it fits the 16 KB ticket bound; return whether it was truncated."""
    if _encoded_size(payload) <= _TICKET_SECTION_MAX_BYTES:
        return False
    _shrink_ticket_section(payload)
    return True


def _minimal_repository_stub(payload: dict[str, Any]) -> None:
    payload["root_files"] = []
    payload["engines"] = {}
    payload["scripts"] = {}
    payload["instruction_files"] = []
    payload["diagnostic_pack_suggestions"] = []
    payload["profile_drift"] = {
        "detected_unenrolled_profiles": [],
        "stale": True,
        "truncated": True,
    }


def _minimal_ticket_stub(payload: dict[str, Any]) -> None:
    _shrink_ticket_section(payload)


def _minimal_workspace_stub(payload: dict[str, Any]) -> None:
    payload["changed_paths"] = []
    payload["change_metrics"] = {}
    payload["issue_ids"] = []
    payload["last_verification"] = None


def _minimal_recent_commits_stub(payload: dict[str, Any]) -> None:
    payload["commits"] = []


_MINIMAL_STUBS: dict[str, Callable[[dict[str, Any]], None]] = {
    "recent_commits": _minimal_recent_commits_stub,
    "ticket": _minimal_ticket_stub,
    "workspace": _minimal_workspace_stub,
    "repository": _minimal_repository_stub,
}


def _enforce_hard_cap(bundle: dict[str, Any]) -> bool:
    """Truncate sections in `_TRUNCATION_ORDER` until the bundle fits `_BUNDLE_HARD_CAP_BYTES`.

    Returns whether any section was truncated by this pass (independent of each section's own
    internal bound, e.g. the ticket section's 16 KB limit).
    """
    if _encoded_size(bundle) <= _BUNDLE_HARD_CAP_BYTES:
        return False
    truncated_any = False
    for name in _TRUNCATION_ORDER:
        section = bundle.get(name)
        if not isinstance(section, dict):
            continue
        _MINIMAL_STUBS[name](section)
        section["truncated"] = True
        truncated_any = True
        if _encoded_size(bundle) <= _BUNDLE_HARD_CAP_BYTES:
            break
    return truncated_any


class RepoTaskContextReader:
    """Assemble one bounded task-context bundle for resuming or starting a task.

    Every section reuses the reused use case's `compute()` method — its pure application logic,
    with no audit call inside — so the whole bundle produces exactly one `repo_task_context`
    audit event carrying each present section's `duration_ms`, instead of one event per section.
    """

    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._repository_reader = RepositoryContextReader(ctx)
        self._ticket_reader = RepositoryIssueSpecReader(ctx)
        self._workspace_reader = WorkspaceStatusReader(ctx)
        self._commits_reader = RecentCommitsReader(ctx)

    def execute(self, c: RepoTaskContextCommand) -> RepoTaskContextResult:
        details: dict[str, object] = {
            "repo_id": c.repo_id,
            "issue_number": c.issue_number,
            "workspace_id": c.workspace_id,
        }
        return self.ctx.audited("repo_task_context", details, lambda: self._compute(c, details))

    def _timed(self, details: dict[str, object], name: str, op: Callable[[], T]) -> T:
        started = time.monotonic()
        result = op()
        details[f"{name}_duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        return result

    def _compute(
        self, c: RepoTaskContextCommand, details: dict[str, object]
    ) -> RepoTaskContextResult:
        # Fail closed for an unknown repository before doing any section work.
        self.ctx.repo(c.repo_id)

        # Fail closed: a supplied workspace must actually belong to the requested repository.
        workspace_path: Path | None = None
        if c.workspace_id is not None:
            record, _repo, workspace_path = self.ctx.workspace(c.workspace_id)
            if record.repo_id != c.repo_id:
                raise WorkspaceError(
                    f"Workspace {c.workspace_id!r} belongs to repository {record.repo_id!r}, "
                    f"not {c.repo_id!r}"
                )

        repository_payload = to_data(
            self._timed(
                details,
                "repository",
                lambda: self._repository_reader.compute(RepositoryContextCommand(c.repo_id)),
            )
        )
        repository_payload["truncated"] = False

        ticket_payload: dict[str, Any] | None = None
        issue_number = c.issue_number
        if issue_number is not None:
            ticket_payload = to_data(
                self._timed(
                    details,
                    "ticket",
                    lambda: self._ticket_reader.compute(
                        RepositoryIssueSpecCommand(c.repo_id, issue_number)
                    ),
                )
            )
            ticket_payload["truncated"] = _bound_ticket_section(ticket_payload)

        workspace_payload: dict[str, Any] | None = None
        workspace_id = c.workspace_id
        if workspace_id is not None:
            workspace_payload = to_data(
                self._timed(
                    details,
                    "workspace",
                    lambda: self._workspace_reader.compute(WorkspaceStatusCommand(workspace_id)),
                )
            )
            workspace_payload["truncated"] = False

        recent_commits_payload = to_data(
            self._timed(
                details,
                "recent_commits",
                lambda: (
                    self._commits_reader.compute_from_path(
                        RecentCommitsCommand(c.repo_id, limit=_RECENT_COMMITS_LIMIT),
                        workspace_path,
                    )
                    if workspace_path is not None
                    else self._commits_reader.compute(
                        RecentCommitsCommand(c.repo_id, limit=_RECENT_COMMITS_LIMIT)
                    )
                ),
            )
        )
        recent_commits_payload["truncated"] = False

        bundle: dict[str, Any] = {
            "repo_id": c.repo_id,
            "issue_number": c.issue_number,
            "workspace_id": c.workspace_id,
            "repository": repository_payload,
            "ticket": ticket_payload,
            "workspace": workspace_payload,
            "recent_commits": recent_commits_payload,
        }
        overflow_truncated = _enforce_hard_cap(bundle)
        truncated = overflow_truncated or bool(ticket_payload and ticket_payload["truncated"])
        details["truncated"] = truncated

        return RepoTaskContextResult(
            c.repo_id,
            c.issue_number,
            c.workspace_id,
            bundle["repository"],
            bundle["ticket"],
            bundle["workspace"],
            bundle["recent_commits"],
            truncated,
        )
