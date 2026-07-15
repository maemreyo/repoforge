"""Repository inspection and deterministic proposal application service."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

from ...domain.repository_proposal import (
    EnrollmentMode,
    RepositoryProposal,
    build_repository_proposal,
)
from ...ports.repository_probe import RepositoryProbe
from ..verification_detection import VerificationProfileDetector


class RepositoryProposalService:
    def __init__(self, probe: RepositoryProbe):
        self._probe = probe

    def inspect(self, path: Path, *, repo_id: str | None = None) -> dict[str, object]:
        facts = self._probe.inspect(path, repo_id=repo_id)
        result = asdict(facts)
        result["root"] = str(facts.root)
        result["common_dir"] = str(facts.common_dir)
        return result

    def propose(
        self,
        path: Path,
        *,
        repo_id: str | None = None,
        decisions: dict[str, str] | None = None,
        template: EnrollmentMode = EnrollmentMode.STANDARD,
        overrides: dict[str, str] | None = None,
    ) -> RepositoryProposal:
        selected_decisions = decisions or {}
        return build_repository_proposal(
            self._probe.inspect(path, repo_id=repo_id),
            decisions=selected_decisions,
            template=template,
            overrides=overrides,
            detected_profiles=VerificationProfileDetector().proposed_profiles(
                path,
                include_dependency_setup=(
                    selected_decisions.get("dependency_install") == "include_non_verification"
                ),
            ),
        )

    @staticmethod
    def verify_approval(proposal: RepositoryProposal, approval_token: str | None) -> str:
        required = f"approve:{proposal.proposal_id}"
        if approval_token != required:
            raise ValueError(f"Approval required. Re-run with --approve {required}")
        return hashlib.sha256(required.encode()).hexdigest()
