"""Per-candidate evidence evaluation for ticket-graph consumers (F-001).

Replaces the blanket ``snapshot.evidence_complete`` gate in ``repo_issue
next`` with an operation-specific ``EvidenceRequirement``.  A candidate is
selectable only when every capability its decision actually needs is present
and complete for that issue; capabilities the operation does not require
(comments, project overlay, ...) never block selection.
"""

from __future__ import annotations

from ...domain.observation import EvidenceRequirement, EvidenceVerdict
from ...domain.tickets import GraphEvidenceCapability, TicketGraphSnapshot
from .issue_graph_payloads import is_capability_complete_for_issue


def evaluate_candidate(
    snapshot: TicketGraphSnapshot | None,
    requirement: EvidenceRequirement,
    issue_number: int,
) -> EvidenceVerdict:
    """Whether one candidate issue has the evidence this operation needs.

    An issue in ``snapshot.unavailable`` has failed its core read and is never
    sufficient.  For each required capability, ``is_capability_complete_for_issue``
    decides completeness; a capability with no coverage entry is treated as
    complete (absence of evidence is not evidence of failure).
    """
    if snapshot is None:
        return EvidenceVerdict(
            sufficient=False,
            missing=(GraphEvidenceCapability.ISSUE,),
            diagnostics=("no graph observation is available",),
        )
    missing: list[GraphEvidenceCapability] = []
    if issue_number in snapshot.unavailable:
        missing.append(GraphEvidenceCapability.ISSUE)
    for capability in requirement.required_capabilities:
        if capability is GraphEvidenceCapability.ISSUE:
            continue
        if not is_capability_complete_for_issue(snapshot, capability, issue_number):
            missing.append(capability)
    if (
        GraphEvidenceCapability.ISSUE in requirement.required_capabilities
        and GraphEvidenceCapability.ISSUE not in missing
        and not is_capability_complete_for_issue(
            snapshot, GraphEvidenceCapability.ISSUE, issue_number
        )
    ):
        missing.append(GraphEvidenceCapability.ISSUE)
    unique = list(dict.fromkeys(missing))
    return EvidenceVerdict(
        sufficient=not unique,
        missing=tuple(unique),
        diagnostics=tuple(
            f"{capability.value} evidence is incomplete for issue #{issue_number}"
            for capability in unique
        ),
    )


__all__ = ["evaluate_candidate"]
