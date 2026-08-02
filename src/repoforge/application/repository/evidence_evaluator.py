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
- ``max_age_ms`` / ``max_skew_ms`` are enforced only against stamps whose
  ``capability`` is in the requirement's required set — unrelated stamps
  (comments, project overlay, ...) never participate in age or skew.
"""

from __future__ import annotations

from datetime import datetime

from ...domain.observation import EvidenceRequirement, EvidenceVerdict, ObservationStamp
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


def _stamps_by_capability(
    stamps: tuple[ObservationStamp, ...],
) -> dict[GraphEvidenceCapability, ObservationStamp]:
    """Map each capability to its stamp; later duplicates overwrite earlier ones.

    Callers that need uniqueness must validate at the codec boundary.  The
    evaluator takes the last stamp per capability so a well-formed snapshot
    (one stamp each) is unambiguous.
    """
    by_capability: dict[GraphEvidenceCapability, ObservationStamp] = {}
    for stamp in stamps:
        if stamp.capability is not None:
            by_capability[stamp.capability] = stamp
    return by_capability


def evaluate_candidate(
    snapshot: TicketGraphSnapshot | None,
    requirement: EvidenceRequirement,
    issue_number: int,
    *,
    now_epoch: float | None = None,
) -> EvidenceVerdict:
    """Whether one candidate issue has the evidence this operation needs.

    ``now_epoch`` enables ``max_age_ms`` / ``max_skew_ms`` enforcement against
    the observation stamps of *required* capabilities only; when it is
    ``None`` those bounds are not applied (a caller that cannot supply a
    trusted clock must set them to ``None``).
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

    if requirement.max_age_ms is not None or requirement.max_skew_ms is not None:
        stamps_by_capability = _stamps_by_capability(snapshot.observation_stamps)
        required_epochs: list[tuple[GraphEvidenceCapability, float]] = []
        for capability in requirement.required_capabilities:
            stamp = stamps_by_capability.get(capability)
            if stamp is None:
                stale.append(capability)
                continue
            epoch = _stamp_epoch(stamp.observed_at)
            if epoch is None:
                stale.append(capability)
                continue
            required_epochs.append((capability, epoch))

        if requirement.max_age_ms is not None and now_epoch is not None:
            for capability, epoch in required_epochs:
                age_ms = (now_epoch - epoch) * 1000
                if age_ms > requirement.max_age_ms:
                    stale.append(capability)

        if requirement.max_skew_ms is not None and len(required_epochs) >= 2:
            epochs = [epoch for _, epoch in required_epochs]
            skew_ms = (max(epochs) - min(epochs)) * 1000
            if skew_ms > requirement.max_skew_ms:
                oldest_epoch = min(epochs)
                for capability, epoch in required_epochs:
                    if epoch == oldest_epoch:
                        stale.append(capability)

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
