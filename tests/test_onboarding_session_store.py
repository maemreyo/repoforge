import os
from pathlib import Path

import pytest

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_onboarding_store import JsonOnboardingStore
from repoforge.domain.errors import ConfigError
from repoforge.domain.onboarding import OnboardingOptions, OnboardingSession


def make_session() -> OnboardingSession:
    return OnboardingSession.new(
        session_id="a" * 24,
        created_at="now",
        config_path="/tmp/config.toml",
        roots=("/repos",),
        options=OnboardingOptions(),
    )


def test_store_uses_private_permissions_and_optimistic_revision(tmp_path: Path) -> None:
    store = JsonOnboardingStore(tmp_path, FcntlLockManager(tmp_path / "locks"))
    created = store.create(make_session())
    saved = store.save(created, expected_revision=0)
    path = tmp_path / "onboarding" / f"{created.session_id}.json"
    assert saved.revision == 1
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(ConfigError, match="SESSION_STALE"):
        store.save(saved, expected_revision=0)


def test_store_rejects_corrupt_payload(tmp_path: Path) -> None:
    store = JsonOnboardingStore(tmp_path, FcntlLockManager(tmp_path / "locks"))
    path = tmp_path / "onboarding" / ("a" * 24 + ".json")
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ConfigError, match="SESSION_CORRUPT"):
        store.read("a" * 24)
