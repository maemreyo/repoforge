"""Bounded removal-safety evidence for stale-workspace cleanup (issue #166).

Reuses the same adapters ``workspace_status`` and ``workspace_pr_status`` already
use (Git status/upstream comparison, ``ctx.github.status``) -- no new git or
GitHub plumbing. The PR-status GitHub read is only attempted for a workspace that
already looks locally safe (clean, fully pushed) and is capped to a small,
bounded number of candidates per call, so this can never fan out into an
unbounded number of live GitHub reads.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ...domain.workspace import WorkspaceRecord
from ...domain.workspace_removal import (
    PrLifecycleState,
    RemovalSafetyEvidence,
    classify_removal_safety,
    order_candidates,
)
from ..context import ApplicationContext

#: Bound on how many locally-clean candidates get a live GitHub PR-status read
#: per evidence-gathering call, regardless of how many workspaces are inspected.
MAX_PR_STATUS_READS = 5
#: Bound on how many candidates the stale-workspace nudge names at once.
MAX_NUDGE_CANDIDATES = 5


def _age_seconds(ctx: ApplicationContext, record: WorkspaceRecord) -> float | None:
    try:
        created = datetime.fromisoformat(record.created_at)
        now = datetime.fromisoformat(ctx.clock.now_iso())
        return max(0.0, (now - created).total_seconds())
    except (ValueError, TypeError):
        return None


def unpushed_commit_count(
    ctx: ApplicationContext, record: WorkspaceRecord, path: Path
) -> int | None:
    try:
        head = ctx.git.head_sha(path)
        upstream = ctx.git.upstream_name(path)
        if upstream:
            upstream_sha = ctx.git.upstream_sha(path)
            ahead, _behind = ctx.git.ahead_behind(path, head, upstream_sha)
            return ahead
        # Never pushed: the number of local-only commits ahead of the configured
        # base is the closest bounded proxy for "work that only exists here".
        base_sha = ctx.git.merge_base(path, head, record.base)
        ahead, _behind = ctx.git.ahead_behind(path, head, base_sha)
        return ahead
    except Exception:
        return None


def _pr_lifecycle_state(
    ctx: ApplicationContext, record: WorkspaceRecord, path: Path
) -> PrLifecycleState:
    try:
        status = ctx.github.status(path, record.branch)
    except Exception as exc:
        # The common case for a workspace with no pull request yet is a non-zero
        # `gh pr view` exit with a "no pull request(s) found" message; treat that
        # as a known, safe "none" rather than an unknown adapter failure. Any
        # other failure (network, auth, rate limit) degrades to unknown.
        if "no pull request" in str(exc).lower():
            return PrLifecycleState.NONE
        return PrLifecycleState.UNKNOWN
    state = str(status.get("state", "")).upper()
    if state == "MERGED":
        return PrLifecycleState.MERGED
    if state == "CLOSED":
        return PrLifecycleState.CLOSED
    if state == "OPEN":
        return PrLifecycleState.OPEN
    return PrLifecycleState.UNKNOWN


def compute_removal_safety(
    ctx: ApplicationContext,
    record: WorkspaceRecord,
    *,
    check_pr_status: bool,
) -> RemovalSafetyEvidence:
    """Compute one workspace's removal-safety evidence.

    ``check_pr_status`` gates the live GitHub read; callers bound how many
    workspaces get one per invocation via :data:`MAX_PR_STATUS_READS`.
    """
    path = Path(record.path)
    age_seconds = _age_seconds(ctx, record)
    if not path.is_dir():
        return classify_removal_safety(
            workspace_id=record.workspace_id,
            clean=None,
            unpushed_commits=None,
            pr_state=PrLifecycleState.UNKNOWN,
            age_seconds=age_seconds,
        )
    try:
        clean: bool | None = not bool(ctx.git.status_porcelain(path).strip())
    except Exception:
        clean = None
    unpushed = unpushed_commit_count(ctx, record, path)
    pr_state = PrLifecycleState.UNKNOWN
    if check_pr_status and clean is True and unpushed == 0:
        pr_state = _pr_lifecycle_state(ctx, record, path)
    return classify_removal_safety(
        workspace_id=record.workspace_id,
        clean=clean,
        unpushed_commits=unpushed,
        pr_state=pr_state,
        age_seconds=age_seconds,
    )


def compute_removal_candidates(
    ctx: ApplicationContext,
    records: list[WorkspaceRecord],
) -> list[RemovalSafetyEvidence]:
    """Compute bounded removal-safety evidence for every given workspace.

    Only the first :data:`MAX_PR_STATUS_READS` locally-clean, fully-pushed
    workspaces (in input order) get a live GitHub PR-status read; the rest are
    still classified from local evidence alone (a missing PR check degrades
    that workspace to "unknown", never to a false "safe").
    """
    evidence: list[RemovalSafetyEvidence] = []
    pr_reads_remaining = MAX_PR_STATUS_READS
    for record in records:
        allow_pr_check = pr_reads_remaining > 0
        item = compute_removal_safety(ctx, record, check_pr_status=allow_pr_check)
        # A read is only actually spent when the workspace passed the local
        # clean/fully-pushed gate inside compute_removal_safety; a workspace that
        # never reached the live GitHub call (dirty or unpushed) must not consume
        # the bounded budget meant for genuinely eligible candidates.
        if allow_pr_check and item.clean is True and item.unpushed_commits == 0:
            pr_reads_remaining -= 1
        evidence.append(item)
    return evidence


def build_stale_workspaces_nudge(ctx: ApplicationContext) -> dict[str, Any] | None:
    """Build the bounded ``stale_workspaces`` advisory nudge, or ``None`` if it
    should not fire right now.

    The nudge only fires once the number of safely-removable, sufficiently-aged
    workspaces meets ``server.stale_workspace_candidate_threshold``, and then only
    once per the nudge tracker's rate-limit window -- repeated ``workspace_create``/
    ``workspace_list`` calls in the same window do not repeat it. This never
    authorizes anything: ``workspace_remove`` still performs its own real-time
    safety check regardless of what this nudge lists.
    """
    server = ctx.config.server
    records = ctx.store.list()
    evidence = compute_removal_candidates(ctx, records)
    eligible = tuple(
        item
        for item in evidence
        if item.safe
        and item.age_seconds is not None
        and item.age_seconds >= server.stale_workspace_min_age_seconds
    )
    if len(eligible) < server.stale_workspace_candidate_threshold:
        return None
    tracker = ctx.nudge_tracker
    if tracker is None or not tracker.observe_stale_workspace_nudge(ctx.now_epoch()):
        return None
    ordered = order_candidates(eligible)[:MAX_NUDGE_CANDIDATES]
    return {
        "count": len(eligible),
        "candidates": [
            {
                "workspace_id": item.workspace_id,
                "age_seconds": item.age_seconds,
                "pr_state": item.pr_state,
            }
            for item in ordered
        ],
        "safe_next_action": (
            f"{len(eligible)} workspace(s) are safely removable (clean, fully pushed, "
            "no open pull request) -- call workspace_remove for the listed workspace_id(s) "
            "to reclaim disk space."
        ),
    }


__all__ = [
    "MAX_NUDGE_CANDIDATES",
    "MAX_PR_STATUS_READS",
    "build_stale_workspaces_nudge",
    "compute_removal_candidates",
    "compute_removal_safety",
    "unpushed_commit_count",
]
