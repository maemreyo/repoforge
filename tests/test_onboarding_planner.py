import hashlib
from dataclasses import replace
from pathlib import Path

from repoforge.application.onboarding.planner import OnboardingPlanner, PlanningInput
from repoforge.domain.config_generation import CapabilityDeltaKind
from repoforge.domain.onboarding import (
    DiscoveryCandidate,
    DiscoveryIdentity,
    OnboardingOptions,
    OnboardingRepositoryState,
    OnboardingSession,
    OnboardingStatus,
    RepositoryProgress,
)
from repoforge.domain.repository_proposal import (
    EnrollmentMode,
    ProposalConfidence,
    RepositoryPolicyProposal,
    RepositoryProposal,
    RequiredDecision,
)


class FakeProposals:
    def __init__(self, *, decision=False, blocked=False):
        self.decision = decision
        self.blocked = blocked

    def propose(
        self,
        path: Path,
        *,
        repo_id=None,
        decisions=None,
        template=EnrollmentMode.STANDARD,
        overrides=None,
    ):
        required = (
            ()
            if not self.decision or decisions
            else (RequiredDecision("manager", "Choose", ("uv", "pip")),)
        )
        proposal_id = hashlib.sha256(
            f"{repo_id}:{template.value}:{sorted((decisions or {}).items())}".encode()
        ).hexdigest()
        return RepositoryProposal(
            proposal_id,
            "f" * 64,
            str(repo_id),
            str(path),
            ProposalConfidence.BLOCKED if self.blocked else ProposalConfidence.HIGH,
            (),
            required,
            RepositoryPolicyProposal(
                template,
                "origin",
                "main",
                ("main",),
                (),
                (".git",),
                (),
                template is not EnrollmentMode.READ_ONLY,
                10,
                100,
                1000,
            ),
            CapabilityDeltaKind.EXPANSION,
        )

    @staticmethod
    def verify_approval(proposal, token):
        assert token == f"approve:{proposal.proposal_id}"
        return hashlib.sha256(token.encode()).hexdigest()


def session() -> OnboardingSession:
    value = OnboardingSession.new(
        session_id="a" * 24,
        created_at="now",
        config_path="/tmp/config.toml",
        roots=("/repos",),
        options=OnboardingOptions(),
    )
    candidate = DiscoveryCandidate(
        DiscoveryIdentity("/repos/demo", "/repos/demo", "/repos/demo/.git", True, False), "demo"
    )
    return replace(
        value,
        status=OnboardingStatus.DISCOVERED,
        repositories=(OnboardingRepositoryState(candidate),),
    )


def test_planner_requires_decision_then_exact_approval() -> None:
    planner = OnboardingPlanner(FakeProposals(decision=True))
    updated, plan = planner.plan(
        session(),
        current_source=None,
        current_resolved_text=None,
        current_generation=None,
        inputs=(("demo", PlanningInput(EnrollmentMode.STANDARD, (), (), ())),),
        now="later",
        tunnel_id="t",
    )
    assert updated.status is OnboardingStatus.AWAITING_DECISIONS and plan is None
    decided = PlanningInput(EnrollmentMode.STANDARD, (("manager", "uv"),), (), ())
    updated, plan = planner.plan(
        session(),
        current_source=None,
        current_resolved_text=None,
        current_generation=None,
        inputs=(("demo", decided),),
        now="later",
        tunnel_id="t",
    )
    assert updated.status is OnboardingStatus.AWAITING_APPROVAL and plan is None
    proposal_id = updated.repositories[0].proposal_id
    approved = replace(decided, approvals=(f"approve:{proposal_id}",))
    updated, plan = planner.plan(
        updated,
        current_source=None,
        current_resolved_text=None,
        current_generation=None,
        inputs=(("demo", approved),),
        now="later2",
        tunnel_id="t",
    )
    assert updated.status is OnboardingStatus.READY and plan is not None
    assert "approve:" not in repr(updated)
    assert plan.repo_ids == ("demo",)


def test_blocked_proposal_never_becomes_ready() -> None:
    updated, plan = OnboardingPlanner(FakeProposals(blocked=True)).plan(
        session(),
        current_source=None,
        current_resolved_text=None,
        current_generation=None,
        inputs=(("demo", PlanningInput(EnrollmentMode.STANDARD, (), (), ("approve:x",))),),
        now="later",
        tunnel_id="t",
    )
    assert updated.repositories[0].progress is RepositoryProgress.BLOCKED and plan is None


def test_duplicate_ids_can_be_resolved_before_planning() -> None:
    first = DiscoveryCandidate(
        DiscoveryIdentity("/one/same", "/one/same", "/one/same/.git", True, False),
        "same",
    )
    second = DiscoveryCandidate(
        DiscoveryIdentity("/two/same", "/two/same", "/two/same/.git", True, False),
        "same-two",
    )
    value = replace(
        session(),
        repositories=(OnboardingRepositoryState(first), OnboardingRepositoryState(second)),
    )
    planner = OnboardingPlanner(FakeProposals())
    updated, _ = planner.plan(
        value,
        current_source=None,
        current_resolved_text=None,
        current_generation=None,
        inputs=(
            ("same", PlanningInput(EnrollmentMode.STANDARD, (), (), ())),
            ("same-two", PlanningInput(EnrollmentMode.STANDARD, (), (), ())),
        ),
        now="later",
        tunnel_id="t",
    )
    assert updated.status is OnboardingStatus.AWAITING_APPROVAL
