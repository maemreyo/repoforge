"""Path-safe, bounded Forge v2 workspace lifecycle facades."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ...domain.execution_environment import ExecutionEvidence
from ..context import ApplicationContext
from ..fingerprint_cache import read_fingerprint
from ..retrieval import paginate
from .base_status import collect_workspace_base_status
from .create import WorkspaceCreateCommand, WorkspaceCreator
from .format_changed import WorkspaceChangedFormatter, WorkspaceFormatChangedCommand
from .hygiene_status import WorkspaceHygieneStatusCommand, WorkspaceHygieneStatusReader
from .remove import WorkspaceRemoveCommand, WorkspaceRemover
from .status import WorkspaceStatusCommand, WorkspaceStatusReader


@dataclass(frozen=True, slots=True)
class WorkspaceCreateV2Command:
    repo_id: str
    task_slug: str
    base: str | None = None
    idempotency_key: str | None = None
    issue_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceCreateV2Result:
    status: str
    summary: str
    error: None
    workspace_id: str
    repo_id: str
    branch: str
    base: str
    head_sha: str
    workspace_fingerprint: str
    issue_ids: tuple[str, ...]


class WorkspaceCreatorV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._creator = WorkspaceCreator(ctx)
        self._status = WorkspaceStatusReader(ctx)

    def execute(self, command: WorkspaceCreateV2Command) -> WorkspaceCreateV2Result:
        created = self._creator.execute(
            WorkspaceCreateCommand(
                command.repo_id,
                command.task_slug,
                command.base,
                command.idempotency_key,
                command.issue_ids,
            )
        )
        status = self._status.compute(WorkspaceStatusCommand(created.workspace_id))
        return WorkspaceCreateV2Result(
            "ok",
            f"Created isolated workspace {created.workspace_id}",
            None,
            created.workspace_id,
            created.repo_id,
            created.branch,
            created.base,
            created.head_sha,
            status.workspace_fingerprint,
            created.issue_ids,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceRemoveV2Command:
    workspace_id: str
    delete_local_branch: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceRemoveV2Result:
    status: str
    summary: str
    error: None
    workspace_id: str
    removed: bool
    local_branch_deleted: bool
    remote_untouched: bool
    tombstone: str


class WorkspaceRemoverV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self._remover = WorkspaceRemover(ctx)

    def execute(self, command: WorkspaceRemoveV2Command) -> WorkspaceRemoveV2Result:
        removed = self._remover.execute(
            WorkspaceRemoveCommand(command.workspace_id, command.delete_local_branch)
        )
        tombstone = (
            "The local worktree and registry record were removed. Remote branches and pull "
            "requests were not changed."
        )
        return WorkspaceRemoveV2Result(
            "ok",
            f"Removed workspace {command.workspace_id}",
            None,
            command.workspace_id,
            removed.removed,
            removed.local_branch_deleted,
            removed.remote_branch_untouched,
            tombstone,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceListV2Command:
    exists: bool | None = True
    lifecycle: str | None = None
    repo_id: str | None = None
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSummaryV2:
    workspace_id: str
    repo_id: str
    branch: str
    base: str
    exists: bool
    dirty: bool | None
    lifecycle: str
    issue_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceListV2Result:
    status: str
    summary: str
    error: None
    workspaces: tuple[WorkspaceSummaryV2, ...]
    cleanup_guidance: tuple[str, ...]
    truncated: bool
    next_cursor: str | None


class WorkspaceListerV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx

    def execute(self, command: WorkspaceListV2Command) -> WorkspaceListV2Result:
        if not 1 <= command.limit <= 100:
            raise ValueError("workspace_list limit must be between 1 and 100")
        if command.lifecycle is not None and command.lifecycle not in {
            "active",
            "orphaned_read_only",
        }:
            raise ValueError("workspace_list lifecycle filter is invalid")
        details: dict[str, object] = {
            "exists": command.exists,
            "lifecycle": command.lifecycle,
            "repo_id": command.repo_id,
            "limit": command.limit,
        }

        def operation() -> WorkspaceListV2Result:
            values: list[WorkspaceSummaryV2] = []
            guidance: list[str] = []
            for record in self.ctx.store.list():
                workspace = Path(record.path)
                exists = workspace.is_dir()
                lifecycle = (
                    "active"
                    if record.repo_id in self.ctx.config.repositories
                    else "orphaned_read_only"
                )
                if command.exists is not None and exists is not command.exists:
                    continue
                if command.lifecycle is not None and lifecycle != command.lifecycle:
                    continue
                if command.repo_id is not None and record.repo_id != command.repo_id:
                    continue
                dirty: bool | None = None
                if exists:
                    try:
                        dirty = bool(self.ctx.git.status_porcelain(workspace).strip())
                    except Exception:
                        dirty = None
                values.append(
                    WorkspaceSummaryV2(
                        record.workspace_id,
                        record.repo_id,
                        record.branch,
                        record.base,
                        exists,
                        dirty,
                        lifecycle,
                        tuple(record.metadata.get("issue_ids", ())),
                    )
                )
                if not exists:
                    guidance.append(
                        f"{record.workspace_id}: worktree is missing; review recovery needs, then "
                        "remove the stale registry record explicitly."
                    )
                elif lifecycle == "orphaned_read_only":
                    guidance.append(
                        f"{record.workspace_id}: repository is no longer enrolled; only bounded "
                        "reads and explicit cleanup remain available."
                    )
            values.sort(key=lambda item: (item.repo_id, item.workspace_id))
            page = paginate(
                values,
                kind="workspace_list_v2",
                scope="workspace-registry",
                request={
                    "exists": command.exists,
                    "lifecycle": command.lifecycle,
                    "repo_id": command.repo_id,
                },
                max_items=command.limit,
                byte_budget=120_000,
                cursor=command.cursor,
            )
            selected = tuple(item for item in page.items if isinstance(item, WorkspaceSummaryV2))
            details["workspace_count"] = len(selected)
            return WorkspaceListV2Result(
                "ok",
                f"Listed {len(selected)} managed workspace(s)",
                None,
                selected,
                tuple(sorted(set(guidance)))[:100],
                page.truncated,
                page.next_cursor,
            )

        return self.ctx.audited("workspace_list", details, operation, mutating=False)


@dataclass(frozen=True, slots=True)
class WorkspaceStatusV2Command:
    workspace_id: str
    sections: tuple[str, ...] = ("local",)
    byte_budget: int = 60_000


@dataclass(frozen=True, slots=True)
class StatusFact:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class StatusSectionV2:
    section: str
    freshness: str
    facts: tuple[StatusFact, ...]
    violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceStatusV2Result:
    status: str
    summary: str
    error: None
    workspace_id: str
    repo_id: str
    head_sha: str
    workspace_fingerprint: str
    clean: bool
    sections: tuple[StatusSectionV2, ...]
    fingerprint_source: str
    truncated: bool


class WorkspaceStatusV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._hygiene = WorkspaceHygieneStatusReader(ctx)

    def execute(self, command: WorkspaceStatusV2Command) -> WorkspaceStatusV2Result:
        requested = tuple(dict.fromkeys(command.sections))
        if (
            not requested
            or len(requested) > 3
            or any(section not in {"local", "base", "hygiene"} for section in requested)
        ):
            raise ValueError("workspace_status sections must select local, base, and/or hygiene")
        if not 1 <= command.byte_budget <= 120_000:
            raise ValueError("workspace_status byte_budget must be between 1 and 120000")
        record, repo, workspace = self.ctx.workspace(command.workspace_id)
        details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "sections": list(requested),
        }

        def operation() -> WorkspaceStatusV2Result:
            with self.ctx.locks.lock(command.workspace_id):
                fresh_record = self.ctx.store.load(command.workspace_id)
                lookup = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    workspace,
                )
                head = self.ctx.git.head_sha(workspace)
                clean = not bool(self.ctx.git.status_porcelain(workspace).strip())
                sections: list[StatusSectionV2] = []
                if "local" in requested:
                    changed = self.ctx.git.changed_paths(workspace, repo)
                    metrics = self.ctx.git.change_metrics(workspace, repo)
                    sections.append(
                        StatusSectionV2(
                            "local",
                            "local",
                            (
                                StatusFact("branch", fresh_record.branch),
                                StatusFact("base", fresh_record.base),
                                StatusFact(
                                    "ahead_of_base",
                                    str(
                                        self.ctx.git.ahead_of_base(
                                            workspace,
                                            fresh_record.remote,
                                            fresh_record.base,
                                        )
                                    ),
                                ),
                                StatusFact("changed_paths", json.dumps(changed)),
                                StatusFact("change_metrics", json.dumps(metrics, sort_keys=True)),
                                StatusFact(
                                    "verification_current",
                                    str(
                                        fresh_record.last_verification is not None
                                        and fresh_record.last_verification.fingerprint
                                        == lookup.fingerprint
                                    ).lower(),
                                ),
                            ),
                        )
                    )
                if "base" in requested:
                    base = collect_workspace_base_status(
                        self.ctx,
                        fresh_record,
                        repo,
                        workspace,
                        fetch_remote=True,
                    )
                    base_violations = (
                        (f"remote_error:{base.remote_error_code}",)
                        if not base.remote_available and base.remote_error_code
                        else ()
                    )
                    sections.append(
                        StatusSectionV2(
                            "base",
                            "live" if base.remote_available else "unavailable",
                            (
                                StatusFact("configured_base", base.configured_base),
                                StatusFact("workspace_base_sha", base.workspace_base_sha),
                                StatusFact("latest_base_sha", base.latest_base_sha),
                                StatusFact("ahead", str(base.ahead_base)),
                                StatusFact("behind", str(base.behind_base)),
                                StatusFact("staleness", base.staleness),
                                StatusFact("published_state", base.published_state),
                                StatusFact(
                                    "upstream_changed_paths",
                                    json.dumps(base.upstream_changed_paths),
                                ),
                                StatusFact("overlap_paths", json.dumps(base.overlap_paths)),
                                StatusFact(
                                    "generated_overlap_paths",
                                    json.dumps(base.generated_overlap_paths),
                                ),
                                StatusFact(
                                    "expected_evidence_invalidation",
                                    json.dumps(base.expected_evidence_invalidation),
                                ),
                                StatusFact("verify_selector", json.dumps(base.verify_selector)),
                                StatusFact("recommended_action", base.recommended_action),
                                StatusFact(
                                    "recreate_eligible",
                                    str(base.recreate_eligible).lower(),
                                ),
                                StatusFact(
                                    "recreate_blockers",
                                    json.dumps(base.recreate_blockers),
                                ),
                            ),
                            base_violations,
                        )
                    )
                if "hygiene" in requested:
                    hygiene = self._hygiene.compute(
                        WorkspaceHygieneStatusCommand(command.workspace_id)
                    )
                    hygiene_violations = tuple(
                        json.dumps(item, sort_keys=True, ensure_ascii=False)[:1000]
                        for item in (*hygiene.introduced, *hygiene.changed_path_findings)
                    )[:200]
                    sections.append(
                        StatusSectionV2(
                            "hygiene",
                            "local" if hygiene.status == "available" else "unavailable",
                            (
                                StatusFact("status", hygiene.status),
                                StatusFact("formatter_id", hygiene.formatter_id or ""),
                                StatusFact(
                                    "available_formatters",
                                    json.dumps(hygiene.available_formatters),
                                ),
                                StatusFact("base_cache_hit", str(hygiene.base_cache_hit).lower()),
                                StatusFact(
                                    "preexisting_count",
                                    str(len(hygiene.preexisting)),
                                ),
                                StatusFact("introduced_count", str(len(hygiene.introduced))),
                                StatusFact("resolved_count", str(len(hygiene.resolved))),
                            ),
                            hygiene_violations,
                        )
                    )
                bounded, truncated = self._bound_sections(tuple(sections), command.byte_budget)
                details.update(
                    {
                        "fingerprint_source": lookup.source,
                        "truncated": truncated,
                    }
                )
                return WorkspaceStatusV2Result(
                    "ok",
                    f"Read {len(bounded)} workspace status section(s)",
                    None,
                    command.workspace_id,
                    record.repo_id,
                    head,
                    lookup.fingerprint,
                    clean,
                    bounded,
                    "cache" if lookup.source == "cache_hit" else "scan",
                    truncated,
                )

        return self.ctx.audited("workspace_status", details, operation, mutating=False)

    @staticmethod
    def _bound_sections(
        sections: tuple[StatusSectionV2, ...], byte_budget: int
    ) -> tuple[tuple[StatusSectionV2, ...], bool]:
        mutable = [replace(section) for section in sections]

        def size() -> int:
            return len(
                json.dumps(
                    [asdict(section) for section in mutable],
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            )

        truncated = False
        while mutable and size() > byte_budget:
            truncated = True
            last = mutable[-1]
            if last.violations:
                mutable[-1] = replace(last, violations=last.violations[:-1])
            elif last.facts:
                mutable[-1] = replace(last, facts=last.facts[:-1])
            elif len(mutable) > 1:
                mutable.pop()
            else:
                break
        return tuple(mutable), truncated


@dataclass(frozen=True, slots=True)
class WorkspaceFormatChangedV2Command:
    workspace_id: str
    expected_fingerprint: str
    formatter_id: str | None = None


@dataclass(frozen=True, slots=True)
class FormatterEvidenceV2:
    formatter_id: str
    selected_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    outcome: str


@dataclass(frozen=True, slots=True)
class WorkspaceFormatChangedV2Result:
    status: str
    summary: str
    error: None
    workspace_id: str
    formatters: tuple[FormatterEvidenceV2, ...]
    changed: bool
    head_sha: str
    workspace_fingerprint: str
    execution_evidence: ExecutionEvidence | None


class WorkspaceChangedFormatterV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self._formatter = WorkspaceChangedFormatter(ctx)

    def execute(self, command: WorkspaceFormatChangedV2Command) -> WorkspaceFormatChangedV2Result:
        result = self._formatter.execute(
            WorkspaceFormatChangedCommand(
                command.workspace_id,
                command.expected_fingerprint,
                command.formatter_id,
            )
        )
        changed = result.fingerprint_changed
        evidence = FormatterEvidenceV2(
            result.formatter_id,
            tuple(result.selected_paths),
            tuple(result.modified_paths),
            "changed" if changed else "no_op",
        )
        return WorkspaceFormatChangedV2Result(
            "ok",
            (
                f"Formatter {result.formatter_id} changed {len(result.modified_paths)} path(s)"
                if changed
                else f"Formatter {result.formatter_id} was a no-op"
            ),
            None,
            command.workspace_id,
            (evidence,),
            changed,
            result.head_sha,
            result.fingerprint_after,
            result.execution_evidence,
        )
