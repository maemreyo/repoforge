"""Capability coverage computation for the bounded ticket-graph traversal.

Constructs the five-capability ``CapabilityCoverage`` tuple and parallel
``ObservationStamp`` provenance from the per-capability unavailable /
truncated sets accumulated during the BFS traversal (F-003).
"""

from __future__ import annotations

from datetime import datetime, timezone

from ...domain.observation import ObservationStamp
from ...domain.tickets import (
    CapabilityCoverage,
    GraphEvidenceCapability,
)


def build_capability_coverage(
    issue_unavailable: set[int],
    sub_issues_unavailable: set[int],
    sub_issues_truncated: bool,
    comments_unavailable: set[int],
    comments_truncated: bool,
    dependencies_unavailable: set[int],
    dependencies_truncated: bool,
    project_unavailable: set[int],
    project_complete: bool,
) -> tuple[CapabilityCoverage, ...]:
    """Construct the canonical five-capability coverage tuple.

    Each ``CapabilityCoverage`` reports which issues were unavailable for that
    capability and whether the result was truncated.  ``PROJECT_OVERLAY`` is
    never truncated (either read or unavailable).
    """
    return (
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


def build_observation_stamps(
    issue_unavailable: set[int],
    sub_issues_unavailable: set[int],
    sub_issues_truncated: bool,
    comments_unavailable: set[int],
    comments_truncated: bool,
    dependencies_unavailable: set[int],
    dependencies_truncated: bool,
    project_unavailable: set[int],
    project_complete: bool,
    *,
    observed_at: str,
    wanted: set[int],
) -> tuple[ObservationStamp, ...]:
    """Per-capability ``ObservationStamp`` parallel to the coverage tuple.

    Each stamp records the observation time, whether the capability was
    complete for its wanted scope, truncation, and how many wanted issues it
    actually observed.  ``PROJECT_OVERLAY`` is complete only when the read
    covered every wanted issue (F-003) — a listing that stopped before the
    wanted set is truncated, not complete.
    """
    return (
        ObservationStamp(
            capability=GraphEvidenceCapability.ISSUE,
            source="live_full",
            observed_at=observed_at,
            complete=not issue_unavailable,
            truncated=False,
            item_count=len(wanted - issue_unavailable),
        ),
        ObservationStamp(
            capability=GraphEvidenceCapability.SUB_ISSUES,
            source="live_full",
            observed_at=observed_at,
            complete=not sub_issues_unavailable and not sub_issues_truncated,
            truncated=sub_issues_truncated,
            item_count=len(wanted - sub_issues_unavailable),
        ),
        ObservationStamp(
            capability=GraphEvidenceCapability.COMMENTS,
            source="live_full",
            observed_at=observed_at,
            complete=not comments_unavailable and not comments_truncated,
            truncated=comments_truncated,
            item_count=len(wanted - comments_unavailable),
        ),
        ObservationStamp(
            capability=GraphEvidenceCapability.DEPENDENCIES,
            source="live_full",
            observed_at=observed_at,
            complete=not dependencies_unavailable and not dependencies_truncated,
            truncated=dependencies_truncated,
            item_count=len(wanted - dependencies_unavailable),
        ),
        ObservationStamp(
            capability=GraphEvidenceCapability.PROJECT_OVERLAY,
            source="live_full",
            observed_at=observed_at,
            complete=project_complete,
            truncated=not project_complete and bool(project_unavailable),
            item_count=len(wanted - project_unavailable),
        ),
    )


def current_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["build_capability_coverage", "build_observation_stamps", "current_utc_iso"]
