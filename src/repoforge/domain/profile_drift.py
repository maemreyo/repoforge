"""Snapshot-bound verification profile drift contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DetectedUnenrolledProfile:
    profile_id: str
    command: tuple[str, ...]
    provenance: tuple[str, ...]
    verification: bool
    timeout_seconds: int
    network_policy: str
    mutability: str
    capability_delta: str
    reason: str
    requires_operator_confirmation: bool
    proposal_ready: bool
    repo_policy_apply: dict[str, Any] | None
    equivalent_pending_proposal_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "command": list(self.command),
            "provenance": list(self.provenance),
            "verification": self.verification,
            "timeout_seconds": self.timeout_seconds,
            "network_policy": self.network_policy,
            "mutability": self.mutability,
            "capability_delta": self.capability_delta,
            "reason": self.reason,
            "requires_operator_confirmation": self.requires_operator_confirmation,
            "proposal_ready": self.proposal_ready,
            "repo_policy_apply": self.repo_policy_apply,
            "equivalent_pending_proposal_id": self.equivalent_pending_proposal_id,
        }


@dataclass(frozen=True, slots=True)
class ProfileDriftAssessment:
    repo_id: str
    head_sha: str
    config_identity: str
    policy_hash: str
    source_dirty: bool
    stale: bool
    detected_unenrolled_profiles: tuple[DetectedUnenrolledProfile, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "head_sha": self.head_sha,
            "config_identity": self.config_identity,
            "policy_hash": self.policy_hash,
            "source_dirty": self.source_dirty,
            "stale": self.stale,
            "detected_unenrolled_profiles": [
                item.as_dict() for item in self.detected_unenrolled_profiles
            ],
        }
