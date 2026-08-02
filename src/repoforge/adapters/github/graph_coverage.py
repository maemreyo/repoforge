"""Capability coverage computation for the bounded ticket-graph traversal.

Constructs the five-capability ``CapabilityCoverage`` tuple from the
per-capability unavailable / truncated sets accumulated during the BFS
traversal.
"""

from __future__ import annotations

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


__all__ = ["build_capability_coverage"]
