from pathlib import Path

from repoforge.application.onboarding.discover import OnboardingDiscoveryService
from repoforge.domain.onboarding import DiscoveryIdentity, ExclusionReason
from repoforge.ports.repository_discovery import DiscoveryRequest


class FakeDiscovery:
    def __init__(self, values):
        self.values = values

    def discover(self, request):
        return self.values


def test_classification_excludes_linked_and_enrolled_and_preserves_nested() -> None:
    values = (
        DiscoveryIdentity("/repos/main", "/repos/main", "/repos/main/.git", True, False),
        DiscoveryIdentity(
            "/repos/main/nested", "/repos/main/nested", "/repos/main/nested/.git", True, False
        ),
        DiscoveryIdentity(
            "/repos/.claude/worktrees/a",
            "/repos/.claude/worktrees/a",
            "/repos/main/.git",
            False,
            False,
        ),
        DiscoveryIdentity(
            "/repos/existing", "/repos/existing", "/repos/existing/.git", True, False
        ),
    )
    result = OnboardingDiscoveryService(FakeDiscovery(values)).discover(
        DiscoveryRequest((Path("/repos"),), 8, (), (), ()),
        enrolled=(("existing", "/repos/existing"),),
    )
    assert [item.repo_id for item in result.eligible] == ["main", "nested"]
    assert result.eligible[1].parent_repo_id == "main"
    assert {item.reason for item in result.exclusions} == {
        ExclusionReason.LINKED_WORKTREE,
        ExclusionReason.ALREADY_ENROLLED,
    }
