"""Consolidated, path-safe Forge v2 repository read families."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...domain.errors import ConfigError
from ...domain.tickets import TicketNode
from ..context import ApplicationContext
from ..retrieval import paginate
from ..workspace.status import WorkspaceStatusCommand, WorkspaceStatusReader
from .commit_read import RepositoryCommitReadCommand, RepositoryCommitReader
from .compare import RepositoryCompareCommand, RepositoryComparer
from .issue_graph import (
    RepositoryIssueGraphCommand,
    RepositoryIssueGraphReader,
    node_payload,
)
from .issue_next import RepositoryIssueNextCommand, RepositoryIssueNextReader
from .issue_spec import RepositoryIssueSpecCommand, RepositoryIssueSpecReader
from .pr_read import PullRequestReadCommand, PullRequestReader
from .recent_commits import RecentCommitsCommand, RecentCommitsReader
from .status import RepositoryStatusCommand, RepositoryStatusReader


@dataclass(frozen=True, slots=True)
class CompactCommit:
    sha: str
    subject: str
    author: str
    committed_at: str


@dataclass(frozen=True, slots=True)
class CompactFileChange:
    path: str
    status: str
    additions: int
    deletions: int


@dataclass(frozen=True, slots=True)
class CompactComparison:
    base_sha: str
    head_sha: str
    merge_base_sha: str
    ahead: int
    behind: int
    files: tuple[CompactFileChange, ...]


@dataclass(frozen=True, slots=True)
class RepositoryHistoryV2Command:
    repo_id: str
    mode: str
    ref: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    path_glob: str | None = None
    limit: int = 20
    include_patch: bool = False
    byte_budget: int = 60_000
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryHistoryV2Result:
    status: str
    summary: str
    error: None
    repo_id: str
    mode: str
    commit: CompactCommit | None
    commits: tuple[CompactCommit, ...]
    comparison: CompactComparison | None
    truncated: bool
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CompactRepository:
    repo_id: str
    capabilities: tuple[str, ...]
    default_ref: str


@dataclass(frozen=True, slots=True)
class RepositoryListV2Command:
    detail: bool = False
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class RepositoryListV2Result:
    status: str
    summary: str
    error: None
    repositories: tuple[CompactRepository, ...]
    truncated: bool
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Fact:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class PullRequestEvidenceV2:
    number: int
    title: str
    state: str
    draft: bool
    head_sha: str
    base_ref: str
    review_decision: str | None
    freshness: str


@dataclass(frozen=True, slots=True)
class RepositoryPrReadV2Command:
    repo_id: str
    pr_number: int
    fresh: bool = False
    detail: str = "overview"
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryPrReadV2Result:
    status: str
    summary: str
    error: None
    repo_id: str
    pull_request: PullRequestEvidenceV2
    facts: tuple[Fact, ...]
    truncated: bool
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class IssueEvidenceV2:
    number: int
    title: str
    state: str
    body: str
    labels: tuple[str, ...]
    freshness: str


@dataclass(frozen=True, slots=True)
class IssueGraphNodeV2:
    number: int
    title: str
    status: str
    priority: str | None
    blockers: tuple[int, ...]
    children: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class IssueDriftV2:
    code: str
    message: str
    issue_number: int


@dataclass(frozen=True, slots=True)
class RepositoryIssueV2Command:
    repo_id: str
    mode: str
    issue_number: int | None = None
    root_issue: int | None = None
    status: str | None = None
    priority: str | None = None
    initiative: int | None = None
    limit: int = 10
    fresh: bool = False
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryIssueV2Result:
    status: str
    summary: str
    error: None
    repo_id: str
    mode: str
    graph_status: str
    issue: IssueEvidenceV2 | None
    nodes: tuple[IssueGraphNodeV2, ...]
    selected: tuple[IssueGraphNodeV2, ...]
    drift: tuple[IssueDriftV2, ...]
    next_action: str | None
    truncated: bool
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ContextSectionV2:
    name: str
    freshness: str
    complete: bool
    truncated: bool
    facts: tuple[Fact, ...]


@dataclass(frozen=True, slots=True)
class RepositoryTaskContextV2Command:
    repo_id: str
    issue_number: int | None = None
    workspace_id: str | None = None
    sections: tuple[str, ...] = (
        "repository",
        "status",
        "ticket",
        "workspace",
        "recent_commits",
    )
    byte_budget: int = 96_000


@dataclass(frozen=True, slots=True)
class RepositoryTaskContextV2Result:
    status: str
    summary: str
    error: None
    repo_id: str
    sections: tuple[ContextSectionV2, ...]
    truncated: bool
    next_cursor: None


def _json_value(value: object, *, max_chars: int = 10_000) -> str:
    if isinstance(value, str):
        return value[:max_chars]
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return rendered[:max_chars]


def _text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _compact_recent(raw: dict[str, Any]) -> CompactCommit:
    return CompactCommit(
        sha=_text(raw, "sha", "commit_sha"),
        subject=_text(raw, "subject", "title", "message")[:500] or "(no subject)",
        author=_text(raw, "author", "author_name")[:300] or "unknown",
        committed_at=_text(raw, "date", "committed_at", "timestamp")[:80] or "unknown",
    )


def _file_status(raw: str) -> str:
    marker = raw.upper()[:1]
    return {
        "A": "added",
        "D": "deleted",
        "R": "renamed",
    }.get(marker, "modified")


def _graph_node(node: TicketNode | dict[str, Any]) -> IssueGraphNodeV2:
    payload = node_payload(node) if isinstance(node, TicketNode) else node
    priority = payload.get("priority")
    return IssueGraphNodeV2(
        number=int(payload["number"]),
        title=str(payload["title"])[:1000],
        status=str(payload["status"])[:100],
        priority=str(priority)[:30] if priority is not None else None,
        blockers=tuple(int(item) for item in payload.get("blockers", ())),
        children=tuple(int(item) for item in payload.get("children", ())),
    )


def _issue_labels(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    labels: list[str] = []
    for item in raw[:100]:
        if isinstance(item, str):
            labels.append(item[:200])
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            labels.append(str(item["name"])[:200])
    return tuple(labels)


class RepositoryHistoryV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._commits = RepositoryCommitReader(ctx)
        self._recent = RecentCommitsReader(ctx)
        self._compare = RepositoryComparer(ctx)

    def execute(self, command: RepositoryHistoryV2Command) -> RepositoryHistoryV2Result:
        return self.ctx.audited(
            "repo_history",
            {"repo_id": command.repo_id, "mode": command.mode},
            lambda: self._read(command),
        )

    def _read(self, command: RepositoryHistoryV2Command) -> RepositoryHistoryV2Result:
        if command.mode == "commit":
            if command.ref is None:
                raise ValueError("ref is required for commit history mode")
            commit_raw = self._commits.compute(
                RepositoryCommitReadCommand(
                    command.repo_id,
                    command.ref,
                    command.limit,
                    command.include_patch,
                )
            )
            commit = CompactCommit(
                commit_raw.commit_sha,
                commit_raw.subject[:500] or "(no subject)",
                commit_raw.author.name[:300] or "unknown",
                commit_raw.author.date[:80] or commit_raw.committer.date[:80] or "unknown",
            )
            return RepositoryHistoryV2Result(
                "ok",
                f"Read commit {commit_raw.commit_sha[:12]}",
                None,
                command.repo_id,
                command.mode,
                commit,
                (),
                None,
                commit_raw.files_truncated or commit_raw.patch_truncated,
                None,
            )
        if command.mode == "log":
            log_raw = self._recent.compute(RecentCommitsCommand(command.repo_id, 100))
            commits = tuple(_compact_recent(item) for item in log_raw.commits)
            page = paginate(
                commits,
                kind="repo_history_log",
                scope=command.repo_id,
                request={"limit": command.limit},
                max_items=command.limit,
                byte_budget=command.byte_budget,
                cursor=command.cursor,
            )
            return RepositoryHistoryV2Result(
                "ok",
                f"Read {len(page.items)} commit(s)",
                None,
                command.repo_id,
                command.mode,
                None,
                tuple(page.items),  # type: ignore[arg-type]
                None,
                page.truncated,
                page.next_cursor,
            )
        if command.mode != "compare":
            raise ValueError("mode must be one of: commit, log, compare")
        if command.base_ref is None or command.head_ref is None:
            raise ValueError("base_ref and head_ref are required for compare history mode")
        raw_compare = self._compare.compute(
            RepositoryCompareCommand(
                command.repo_id,
                command.base_ref,
                command.head_ref,
                command.path_glob,
                command.limit,
                command.include_patch,
            )
        )
        changes = tuple(
            CompactFileChange(
                item.path,
                _file_status(item.status),
                item.additions or 0,
                item.deletions or 0,
            )
            for item in raw_compare.files
        )
        page = paginate(
            changes,
            kind="repo_history_compare",
            scope=f"{command.repo_id}:{raw_compare.base_sha}:{raw_compare.head_sha}",
            request={
                "base_ref": command.base_ref,
                "head_ref": command.head_ref,
                "path_glob": command.path_glob,
                "include_patch": command.include_patch,
            },
            max_items=command.limit,
            byte_budget=command.byte_budget,
            cursor=command.cursor,
        )
        comparison = CompactComparison(
            raw_compare.base_sha,
            raw_compare.head_sha,
            raw_compare.merge_base_sha,
            raw_compare.ahead,
            raw_compare.behind,
            tuple(page.items),  # type: ignore[arg-type]
        )
        return RepositoryHistoryV2Result(
            "ok",
            f"Compared {command.base_ref} with {command.head_ref}",
            None,
            command.repo_id,
            command.mode,
            None,
            (),
            comparison,
            raw_compare.files_truncated or raw_compare.patch_truncated or page.truncated,
            page.next_cursor,
        )


class RepositoryListV2:
    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx

    def execute(self, command: RepositoryListV2Command) -> RepositoryListV2Result:
        def operation() -> RepositoryListV2Result:
            if not 1 <= command.limit <= 100:
                raise ValueError("limit must be between 1 and 100")
            repositories: list[CompactRepository] = []
            for repo in sorted(
                self.ctx.config.repositories.values(), key=lambda item: item.repo_id
            ):
                capabilities = ["read"]
                if not repo.read_only:
                    capabilities.append("write")
                if repo.publish_enabled:
                    capabilities.append("publish")
                if any(profile.verification for profile in repo.profiles.values()):
                    capabilities.append("verify")
                repositories.append(
                    CompactRepository(repo.repo_id, tuple(capabilities), repo.default_base)
                )
            page = paginate(
                repositories,
                kind="repo_list_v2",
                scope="configured-repositories",
                request={"detail": command.detail, "limit": command.limit},
                max_items=command.limit,
                byte_budget=60_000,
                cursor=command.cursor,
            )
            return RepositoryListV2Result(
                "ok",
                f"Listed {len(page.items)} repository/repositories",
                None,
                tuple(page.items),  # type: ignore[arg-type]
                page.truncated,
                page.next_cursor,
            )

        return self.ctx.audited("repo_list", {"detail": command.detail}, operation)


class RepositoryPrReadV2:
    _DETAILS = frozenset({"overview", "files", "checks", "reviews"})

    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._reader = PullRequestReader(ctx)

    def execute(self, command: RepositoryPrReadV2Command) -> RepositoryPrReadV2Result:
        return self.ctx.audited(
            "repo_pr_read",
            {
                "repo_id": command.repo_id,
                "pr_number": command.pr_number,
                "detail": command.detail,
            },
            lambda: self._read(command),
        )

    def _read(self, command: RepositoryPrReadV2Command) -> RepositoryPrReadV2Result:
        if command.detail not in self._DETAILS:
            raise ValueError("detail must be one of: overview, files, checks, reviews")
        raw = self._reader.compute(
            PullRequestReadCommand(command.repo_id, command.pr_number, command.fresh)
        ).payload
        repo = self.ctx.repo(command.repo_id)
        head_sha = _text(raw, "headRefOid", "head_sha") or self.ctx.git.head_sha(repo.path)
        evidence = PullRequestEvidenceV2(
            number=int(raw.get("number") or command.pr_number),
            title=_text(raw, "title")[:1000] or f"Pull request #{command.pr_number}",
            state=_text(raw, "state")[:80] or "UNKNOWN",
            draft=bool(raw.get("isDraft", raw.get("draft", False))),
            head_sha=head_sha,
            base_ref=_text(raw, "baseRefName", "base_ref")[:512] or repo.default_base,
            review_decision=(_text(raw, "reviewDecision", "review_decision")[:80] or None),
            freshness="cache" if raw.get("cache_hit") else "live",
        )
        detail_value: object
        if command.detail == "files":
            detail_value = raw.get("files", [])
        elif command.detail == "checks":
            detail_value = raw.get("statusCheckRollup", raw.get("checks", []))
        elif command.detail == "reviews":
            detail_value = raw.get("reviews", [])
        else:
            detail_value = {
                "head_ref": raw.get("headRefName"),
                "author": raw.get("author"),
                "url": raw.get("url"),
            }
        facts = (Fact(command.detail, _json_value(detail_value)),)
        return RepositoryPrReadV2Result(
            "ok",
            f"Read pull request #{evidence.number}",
            None,
            command.repo_id,
            evidence,
            facts,
            False,
            None,
        )


class RepositoryIssueV2:
    _MODES = frozenset({"read", "spec", "graph", "next"})

    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._spec = RepositoryIssueSpecReader(ctx)
        self._graph = RepositoryIssueGraphReader(ctx)
        self._next = RepositoryIssueNextReader(ctx)

    def execute(self, command: RepositoryIssueV2Command) -> RepositoryIssueV2Result:
        if command.mode not in self._MODES:
            raise ValueError("mode must be one of: read, spec, graph, next")
        return self.ctx.audited(
            "repo_issue",
            {"repo_id": command.repo_id, "mode": command.mode},
            lambda: self._read(command),
        )

    def _read(self, command: RepositoryIssueV2Command) -> RepositoryIssueV2Result:
        if command.mode in {"read", "spec"}:
            if command.issue_number is None:
                raise ValueError("issue_number is required for read and spec modes")
            raw = self._spec.compute(
                RepositoryIssueSpecCommand(
                    command.repo_id,
                    command.issue_number,
                    command.fresh,
                )
            )
            live = raw.live
            state = _text(live, "state").lower()
            if state not in {"open", "closed"}:
                state = "open"
            issue = IssueEvidenceV2(
                int(live.get("number") or command.issue_number),
                _text(live, "title")[:1000] or f"Issue #{command.issue_number}",
                state,
                _text(live, "body")[:60_000],
                _issue_labels(live.get("labels")),
                "cache" if raw.cache_hit else "live",
            )
            drift = tuple(
                IssueDriftV2(
                    str(item.get("code") or "LIVE_DRIFT")[:120],
                    str(item.get("message") or "Live issue metadata drifted")[:1000],
                    command.issue_number,
                )
                for item in raw.drift[:100]
            )
            return RepositoryIssueV2Result(
                "ok",
                f"Read issue #{command.issue_number}",
                None,
                command.repo_id,
                command.mode,
                "not_requested",
                issue,
                (),
                (),
                drift,
                None,
                False,
                None,
            )
        if command.mode == "graph":
            raw_graph = self._graph.compute(
                RepositoryIssueGraphCommand(
                    command.repo_id,
                    command.root_issue,
                    command.status,
                    command.priority,
                    command.initiative,
                    command.fresh,
                )
            )
            if not raw_graph.valid and any(
                item.get("code") == "GRAPH_NOT_CONFIGURED" for item in raw_graph.diagnostics
            ):
                return RepositoryIssueV2Result(
                    "ok",
                    "Ticket graph is unavailable",
                    None,
                    command.repo_id,
                    command.mode,
                    "graph_unavailable",
                    None,
                    (),
                    (),
                    (),
                    (
                        "Configure the GitHub-native ticket graph root, then retry. "
                        + raw_graph.safe_next_action
                        if raw_graph.safe_next_action
                        else "Configure the GitHub-native ticket graph root, then retry."
                    ),
                    False,
                    None,
                )
            nodes = tuple(_graph_node(item) for item in raw_graph.nodes)
            page = paginate(
                nodes,
                kind="repo_issue_graph_v2",
                scope=f"{command.repo_id}:{raw_graph.program_issue}",
                request={
                    "root_issue": command.root_issue,
                    "status": command.status,
                    "priority": command.priority,
                    "initiative": command.initiative,
                    "fresh": command.fresh,
                },
                max_items=command.limit,
                byte_budget=60_000,
                cursor=command.cursor,
            )
            drift = tuple(
                IssueDriftV2(
                    str(item.get("code") or "GRAPH_DRIFT")[:120],
                    str(item.get("message") or "Ticket graph evidence drifted")[:1000],
                    max(1, int(item.get("issue_number") or raw_graph.program_issue or 1)),
                )
                for item in raw_graph.diagnostics[:100]
            )
            return RepositoryIssueV2Result(
                "ok",
                f"Read {len(page.items)} ticket graph node(s)",
                None,
                command.repo_id,
                command.mode,
                "available" if raw_graph.valid else "graph_unavailable",
                None,
                tuple(page.items),  # type: ignore[arg-type]
                (),
                drift,
                raw_graph.safe_next_action,
                raw_graph.truncated or page.truncated,
                page.next_cursor,
            )
        raw_next = self._next.compute(
            RepositoryIssueNextCommand(
                command.repo_id,
                command.root_issue,
                command.limit,
                fresh=command.fresh,
            )
        )
        if any(item.get("code") == "GRAPH_NOT_CONFIGURED" for item in raw_next.diagnostics):
            return RepositoryIssueV2Result(
                "ok",
                "Ticket graph is unavailable",
                None,
                command.repo_id,
                command.mode,
                "graph_unavailable",
                None,
                (),
                (),
                (),
                "Configure the GitHub-native ticket graph root, then retry.",
                False,
                None,
            )
        selected = tuple(_graph_node(item) for item in raw_next.tickets)
        drift = tuple(
            IssueDriftV2(
                str(item.get("code") or "READINESS_DRIFT")[:120],
                str(item.get("message") or "Ticket readiness evidence drifted")[:1000],
                max(1, int(item.get("issue_number") or command.root_issue or 1)),
            )
            for item in raw_next.diagnostics[:100]
        )
        return RepositoryIssueV2Result(
            "ok",
            f"Selected {len(selected)} next ticket(s)",
            None,
            command.repo_id,
            command.mode,
            "available" if raw_next.valid else "graph_unavailable",
            None,
            (),
            selected,
            drift,
            None if raw_next.valid else "Refresh the GitHub graph and resolve reported drift.",
            False,
            None,
        )


class RepositoryTaskContextV2:
    _SECTIONS = frozenset({"repository", "status", "ticket", "workspace", "recent_commits"})

    def __init__(self, ctx: ApplicationContext) -> None:
        self.ctx = ctx
        self._status = RepositoryStatusReader(ctx)
        self._spec = RepositoryIssueSpecReader(ctx)
        self._workspace = WorkspaceStatusReader(ctx)
        self._recent = RecentCommitsReader(ctx)

    def execute(
        self,
        command: RepositoryTaskContextV2Command,
    ) -> RepositoryTaskContextV2Result:
        return self.ctx.audited(
            "repo_task_context",
            {
                "repo_id": command.repo_id,
                "issue_number": command.issue_number,
                "workspace_id": command.workspace_id,
                "sections": list(command.sections),
            },
            lambda: self._read(command),
        )

    def _read(self, command: RepositoryTaskContextV2Command) -> RepositoryTaskContextV2Result:
        if not 1 <= command.byte_budget <= 120_000:
            raise ValueError("byte_budget must be between 1 and 120000")
        if not command.sections or len(command.sections) > 5:
            raise ValueError("sections must contain between 1 and 5 entries")
        if len(set(command.sections)) != len(command.sections):
            raise ValueError("sections must be unique")
        unknown = set(command.sections) - self._SECTIONS
        if unknown:
            raise ValueError(f"Unknown task-context section: {sorted(unknown)[0]}")
        repo = self.ctx.repo(command.repo_id)
        sections: list[ContextSectionV2] = []
        for name in command.sections:
            if name == "repository":
                capabilities = ["read"]
                if not repo.read_only:
                    capabilities.append("write")
                if repo.publish_enabled:
                    capabilities.append("publish")
                if any(profile.verification for profile in repo.profiles.values()):
                    capabilities.append("verify")
                sections.append(
                    ContextSectionV2(
                        name,
                        "local",
                        True,
                        False,
                        (
                            Fact("display_name", repo.display_name or repo.repo_id),
                            Fact("default_ref", repo.default_base),
                            Fact("capabilities", _json_value(capabilities)),
                        ),
                    )
                )
            elif name == "status":
                status = self._status.compute(RepositoryStatusCommand(command.repo_id))
                sections.append(
                    ContextSectionV2(
                        name,
                        "live",
                        True,
                        False,
                        (
                            Fact("git_status", status.git_status[:10_000]),
                            Fact("gh_authenticated", _json_value(status.gh_authenticated)),
                            Fact("default_ref", repo.default_base),
                        ),
                    )
                )
            elif name == "ticket":
                if command.issue_number is None:
                    sections.append(ContextSectionV2(name, "unavailable", False, False, ()))
                    continue
                spec = self._spec.compute(
                    RepositoryIssueSpecCommand(command.repo_id, command.issue_number)
                )
                sections.append(
                    ContextSectionV2(
                        name,
                        "cache" if spec.cache_hit else "live",
                        True,
                        False,
                        (
                            Fact("number", str(command.issue_number)),
                            Fact("title", _text(spec.live, "title")[:1000]),
                            Fact("state", _text(spec.live, "state")[:80]),
                            Fact("graph_member", _json_value(spec.graph_member)),
                            Fact(
                                "drift_codes",
                                _json_value([item.get("code") for item in spec.drift]),
                            ),
                        ),
                    )
                )
            elif name == "workspace":
                if command.workspace_id is None:
                    sections.append(ContextSectionV2(name, "unavailable", False, False, ()))
                    continue
                workspace = self._workspace.compute(WorkspaceStatusCommand(command.workspace_id))
                if workspace.repo_id != command.repo_id:
                    raise ConfigError(
                        f"Workspace {command.workspace_id!r} belongs to {workspace.repo_id!r}, "
                        f"not {command.repo_id!r}"
                    )
                sections.append(
                    ContextSectionV2(
                        name,
                        "local",
                        True,
                        False,
                        (
                            Fact("branch", workspace.branch),
                            Fact("base", workspace.base),
                            Fact("head_sha", workspace.head_sha),
                            Fact("workspace_fingerprint", workspace.workspace_fingerprint),
                            Fact("clean", _json_value(workspace.clean)),
                            Fact("changed_paths", _json_value(workspace.changed_paths)),
                        ),
                    )
                )
            else:
                recent = self._recent.compute(RecentCommitsCommand(command.repo_id, 5))
                compact = [_compact_recent(item) for item in recent.commits]
                sections.append(
                    ContextSectionV2(
                        name,
                        "local",
                        True,
                        False,
                        tuple(
                            Fact(
                                item.sha,
                                _json_value({"subject": item.subject, "author": item.author}),
                            )
                            for item in compact
                        ),
                    )
                )
        bounded: list[ContextSectionV2] = []
        used = 0
        truncated = False
        for section in sections:
            selected_facts: list[Fact] = []
            for fact in section.facts:
                size = len(
                    json.dumps(
                        {"name": section.name, "key": fact.key, "value": fact.value},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                if selected_facts and used + size > command.byte_budget:
                    truncated = True
                    break
                selected_facts.append(fact)
                used += size
            bounded.append(
                ContextSectionV2(
                    section.name,
                    section.freshness,
                    section.complete,
                    section.truncated or len(selected_facts) != len(section.facts),
                    tuple(selected_facts),
                )
            )
        return RepositoryTaskContextV2Result(
            "ok",
            f"Assembled {len(bounded)} task-context section(s)",
            None,
            command.repo_id,
            tuple(bounded),
            truncated,
            None,
        )
