"""Deterministic proposal, decision, approval, and complete-batch planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ...domain.config_generation import (
    CapabilityDeltaKind,
    ConfigGeneration,
    classify_capability_delta,
    sha256_text,
)
from ...domain.errors import ConfigError
from ...domain.onboarding import (
    OnboardingBatchPlan,
    OnboardingRepositoryState,
    OnboardingSession,
    OnboardingStatus,
    RepositoryProgress,
    detect_duplicate_repo_ids,
    transition_session,
)
from ...domain.repository_proposal import EnrollmentMode, RepositoryProposal
from ..configuration.document import apply_proposal, parse_resolved, render_resolved
from ..configuration.source import (
    SourceConfiguration,
    SourceRepository,
    add_source_repository,
    render_source,
)
from ..repository_admin.proposals import RepositoryProposalService


@dataclass(frozen=True, slots=True)
class PlanningInput:
    template: EnrollmentMode
    decisions: tuple[tuple[str, str], ...]
    overrides: tuple[tuple[str, str], ...]
    approvals: tuple[str, ...]
    skip: bool = False


class OnboardingPlanner:
    def __init__(self, proposals: RepositoryProposalService):
        self._proposals = proposals

    @staticmethod
    def _required(proposal: RepositoryProposal) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        return tuple((item.code, item.prompt, item.choices) for item in proposal.required_decisions)

    def plan(
        self,
        session: OnboardingSession,
        *,
        current_source: SourceConfiguration | None,
        current_resolved_text: str | None,
        current_generation: ConfigGeneration | None,
        inputs: tuple[tuple[str, PlanningInput], ...],
        now: str,
        tunnel_id: str | None = None,
        profile: str = "repoforge",
    ) -> tuple[OnboardingSession, OnboardingBatchPlan | None]:
        duplicates = detect_duplicate_repo_ids(
            tuple(item.candidate for item in session.repositories)
        )
        if duplicates:
            raise ConfigError(
                "DUPLICATE_REPOSITORY_ID: "
                + "; ".join(f"{key}={','.join(paths)}" for key, paths in duplicates.items())
            )
        by_id = dict(inputs)
        states: list[OnboardingRepositoryState] = []
        approved: list[tuple[RepositoryProposal, PlanningInput, str]] = []
        for state in session.repositories:
            repo_id = state.candidate.repo_id
            value = by_id.get(
                repo_id, PlanningInput(EnrollmentMode(session.options.template), (), (), ())
            )
            if value.skip:
                states.append(
                    replace(
                        state, progress=RepositoryProgress.SKIPPED, template=value.template.value
                    )
                )
                continue
            proposal = self._proposals.propose(
                Path(state.candidate.identity.path),
                repo_id=repo_id,
                decisions=dict(value.decisions),
                template=value.template,
                overrides=dict(value.overrides),
            )
            payload = json.dumps(asdict(proposal), sort_keys=True, ensure_ascii=False, default=str)
            base = replace(
                state,
                template=value.template.value,
                decisions=tuple(sorted(value.decisions)),
                overrides=tuple(sorted(value.overrides)),
                proposal_id=proposal.proposal_id,
                facts_fingerprint=proposal.facts_fingerprint,
                required_decisions=self._required(proposal),
                proposal_json=payload,
                error_code=None,
            )
            if proposal.confidence.value == "blocked":
                states.append(
                    replace(
                        base, progress=RepositoryProgress.BLOCKED, error_code="PROPOSAL_BLOCKED"
                    )
                )
                continue
            if proposal.required_decisions:
                states.append(
                    replace(base, progress=RepositoryProgress.NEEDS_DECISION, approval_sha256=None)
                )
                continue
            required_token = f"approve:{proposal.proposal_id}"
            required_hash = hashlib.sha256(required_token.encode()).hexdigest()
            supplied = required_token in value.approvals or state.approval_sha256 == required_hash
            if not supplied:
                states.append(
                    replace(base, progress=RepositoryProgress.NEEDS_APPROVAL, approval_sha256=None)
                )
                continue
            # Reuse the production verifier so approval syntax stays one source of truth.
            approval_hash = self._proposals.verify_approval(proposal, required_token)
            states.append(
                replace(base, progress=RepositoryProgress.APPROVED, approval_sha256=approval_hash)
            )
            approved.append((proposal, value, approval_hash))
        updated = replace(session, repositories=tuple(states), updated_at=now)
        if any(item.progress is RepositoryProgress.NEEDS_DECISION for item in states) or any(
            item.progress is RepositoryProgress.BLOCKED for item in states
        ):
            return transition_session(updated, OnboardingStatus.AWAITING_DECISIONS, now=now), None
        if any(item.progress is RepositoryProgress.NEEDS_APPROVAL for item in states):
            return transition_session(updated, OnboardingStatus.AWAITING_APPROVAL, now=now), None
        selected = [item for item in approved]
        if not selected:
            return transition_session(updated, OnboardingStatus.READY, now=now), None
        source = current_source
        if source is None:
            if not tunnel_id:
                raise ConfigError("INPUT_REQUIRED: --tunnel-id is required for initial onboarding")
            source = SourceConfiguration(tunnel_id, profile, ())
        document = parse_resolved(current_resolved_text)
        fingerprints = current_generation.repository_fingerprint_map() if current_generation else {}
        for proposal, value, _approval_hash in selected:
            source = add_source_repository(
                source,
                SourceRepository(
                    proposal.repo_id,
                    proposal.path,
                    proposal.proposal_id,
                    value.template.value,
                    tuple(sorted(value.decisions)),
                    tuple(sorted(value.overrides)),
                ),
            )
            document = apply_proposal(document, proposal)
            fingerprints[proposal.repo_id] = proposal.facts_fingerprint
        source_text = render_source(source)
        proposal_ids = tuple(sorted(item[0].proposal_id for item in selected))
        combined = hashlib.sha256("\n".join(proposal_ids).encode()).hexdigest()
        resolved_text = render_resolved(
            document,
            generation=(current_generation.generation if current_generation else 0) + 1,
            source_path=session.config_path,
            source_sha256=sha256_text(source_text),
            created_at=now,
            reason="guided onboarding approved repository batch",
            proposal_id=combined,
            repository_fingerprints=tuple(sorted(fingerprints.items())),
        )
        delta = (
            classify_capability_delta(current_resolved_text, resolved_text).kind
            if current_resolved_text
            else CapabilityDeltaKind.EXPANSION
        )
        plan = OnboardingBatchPlan(
            source_text,
            resolved_text,
            tuple(item[0].repo_id for item in selected),
            proposal_ids,
            combined,
            tuple(sorted(item[2] for item in selected)),
            tuple(sorted(fingerprints.items())),
            delta.value,
        )
        return transition_session(updated, OnboardingStatus.READY, now=now), plan
