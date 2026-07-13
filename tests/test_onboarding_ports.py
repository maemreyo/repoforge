from pathlib import Path
from typing import get_type_hints

from repoforge.ports.onboarding_store import OnboardingStore
from repoforge.ports.repository_discovery import DiscoveryRequest


def test_discovery_request_is_bounded_and_explicit() -> None:
    request = DiscoveryRequest(
        (Path("/repos"),), 6, (), ("**/.venv/**",), (Path("/state/workspaces"),)
    )
    assert request.max_depth == 6


def test_onboarding_store_exposes_optimistic_save() -> None:
    assert "expected_revision" in get_type_hints(OnboardingStore.save)
