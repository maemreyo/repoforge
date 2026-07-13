import hashlib
from dataclasses import replace

from test_onboarding_planner import FakeProposals, session

from repoforge.application.onboarding.planner import OnboardingPlanner, PlanningInput
from repoforge.domain.onboarding import OnboardingStatus
from repoforge.domain.repository_proposal import EnrollmentMode


def test_changed_proposal_invalidates_persisted_approval() -> None:
    planner = OnboardingPlanner(FakeProposals())
    initial = session()
    first, _ = planner.plan(
        initial,
        current_source=None,
        current_resolved_text=None,
        current_generation=None,
        inputs=(("demo", PlanningInput(EnrollmentMode.STANDARD, (), (), ())),),
        now="one",
        tunnel_id="t",
    )
    proposal_id = first.repositories[0].proposal_id
    approved_hash = hashlib.sha256(f"approve:{proposal_id}".encode()).hexdigest()
    persisted = replace(
        first,
        repositories=(replace(first.repositories[0], approval_sha256=approved_hash),),
        status=OnboardingStatus.DISCOVERED,
    )
    changed_planner = OnboardingPlanner(FakeProposals())
    changed, _ = changed_planner.plan(
        persisted,
        current_source=None,
        current_resolved_text=None,
        current_generation=None,
        inputs=(("demo", PlanningInput(EnrollmentMode.STRICT, (), (), ())),),
        now="two",
        tunnel_id="t",
    )
    assert changed.status is OnboardingStatus.AWAITING_APPROVAL
    assert changed.repositories[0].approval_sha256 is None
