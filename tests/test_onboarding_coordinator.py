from dataclasses import replace
from pathlib import Path

from test_onboarding_planner import FakeProposals

from repoforge.application.onboarding.coordinator import OnboardingCommand, OnboardingCoordinator
from repoforge.application.onboarding.discover import OnboardingDiscoveryService
from repoforge.application.onboarding.planner import OnboardingPlanner
from repoforge.application.onboarding.preflight import OnboardingPreflightService
from repoforge.domain.config_generation import CapabilityDeltaKind, ConfigGeneration
from repoforge.domain.onboarding import DiscoveryIdentity, OnboardingOptions, OnboardingStatus
from repoforge.ports.onboarding_environment import EnvironmentPreflight


class MemorySessions:
    def __init__(self):
        self.value = None

    def create(self, s):
        self.value = s
        return s

    def read(self, i):
        return self.value if self.value and self.value.session_id == i else None

    def save(self, s, *, expected_revision):
        assert self.value.revision == expected_revision
        self.value = replace(s, revision=expected_revision + 1)
        return self.value

    def cancel(self, i, *, expected_revision, updated_at):
        self.value = replace(
            self.value,
            status=OnboardingStatus.CANCELLED,
            revision=expected_revision + 1,
            updated_at=updated_at,
        )
        return self.value


class RawDiscovery:
    def discover(self, request):
        return (DiscoveryIdentity("/repos/demo", "/repos/demo", "/repos/demo/.git", True, False),)


class Env:
    def inspect(self, path):
        return EnvironmentPreflight(
            "/rf", "/py", None, None, "git", "gh", True, "tunnel", False, True, ()
        )


class Clock:
    def __init__(self):
        self.n = 0

    def now_iso(self):
        self.n += 1
        return f"now-{self.n}"


class IDs:
    def new_hex(self, length=10):
        return "a" * length


class Store:
    def __init__(self):
        self.source_path = Path("/tmp/config.toml")
        self.root = Path("/tmp/state")
        self.accept_calls = []

    def current(self):
        return None

    def active(self):
        return None

    def read_source_text(self):
        raise AssertionError

    def read_resolved_text(self, generation=None):
        raise AssertionError

    def accept(self, mutation):
        self.accept_calls.append(mutation)
        return ConfigGeneration(
            1,
            "a" * 64,
            "b" * 64,
            mutation.repository_fingerprints,
            "now",
            "reason",
            mutation.proposal_id,
            mutation.approval,
            CapabilityDeltaKind.EXPANSION,
            None,
        )


def make_coordinator(store, sessions, smoke, activate):
    return OnboardingCoordinator(
        sessions=sessions,
        discovery=OnboardingDiscoveryService(RawDiscovery()),
        preflight=OnboardingPreflightService(Env()),
        planner=OnboardingPlanner(FakeProposals()),
        configs=store,
        clock=Clock(),
        ids=IDs(),
        smoke=smoke,
        activate=activate,
    )


def test_plan_only_never_mutates() -> None:
    store = Store()
    sessions = MemorySessions()
    calls = []
    coordinator = make_coordinator(
        store, sessions, lambda *a: calls.append("smoke"), lambda *a: calls.append("activate")
    )
    first = coordinator.run(
        OnboardingCommand(
            Path("/tmp/config.toml"),
            (Path("/repos"),),
            OnboardingOptions(),
            plan_only=True,
            tunnel_id="t",
        )
    )
    assert first.session.status is OnboardingStatus.AWAITING_APPROVAL
    token = "approve:" + first.session.repositories[0].proposal_id
    result = coordinator.run(
        OnboardingCommand(
            Path("/tmp/config.toml"),
            (Path("/repos"),),
            OnboardingOptions(),
            approvals=(token,),
            resume_session_id=first.session.session_id,
            plan_only=True,
            tunnel_id="t",
        )
    )
    assert result.plan is not None and not store.accept_calls and not calls


def test_complete_batch_smokes_accepts_once_and_activates_once() -> None:
    store = Store()
    sessions = MemorySessions()
    calls = []
    coordinator = make_coordinator(
        store,
        sessions,
        lambda *a: calls.append("smoke") or (),
        lambda *a: calls.append("activate") or {"active_generation": 1},
    )
    first = coordinator.run(
        OnboardingCommand(
            Path("/tmp/config.toml"), (Path("/repos"),), OnboardingOptions(), tunnel_id="t"
        )
    )
    token = "approve:" + first.session.repositories[0].proposal_id
    result = coordinator.run(
        OnboardingCommand(
            Path("/tmp/config.toml"),
            (Path("/repos"),),
            OnboardingOptions(activate="always"),
            approvals=(token,),
            resume_session_id=first.session.session_id,
            tunnel_id="t",
        )
    )
    assert result.session.status is OnboardingStatus.COMPLETED
    assert len(store.accept_calls) == 1 and calls == ["smoke", "activate"]


def test_all_already_enrolled_completes_without_mutation() -> None:
    class ExistingStore(Store):
        def __init__(self):
            super().__init__()
            self._current = ConfigGeneration(
                1,
                "a" * 64,
                "b" * 64,
                (("demo", "c" * 64),),
                "now",
                "reason",
                None,
                None,
                CapabilityDeltaKind.EQUIVALENT,
                None,
            )

        def current(self):
            return self._current

        def read_source_text(self):
            return 'version = 2\n\n[tunnel]\nid = "t"\nprofile = "repoforge"\n\n[[repo]]\nid = "demo"\npath = "/repos/demo"\nproposal_id = "p"\npolicy_template = "standard"\n'

        def read_resolved_text(self, generation=None):
            return ""

    store = ExistingStore()
    sessions = MemorySessions()
    calls = []
    coordinator = make_coordinator(
        store,
        sessions,
        lambda *a: calls.append("smoke"),
        lambda *a: calls.append("activate"),
    )
    result = coordinator.run(
        OnboardingCommand(
            Path("/tmp/config.toml"),
            (Path("/repos"),),
            OnboardingOptions(),
        )
    )
    assert result.session.status is OnboardingStatus.COMPLETED
    assert not store.accept_calls and not calls
