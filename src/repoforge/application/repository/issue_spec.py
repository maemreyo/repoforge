from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...config import RepositoryConfig
from ...domain.errors import CommandError
from ...domain.tickets import GraphEvidenceCapability, TicketGraph, TicketLiveMetadata
from ..context import ApplicationContext
from ..tickets.graph import (
    compare_live_ticket_metadata,
    declared_blocker_numbers,
    declared_status_drift,
)
from ..tickets.live import ticket_delivery_payload, ticket_live_state_from_issue
from .issue_graph import (
    capability_coverage_payload,
    is_capability_complete_for_issue,
    node_payload,
    read_github_ticket_snapshot,
)

_HEADING = re.compile(r"(?m)^#{2,3}\s+(.+)$")


def _first_heading(body: str) -> str | None:
    match = _HEADING.search(body)
    return match.group(1).strip() if match is not None else None


@dataclass(frozen=True, slots=True)
class RepositoryIssueSpecCommand:
    repo_id: str
    issue_number: int
    fresh: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryIssueSpecResult:
    repo_id: str
    issue_number: int
    source: str
    graph_member: bool
    node: dict[str, Any] | None
    live: dict[str, Any]
    drift: list[dict[str, Any]]
    comments: list[dict[str, Any]]
    evolution: dict[str, object]
    cache_hit: bool
    graph_cache_hit: bool
    observed_at: str | None
    evidence_complete: bool
    capability_coverage: list[dict[str, Any]]


class RepositoryIssueSpecReader:
    """Bounded references for one live issue and its GitHub-native graph metadata."""

    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryIssueSpecCommand) -> RepositoryIssueSpecResult:
        if c.issue_number <= 0:
            raise ValueError("issue_number must be positive")
        repo = self.ctx.repo(c.repo_id)
        return self.ctx.audited(
            "repo_issue_spec",
            {"repo_id": c.repo_id, "issue_number": c.issue_number, "fresh": c.fresh},
            lambda: self._load(c, repo),
        )

    def compute(self, c: RepositoryIssueSpecCommand) -> RepositoryIssueSpecResult:
        """Application logic without a nested audit event, for task-context bundles."""
        if c.issue_number <= 0:
            raise ValueError("issue_number must be positive")
        return self._load(c, self.ctx.repo(c.repo_id))

    def _load_live(
        self, c: RepositoryIssueSpecCommand, repo: RepositoryConfig
    ) -> tuple[dict[str, Any], bool]:
        return self.ctx.github_read(
            "issue",
            c.repo_id,
            repo.path,
            c.issue_number,
            fresh=c.fresh,
            loader=lambda: self.ctx.github.issue_read(repo.path, c.issue_number),
        )

    def _closed_blocker_drift(
        self, c: RepositoryIssueSpecCommand, repo: RepositoryConfig, body: str
    ) -> list[dict[str, Any]]:
        """Detect a declared blocker that is already closed on GitHub.

        Independent of ticket-graph membership (#187 addendum 2, #195): a
        stale ``Blocked by`` reference is detectable from live reads alone.
        A blocker that cannot be read (deleted, inaccessible, transient
        failure) is skipped rather than treated as evidence of anything --
        absence of evidence is not evidence of staleness."""
        drift: list[dict[str, Any]] = []
        for blocker_number in declared_blocker_numbers(body):

            def load_blocker(number: int = blocker_number) -> dict[str, Any]:
                return self.ctx.github.issue_read(repo.path, number)

            try:
                blocker_payload, _ = self.ctx.github_read(
                    "issue",
                    c.repo_id,
                    repo.path,
                    blocker_number,
                    fresh=c.fresh,
                    loader=load_blocker,
                )
            except CommandError:
                continue
            state = str(blocker_payload.get("state") or "").strip().upper()
            if state == "CLOSED":
                drift.append(
                    {
                        "code": "STALE_BLOCKER_REFERENCE",
                        "message": (
                            f"declared blocker #{blocker_number} is already closed on GitHub"
                        ),
                    }
                )
        return drift

    def _load(
        self,
        c: RepositoryIssueSpecCommand,
        repo: RepositoryConfig,
    ) -> RepositoryIssueSpecResult:
        live_payload, cache_hit = self._load_live(c, repo)
        snapshot = None
        graph_cache_hit = False
        if repo.ticket_graph is not None and self.ctx.ticket_graphs is not None:
            snapshot, graph_cache_hit = read_github_ticket_snapshot(
                self.ctx,
                repo,
                root_issue=None,
                fresh=c.fresh,
            )
        node = (
            next(
                (item for item in snapshot.graph.nodes if item.number == c.issue_number),
                None,
            )
            if snapshot is not None
            else None
        )

        comments: list[dict[str, Any]] = []
        raw_comments = live_payload.get("comments")
        if isinstance(raw_comments, list):
            for item in raw_comments:
                if not isinstance(item, dict):
                    continue
                body = item.get("body")
                comments.append(
                    dict(item, heading=_first_heading(body) if isinstance(body, str) else None)
                )

        live_state = ticket_live_state_from_issue(
            live_payload,
            expected_number=c.issue_number,
        )
        drift: list[dict[str, Any]] = []
        if not live_state.delivery.specification_complete:
            drift.append(
                {
                    "code": "LIVE_SPEC_INCOMPLETE",
                    "message": (
                        "live issue is missing objective, acceptance, or verification evidence"
                    ),
                }
            )
        drift.extend(self._closed_blocker_drift(c, repo, str(live_payload.get("body") or "")))
        if node is not None and snapshot is not None:
            if is_capability_complete_for_issue(
                snapshot, GraphEvidenceCapability.ISSUE, c.issue_number
            ):
                live_metadata = TicketLiveMetadata(
                    c.issue_number,
                    str(live_payload.get("title") or ""),
                    str(live_payload.get("state") or ""),
                    str(live_payload.get("body") or ""),
                )
                single_node_graph = TicketGraph(
                    snapshot.graph.schema_version,
                    snapshot.graph.program_issue,
                    (node,),
                )
                drift.extend(
                    {"code": item.code, "message": item.message}
                    for item in compare_live_ticket_metadata(single_node_graph, (live_metadata,))
                )
            else:
                drift.append(
                    {
                        "code": "GRAPH_EVIDENCE_INCOMPLETE_FOR_ISSUE",
                        "message": (
                            "graph metadata for this issue (status/priority/type) could not be "
                            "fully resolved from GitHub; skipping metadata drift comparison to "
                            "avoid comparing against a defaulted value"
                        ),
                    }
                )
        else:
            # No graph node to compare against (unconfigured ticket_graph, or an
            # issue that simply is not enrolled in it): still check the issue's
            # own self-declared Status against its live GitHub state, per #187
            # addendum 2 -- drift checks must run for any issue, graph member
            # or not, not only when a graph comparison target exists.
            status_drift = declared_status_drift(
                c.issue_number,
                str(live_payload.get("body") or ""),
                str(live_payload.get("state") or ""),
            )
            if status_drift is not None:
                drift.append({"code": status_drift.code, "message": status_drift.message})

        evolution = ticket_delivery_payload(live_state.delivery)

        return RepositoryIssueSpecResult(
            c.repo_id,
            c.issue_number,
            "github",
            node is not None,
            node_payload(node) if node is not None else None,
            live_payload,
            drift,
            comments,
            evolution,
            cache_hit,
            graph_cache_hit,
            snapshot.observed_at if snapshot is not None else None,
            snapshot.evidence_complete if snapshot is not None else False,
            capability_coverage_payload(snapshot),
        )
