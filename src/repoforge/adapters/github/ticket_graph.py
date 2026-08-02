"""Bounded read-only GitHub-native ticket graph snapshots.

The graph is traversed through batched GraphQL requests instead of one
``gh api`` subprocess per issue/capability. Aliased root fields give per-node
partial success, so one failing capability (comments, dependencies, ...) does
not erase complete evidence for unrelated capabilities, and provider traffic
is exposed as structured read stats so the batching contract can be verified.

The adapter is split into focused modules: ``graph_query`` (query
construction), ``graph_transport`` (the constrained ``gh api graphql``
transport and provider-traffic stats), ``graph_decode`` (payload parsing and
capability attribution), and ``project_overlay`` (Project V2 reads). This
module composes them into one traversal.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from ...config import GitHubTicketGraphConfig, ServerConfig
from ...domain.errors import CommandError
from ...domain.tickets import (
    GraphEvidenceCapability,
    TicketDiagnostic,
    TicketGraph,
    TicketGraphError,
    TicketGraphSnapshot,
    github_slug_from_remote_url,
)
from ...ports.command import CommandExecutor, CommandResult
from .graph_assembly import build_adjacency_maps, build_nodes_and_live
from .graph_batcher import fetch_batch
from .graph_coverage import build_capability_coverage
from .graph_decode import (
    _REPOSITORY,
    ExpandedIssue,
    parse_issue,
)
from .graph_transport import Stats
from .project_overlay import read_project_values
from .ticket_legacy_reader import (
    GitHubTicketGraphReader,  # noqa: F401 — re-export for compatibility
)

_MAX_GRAPH_NODES = 200
_MAX_BODY_CHARS = 200_000
_MAX_COMMENTS = 20
_MAX_COMMENT_CHARS = 20_000
#: Aliased issue queries per expansion request. Keeps worst-case response bytes
#: bounded while meeting the five-request budget for typical 40-node graphs.
_ALIAS_CHUNK = 25


class CommandGitHubTicketGraphGateway:
    """Traverse native sub-issues and dependencies without invoking GitHub writes."""

    def __init__(self, executor: CommandExecutor, server: ServerConfig) -> None:
        self._executor = executor
        self._server = server
        self._output_limit = min(max(server.max_tool_output_chars, 500_000), 5_000_000)

    def _run(self, argv: list[str], *, cwd: Path, output_limit: int | None = None) -> CommandResult:
        return self._executor.run(
            argv,
            cwd=cwd,
            timeout=self._server.default_command_timeout_seconds,
            output_limit=output_limit or self._output_limit,
        )

    # -- Slug resolution ----------------------------------------------------

    def _resolve_slug(
        self, cwd: Path, source: GitHubTicketGraphConfig, remote: str, stats: Stats
    ) -> str:
        if source.repository is not None:
            if not _REPOSITORY.fullmatch(source.repository):
                raise CommandError(
                    f"Unexpected configured GitHub repository name: {source.repository!r}"
                )
            return source.repository
        local = self._slug_from_remote(cwd, remote)
        if local is not None:
            return local
        started = time.monotonic()
        result = self._run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=cwd,
            output_limit=512,
        )
        stats.record(
            (),
            processes=1,
            response_bytes=len(result.stdout.encode("utf-8")),
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
        slug = result.stdout.strip()
        if not _REPOSITORY.fullmatch(slug):
            raise CommandError(f"Unexpected GitHub repository name: {slug!r}")
        return slug

    def _slug_from_remote(self, cwd: Path, remote: str) -> str | None:
        try:
            result = self._executor.run(
                ["git", "remote", "get-url", remote],
                cwd=cwd,
                check=False,
                timeout=30,
                output_limit=4_096,
            )
        except CommandError:
            return None
        if result.returncode != 0:
            return None
        slug = github_slug_from_remote_url(result.stdout.strip())
        return slug if slug is not None and _REPOSITORY.fullmatch(slug) else None

    # -- Graph read ----------------------------------------------------------

    def read(
        self,
        cwd: Path,
        source: GitHubTicketGraphConfig,
        *,
        max_items: int,
        remote: str = "origin",
    ) -> TicketGraphSnapshot:
        if not 1 <= max_items <= _MAX_GRAPH_NODES:
            raise TicketGraphError(
                "GitHub ticket graph reads must contain between 1 and 200 issues"
            )
        stats = Stats()
        slug = self._resolve_slug(cwd, source, remote, stats)

        parent_by_number: dict[int, int | None] = {source.root_issue: None}
        expanded: dict[int, ExpandedIssue] = {}
        unavailable: set[int] = set()
        truncated = False
        issue_unavailable: set[int] = set()
        sub_issues_unavailable: set[int] = set()
        sub_issues_truncated = False
        comments_unavailable: set[int] = set()
        comments_truncated = False
        dependencies_unavailable: set[int] = set()
        dependencies_truncated = False
        project_unavailable: set[int] = set()
        diagnostics: list[TicketDiagnostic] = []

        discovered: set[int] = {source.root_issue}
        frontier: list[int] = [source.root_issue]
        while frontier:
            if len(expanded) >= max_items:
                truncated = True
                for number in frontier:
                    unavailable.add(number)
                    issue_unavailable.add(number)
                break
            batch = frontier[:_ALIAS_CHUNK]
            frontier = frontier[_ALIAS_CHUNK:]
            try:
                parsed, failed = fetch_batch(self._executor, self._server, cwd, slug, batch, stats)
            except CommandError:
                if source.root_issue in batch:
                    raise
                for number in batch:
                    unavailable.add(number)
                    issue_unavailable.add(number)
                continue
            for number in batch:
                capabilities = failed.get(number, frozenset())
                raw_issue = parsed.get(number)
                issue = parse_issue(raw_issue, number, slug) if raw_issue is not None else None
                if issue is None or GraphEvidenceCapability.ISSUE in capabilities:
                    unavailable.add(number)
                    issue_unavailable.add(number)
                    continue
                if issue.labels_malformed or issue.labels_truncated:
                    unavailable.add(number)
                    issue_unavailable.add(number)
                if GraphEvidenceCapability.SUB_ISSUES in capabilities or issue.children_malformed:
                    unavailable.add(number)
                    sub_issues_unavailable.add(number)
                if GraphEvidenceCapability.COMMENTS in capabilities or issue.comments_malformed:
                    unavailable.add(number)
                    comments_unavailable.add(number)
                if GraphEvidenceCapability.DEPENDENCIES in capabilities or issue.blockers_malformed:
                    unavailable.add(number)
                    dependencies_unavailable.add(number)
                if issue.children_malformed_reason == "repository":
                    diagnostics.append(
                        TicketDiagnostic(
                            "EDGE_REPOSITORY_IDENTITY_MISSING",
                            number,
                            "sub-issue edge node is missing a valid repository identity; "
                            "local and cross-repository issue numbers cannot be "
                            "distinguished",
                        )
                    )
                if issue.blockers_malformed_reason == "repository":
                    diagnostics.append(
                        TicketDiagnostic(
                            "EDGE_REPOSITORY_IDENTITY_MISSING",
                            number,
                            "blocker edge node is missing a valid repository identity; "
                            "local and cross-repository issue numbers cannot be "
                            "distinguished",
                        )
                    )
                if issue.children_external:
                    unavailable.add(number)
                    sub_issues_unavailable.add(number)
                    diagnostics.append(
                        TicketDiagnostic(
                            "CROSS_REPOSITORY_RELATION_UNSUPPORTED",
                            number,
                            "sub-issue references from another repository are not "
                            f"expanded: {', '.join(issue.children_external)}",
                        )
                    )
                if issue.blockers_external:
                    unavailable.add(number)
                    dependencies_unavailable.add(number)
                    diagnostics.append(
                        TicketDiagnostic(
                            "CROSS_REPOSITORY_RELATION_UNSUPPORTED",
                            number,
                            "blocker references from another repository are not "
                            f"evaluated: {', '.join(issue.blockers_external)}",
                        )
                    )
                expanded[number] = issue
                if issue.children_truncated:
                    truncated = True
                    sub_issues_truncated = True
                if issue.blockers_truncated:
                    truncated = True
                    dependencies_truncated = True
                if issue.comments_truncated:
                    truncated = True
                    comments_truncated = True
                for child in issue.children:
                    existing_parent = parent_by_number.get(child)
                    if existing_parent is not None and existing_parent != number:
                        unavailable.add(child)
                        continue
                    parent_by_number[child] = number
                    if child in discovered:
                        continue
                    if len(discovered) >= max_items:
                        # Node budget exhausted: stop expanding this issue's
                        # children instead of enumerating every unseen child as
                        # unavailable, which can push thousands into the
                        # schema-bounded (200) unavailable set. One diagnostic
                        # per parent that hit the budget keeps the cause visible
                        # while truncation marks the traversal incomplete.
                        truncated = True
                        sub_issues_truncated = True
                        diagnostics.append(
                            TicketDiagnostic(
                                "SUB_ISSUE_TRAVERSAL_BUDGET_EXCEEDED",
                                number,
                                f"sub-issue traversal budget of {max_items} reached; "
                                "remaining children are not expanded",
                            )
                        )
                        break
                    discovered.add(child)
                    frontier.append(child)

        wanted = set(expanded)
        if source.root_issue not in wanted:
            raise TicketGraphError(f"GitHub ticket graph root #{source.root_issue} is unavailable")

        # Same-repository blockers that are not descendants of the root via
        # subIssues are still real dependency edges this traversal never
        # expanded. Dropping them silently would let `dependencies.complete`
        # stay true while known blockers are absent, so the capability is
        # failed closed and the caller cannot select the issue until the
        # dependency context is hydrated.
        for number in wanted:
            outside_scope_blockers = set(expanded[number].blockers) - wanted
            if not outside_scope_blockers:
                continue
            unavailable.add(number)
            dependencies_unavailable.add(number)
            diagnostics.append(
                TicketDiagnostic(
                    "DEPENDENCY_OUTSIDE_SELECTION_SCOPE",
                    number,
                    "Dependencies exist outside the selected traversal scope: "
                    + ", ".join(sorted(map(str, outside_scope_blockers))),
                )
            )

        project_started = time.monotonic()
        try:
            project_values, project_complete, project_bytes = read_project_values(
                self._executor, self._server, cwd, slug, source, wanted
            )
        except CommandError as exc:
            project_values = {}
            project_complete = False
            project_bytes = 0
            project_unavailable.update(wanted)
            diagnostics.append(
                TicketDiagnostic(
                    "PROJECT_OVERLAY_UNAVAILABLE",
                    source.root_issue,
                    "project overlay could not be read; GitHub-native graph "
                    f"evidence remains complete ({type(exc).__name__})",
                )
            )
        project_duration_ms = round((time.monotonic() - project_started) * 1000, 3)
        if source.project_owner is not None and source.project_number is not None:
            stats.record(
                (GraphEvidenceCapability.PROJECT_OVERLAY,),
                processes=1,
                response_bytes=project_bytes,
                duration_ms=project_duration_ms,
            )

        children_by_number, blockers_by_number, blocks_by_number = build_adjacency_maps(
            wanted,
            parent_by_number,
            expanded,
        )
        nodes, live = build_nodes_and_live(
            wanted,
            expanded,
            parent_by_number,
            children_by_number,
            blockers_by_number,
            blocks_by_number,
            project_values,
            source,
            diagnostics,
        )
        capability_coverage = build_capability_coverage(
            issue_unavailable,
            sub_issues_unavailable,
            sub_issues_truncated,
            comments_unavailable,
            comments_truncated,
            dependencies_unavailable,
            dependencies_truncated,
            project_unavailable,
            project_complete,
        )
        return TicketGraphSnapshot(
            graph=TicketGraph(1, source.root_issue, tuple(nodes)),
            observed_at=datetime.now(timezone.utc).isoformat(),
            evidence_complete=(
                not unavailable
                and not truncated
                and all(coverage.complete for coverage in capability_coverage)
            ),
            unavailable=tuple(sorted(unavailable)),
            truncated=truncated,
            live_issues=tuple(live),
            capability_coverage=capability_coverage,
            read_stats=stats.snapshot(),
            diagnostics=tuple(diagnostics),
            repository_slug=slug,
        )
