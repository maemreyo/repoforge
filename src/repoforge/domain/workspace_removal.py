"""Pure classification for workspace removal-safety and staleness evidence (issue #166).

``removal_safety`` answers "would removing this workspace destroy anything" from
already-gathered evidence (clean tree, unpushed-commit count, pull-request
lifecycle state). It never touches Git, GitHub, or the filesystem itself --
that is the application layer's job -- and it never authorizes anything:
``workspace_remove`` keeps performing its own real-time safety check
regardless of what this module classifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PrLifecycleState(str, Enum):
    MERGED = "merged"
    CLOSED = "closed"
    OPEN = "open"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RemovalSafetyEvidence:
    workspace_id: str
    clean: bool | None
    unpushed_commits: int | None
    pr_state: str
    age_seconds: float | None
    safe: bool
    blocking_reasons: tuple[str, ...]


def classify_removal_safety(
    *,
    workspace_id: str,
    clean: bool | None,
    unpushed_commits: int | None,
    pr_state: PrLifecycleState,
    age_seconds: float | None,
) -> RemovalSafetyEvidence:
    """Classify one workspace's removal safety from already-gathered evidence.

    A workspace is only ``safe`` when every signal is both known and clear: a
    clean tree, zero unpushed commits, and no open pull request. Any unknown
    signal (an adapter failure, a missing worktree, etc.) degrades to "not a
    candidate" rather than a false claim of safety.
    """
    reasons: list[str] = []
    if clean is None:
        reasons.append("unknown_tree_state")
    elif clean is False:
        reasons.append("dirty_tree")
    if unpushed_commits is None:
        reasons.append("unknown_push_state")
    elif unpushed_commits > 0:
        reasons.append("unpushed_commits")
    if pr_state is PrLifecycleState.UNKNOWN:
        reasons.append("unknown_pull_request_state")
    elif pr_state is PrLifecycleState.OPEN:
        reasons.append("open_pull_request")
    return RemovalSafetyEvidence(
        workspace_id=workspace_id,
        clean=clean,
        unpushed_commits=unpushed_commits,
        pr_state=pr_state.value,
        age_seconds=age_seconds,
        safe=not reasons,
        blocking_reasons=tuple(reasons),
    )


def order_candidates(
    evidence: tuple[RemovalSafetyEvidence, ...],
) -> tuple[RemovalSafetyEvidence, ...]:
    """Deterministically order safe candidates oldest-first; unsafe entries are dropped."""
    safe = [item for item in evidence if item.safe]
    return tuple(
        sorted(safe, key=lambda item: (item.age_seconds is None, -(item.age_seconds or 0.0)))
    )


__all__ = [
    "PrLifecycleState",
    "RemovalSafetyEvidence",
    "classify_removal_safety",
    "order_candidates",
]
