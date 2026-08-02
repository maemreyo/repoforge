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
from typing import Any

from ...config import GitHubTicketGraphConfig, ServerConfig
from ...domain.errors import CommandError
from ...domain.tickets import (
    CapabilityCoverage,
    GraphEvidenceCapability,
    TicketDiagnostic,
    TicketGraph,
    TicketGraphError,
    TicketGraphSnapshot,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
    github_slug_from_remote_url,
)
from ...ports.command import CommandExecutor, CommandResult
from .graph_decode import (
    _REPOSITORY,
    ExpandedIssue,
    alias_issue,
    enum_value,
    failed_capabilities,
    parse_issue,
    parse_metadata,
)
from .graph_query import FULL_SELECTION, build_query, selection_capabilities, stripped_selection
from .graph_transport import Stats, run_graphql
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

    def _fetch_batch(
        self,
        cwd: Path,
        slug: str,
        numbers: list[int],
        stats: Stats,
    ) -> tuple[dict[int, dict[str, Any]], dict[int, frozenset[GraphEvidenceCapability]]]:
        """Fetch one frontier level with aliased GraphQL, retrying failed
        aliases with progressively stripped selections.

        Returns ``(parsed_issue_dicts, failed_capabilities_by_number)``. A
        number present in ``parsed`` is usable; its entry in the second dict
        lists the capabilities that could not be read for it. Core issue
        identity/metadata is never accepted partially: any error on the issue
        object itself (including a missing object without an attributable
        error) fails the whole issue closed.
        """
        parsed: dict[int, dict[str, Any]] = {}
        failed: dict[int, set[GraphEvidenceCapability]] = {}
        pending: dict[int, str] = {number: FULL_SELECTION for number in numbers}
        while pending:
            selection = next(iter(pending.values()))
            group = [number for number, current in pending.items() if current == selection]
            query = build_query(slug, group, selection)
            payload, errors = run_graphql(
                self._executor,
                self._server,
                cwd,
                query,
                stats,
                capabilities=selection_capabilities(selection),
            )
            for index, number in enumerate(group):
                alias = f"r{index}"
                capabilities = failed_capabilities(errors, alias)
                issue = alias_issue(payload, alias)
                if issue is not None and not capabilities:
                    parsed[number] = issue
                    pending.pop(number, None)
                    continue
                if GraphEvidenceCapability.ISSUE in capabilities:
                    failed.setdefault(number, set()).add(GraphEvidenceCapability.ISSUE)
                    pending.pop(number, None)
                    continue
                if issue is not None:
                    # Partial object with optional-capability failures: retry
                    # stripped so accepted data only carries capabilities that
                    # actually succeeded.
                    failed.setdefault(number, set()).update(capabilities)
                    next_selection = stripped_selection(failed[number])
                    if next_selection == selection:
                        parsed[number] = issue
                        pending.pop(number, None)
                    else:
                        pending[number] = next_selection
                    continue
                # No issue object, only optional-capability failures: retry
                # stripped; if stripping changes nothing the object is truly
                # missing and the core read fails closed.
                failed.setdefault(number, set()).update(capabilities)
                next_selection = stripped_selection(failed[number])
                if next_selection == selection:
                    failed.setdefault(number, set()).add(GraphEvidenceCapability.ISSUE)
                    pending.pop(number, None)
                else:
                    pending[number] = next_selection
        return parsed, {number: frozenset(caps) for number, caps in failed.items()}

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
                parsed, failed = self._fetch_batch(cwd, slug, batch, stats)
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

        children_by_number: dict[int, set[int]] = {number: set() for number in wanted}
        for child_number, parent_number in parent_by_number.items():
            if child_number in wanted and parent_number in wanted:
                children_by_number[parent_number].add(child_number)
        blockers_by_number: dict[int, set[int]] = {number: set() for number in wanted}
        for number in wanted:
            blockers_by_number[number] = {
                blocker for blocker in expanded[number].blockers if blocker in wanted
            }
        blocks_by_number: dict[int, set[int]] = {number: set() for number in wanted}
        for blocked_number, blocker_numbers in blockers_by_number.items():
            for blocker_number in blocker_numbers:
                blocks_by_number[blocker_number].add(blocked_number)

        nodes: list[TicketNode] = []
        live: list[TicketLiveMetadata] = []
        for number in sorted(wanted):
            issue = expanded[number]
            title = issue.title
            state = issue.state
            body = issue.body
            metadata = parse_metadata(body, issue.labels)
            overlay = project_values.get(number, {})
            status = (
                TicketStatus.DONE
                if state == "CLOSED"
                else enum_value(
                    TicketStatus,
                    overlay.get(source.status_field) or metadata.get("status"),
                )
            )
            priority = enum_value(
                TicketPriority,
                overlay.get(source.priority_field) or metadata.get("priority"),
            )
            ticket_type = enum_value(
                TicketType,
                overlay.get(source.type_field) or metadata.get("type"),
            )
            defaulted_fields: list[str] = []
            if status is None:
                status = TicketStatus.BACKLOG
                defaulted_fields.append("status")
            if priority is None:
                priority = TicketPriority.P3
                defaulted_fields.append("priority")
            if ticket_type is None:
                if number == source.root_issue:
                    ticket_type = TicketType.PROGRAM
                elif children_by_number[number]:
                    ticket_type = TicketType.INITIATIVE
                else:
                    ticket_type = TicketType.IMPLEMENTATION_TICKET
            if defaulted_fields:
                diagnostics.append(
                    TicketDiagnostic(
                        "METADATA_DEFAULTED",
                        number,
                        "metadata fields "
                        + ", ".join(defaulted_fields)
                        + " are missing; defaulted for readiness",
                    )
                )
            initiative = overlay.get(source.initiative_field) or metadata.get("initiative")
            roadmap = (
                (initiative.strip(),)
                if isinstance(initiative, str) and initiative.strip()
                else ("github",)
            )
            nodes.append(
                TicketNode(
                    number=number,
                    title=title,
                    ticket_type=ticket_type,
                    priority=priority,
                    status=status,
                    parent=parent_by_number.get(number),
                    blockers=tuple(sorted(blockers_by_number[number])),
                    blocks=tuple(sorted(blocks_by_number[number])),
                    children=tuple(sorted(children_by_number[number])),
                    roadmap=roadmap,
                )
            )
            live.append(
                TicketLiveMetadata(
                    number,
                    title,
                    state,
                    body,
                    expanded[number].comments,
                )
            )

        capability_coverage = (
            CapabilityCoverage(
                GraphEvidenceCapability.ISSUE,
                not issue_unavailable,
                tuple(sorted(issue_unavailable)),
                False,
            ),
            CapabilityCoverage(
                GraphEvidenceCapability.SUB_ISSUES,
                not sub_issues_unavailable and not sub_issues_truncated,
                tuple(sorted(sub_issues_unavailable)),
                sub_issues_truncated,
            ),
            CapabilityCoverage(
                GraphEvidenceCapability.COMMENTS,
                not comments_unavailable and not comments_truncated,
                tuple(sorted(comments_unavailable)),
                comments_truncated,
            ),
            CapabilityCoverage(
                GraphEvidenceCapability.DEPENDENCIES,
                not dependencies_unavailable and not dependencies_truncated,
                tuple(sorted(dependencies_unavailable)),
                dependencies_truncated,
            ),
            CapabilityCoverage(
                GraphEvidenceCapability.PROJECT_OVERLAY,
                project_complete,
                tuple(sorted(project_unavailable)),
                False,
            ),
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
