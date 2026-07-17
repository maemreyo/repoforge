"""Read-only verification profile drift assessment."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..config import RepositoryConfig
from ..domain.profile_drift import DetectedUnenrolledProfile, ProfileDriftAssessment
from .verification_detection import DetectedVerificationProfile, VerificationProfileDetector


def _proposal_payload(repo_id: str, candidate: DetectedVerificationProfile) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "set_profiles": [
            {
                "name": candidate.profile_id,
                "description": f"Detected from {', '.join(candidate.provenance)}",
                "commands": [list(candidate.argv)],
                "verification": candidate.verification,
                "timeout_seconds": candidate.timeout_seconds,
            }
        ],
        "dry_run": True,
    }


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ProfileDriftAssessor:
    def __init__(self, detector: VerificationProfileDetector | None = None) -> None:
        self._detector = detector or VerificationProfileDetector()

    def assess(
        self,
        repo: RepositoryConfig,
        *,
        head_sha: str,
        config_identity: str,
        policy_hash: str,
        source_dirty: bool,
        pending_proposals: dict[str, str] | None = None,
    ) -> ProfileDriftAssessment:
        """Compare detector candidates with active commands, never display names alone."""

        active_commands = {
            command for profile in repo.profiles.values() for command in profile.commands
        }
        pending = pending_proposals or {}
        candidates: list[DetectedUnenrolledProfile] = []
        for candidate in self._detector.detect(repo.path):
            if candidate.argv in active_commands:
                continue
            payload = _proposal_payload(repo.repo_id, candidate)
            digest = _payload_digest(payload)
            name_conflict = candidate.profile_id in repo.profiles
            reason = (
                "Detected command differs from the active profile with the same name."
                if name_conflict
                else "Detected reviewed-toolchain command is absent from active repository policy."
            )
            proposal_ready = not source_dirty
            candidates.append(
                DetectedUnenrolledProfile(
                    profile_id=candidate.profile_id,
                    command=candidate.argv,
                    provenance=candidate.provenance,
                    verification=candidate.verification,
                    timeout_seconds=candidate.timeout_seconds,
                    network_policy=candidate.network_policy,
                    mutability=candidate.mutability,
                    capability_delta="expansion",
                    reason=reason,
                    requires_operator_confirmation=(
                        candidate.requires_network_confirmation
                        or candidate.network_policy != "local_only"
                        or candidate.mutability != "read_only"
                    ),
                    proposal_ready=proposal_ready,
                    repo_policy_apply=payload if proposal_ready else None,
                    equivalent_pending_proposal_id=pending.get(digest),
                )
            )
        candidates.sort(key=lambda item: (item.profile_id, item.command))
        return ProfileDriftAssessment(
            repo_id=repo.repo_id,
            head_sha=head_sha,
            config_identity=config_identity,
            policy_hash=policy_hash,
            source_dirty=source_dirty,
            stale=source_dirty,
            detected_unenrolled_profiles=tuple(candidates),
        )
