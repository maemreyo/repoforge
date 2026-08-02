"""Per-candidate evidence evaluation for ticket-graph consumers (F-001).

Replaces the blanket ``snapshot.evidence_complete`` gate in ``repo_issue
next`` with an operation-specific ``EvidenceRequirement``.  A candidate is
selectable only when every capability its decision actually needs is present
and complete for that issue, within the requirement's freshness bounds;
capabilities the operation does not require (comments, project overlay, ...)
never block selection.

Fail-closed rules (F-001):

- a required capability with no coverage entry is insufficient — absence of
  evidence is not sufficiency;
- a truncated capability is insufficient for every candidate (global);
- an issue listed in a capability's ``unavailable`` is insufficient for that
  capability only (localized);
- ``max_age_ms`` / ``max_skew_ms`` are enforced against the per-capability
  observation stamps when a ``now_epoch`` is supplied.
"""

from __future__ import annotations

from datetime import datetime

from ...domain.observation import EvidenceRequirement, EvidenceVerdict
from ...domain.tickets import GraphEvidenceCapability, TicketGraphSnapshot
from .issue_graph_payloads import is_capability_complete_for_issue


def _stamp_epoch(observed_at: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def evaluate_candidate(
    snapshot: TicketGraphSnapshot | None,
    requirement: EvidenceRequirement,
    issue_number: int,
    *,
    now_epoch: float | None = None,
) -> EvidenceVerdict:
    """Whether one candidate issue has the evidence this operation needs.

    ``now_epoch`` enables ``max_age_ms`` / ``max_skew_ms`` enforcement against
    the observation stamps; when it is ``None`` those bounds are not applied
    (a caller that cannot supply a trusted clock must set them to ``None``).
    """
    if snapshot is None:
        return EvidenceVerdict(
            sufficient=False,
            missing=(GraphEvidenceCapability.ISSUE,),
            diagnostics=("no graph observation is available",),
        )
    missing: list[GraphEvidenceCapability] = []
    stale: list[GraphEvidenceCapability] = []
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

    stamp_epochs = [
        epoch
        for stamp in snapshot.observation_stamps
        if (epoch := _stamp_epoch(stamp.observed_at)) is not None
    ]
    if stamp_epochs:
        if requirement.max_age_ms is not None and now_epoch is not None:
            oldest = min(stamp_epochs)
            age_ms = (now_epoch - oldest) * 1000
            if age_ms > requirement.max_age_ms:
                stale.append(GraphEvidenceCapability.ISSUE)
        if requirement.max_skew_ms is not None:
            skew_ms = (max(stamp_epochs) - min(stamp_epochs)) * 1000
            if skew_ms > requirement.max_skew_ms:
                stale.append(GraphEvidenceCapability.ISSUE)

    unique_missing = list(dict.fromkeys(missing))
    unique_stale = list(dict.fromkeys(stale))
    sufficient = not unique_missing and not unique_stale
    return EvidenceVerdict(
        sufficient=sufficient,
        missing=tuple(unique_missing),
        stale=tuple(unique_stale),
        diagnostics=tuple(
            [
                f"{capability.value} evidence is incomplete for issue #{issue_number}"
                for capability in unique_missing
            ]
            + [
                f"{capability.value} evidence is stale for issue #{issue_number}"
                for capability in unique_stale
            ]
        ),
    )


__all__ = ["evaluate_candidate"]
