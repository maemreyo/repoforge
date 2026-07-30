"""Observing the stable identity of a configured repository, read-only.

Identity resolution is anchored to the provider host plus the stable numeric repository ID, so
the observer must read both from the provider rather than trusting a configured name. A rename
must be observable as the same repository; a missing or ambiguous answer must fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.github.repository_observation import GhCliRepositoryObserver
from repoforge.config import RepositoryConfig
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.repository_identity import RepositoryProvider
from repoforge.ports.command import CommandResult
from repoforge.testing import FixedClock

NOW = "2026-07-30T00:00:00+00:00"
_SHA = "a" * 64


class RecordingExecutor:
    def __init__(self, results: list[CommandResult | Exception]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "HOME": "/home/demo",
            "PATH": "/safe/bin",
            "GH_TOKEN": "gho_ambient_token_canary_777",
            "GH_HOST": "wrong.example",
            **dict(extra or {}),
        }

    def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"argv": tuple(argv), **kwargs})
        if not self.results:
            raise AssertionError(f"unhandled command: {argv}")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ok(payload: object) -> CommandResult:
    return CommandResult(("gh",), "/repo", 0, json.dumps(payload), "")


def _repo(tmp_path: Path) -> RepositoryConfig:
    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    return RepositoryConfig(repo_id="demo", path=root)


def _observer(results: list[CommandResult | Exception]) -> tuple[GhCliRepositoryObserver, Any]:
    executor = RecordingExecutor(results)
    return GhCliRepositoryObserver(executor, clock=FixedClock(NOW)), executor


def test_observation_anchors_on_the_provider_host_and_stable_numeric_id(tmp_path: Path) -> None:
    observer, executor = _observer(
        [
            _ok({"nameWithOwner": "acme/demo", "url": "https://github.com/acme/demo"}),
            _ok({"id": 987654, "full_name": "acme/demo"}),
        ]
    )

    observed = observer.observe(_repo(tmp_path), config_revision=_SHA)

    assert observed.provider is RepositoryProvider.GITHUB
    assert observed.provider_host == "github.com"
    assert observed.repository_id == "987654"
    assert observed.canonical_name == "github.com/acme/demo"
    assert observed.exists is True
    assert observed.observed_at == NOW
    assert observed.config_revision == _SHA
    assert [call["argv"] for call in executor.calls] == [
        ("gh", "repo", "view", "--json", "nameWithOwner,url"),
        ("gh", "api", "--hostname", "github.com", "repos/acme/demo"),
    ]
    # Observation is read-only and must not inherit ambient GitHub state.
    for call in executor.calls:
        assert "GH_TOKEN" not in call["environment"]
        assert "GH_HOST" not in call["environment"]
        assert "switch" not in call["argv"]


def test_observation_reports_a_rename_as_the_same_stable_repository(tmp_path: Path) -> None:
    # The provider answers with a new name for the same numeric id.
    observer, _ = _observer(
        [
            _ok({"nameWithOwner": "acme/demo-renamed", "url": "https://github.com/acme/demo"}),
            _ok({"id": 987654, "full_name": "acme/demo-renamed"}),
        ]
    )

    observed = observer.observe(_repo(tmp_path), config_revision=_SHA)

    assert observed.repository_id == "987654"
    assert observed.canonical_name == "github.com/acme/demo-renamed"


def test_observation_supports_an_enterprise_host(tmp_path: Path) -> None:
    observer, executor = _observer(
        [
            _ok({"nameWithOwner": "acme/demo", "url": "https://github.acme-corp.net/acme/demo"}),
            _ok({"id": 42, "full_name": "acme/demo"}),
        ]
    )

    observed = observer.observe(_repo(tmp_path), config_revision=_SHA)

    assert observed.provider_host == "github.acme-corp.net"
    assert observed.canonical_name == "github.acme-corp.net/acme/demo"
    assert executor.calls[1]["argv"][3] == "github.acme-corp.net"


def test_a_repository_the_provider_does_not_confirm_is_observed_as_absent(
    tmp_path: Path,
) -> None:
    observer, _ = _observer(
        [
            _ok({"nameWithOwner": "acme/demo", "url": "https://github.com/acme/demo"}),
            RepoForgeError("404", code=ErrorCode.NOT_FOUND),
        ]
    )

    observed = observer.observe(_repo(tmp_path), config_revision=_SHA)

    # `exists=False` is a real observation, and the resolver fails closed on it.
    assert observed.exists is False
    assert observed.repository_id == "0"


def test_a_malformed_or_unsafe_provider_answer_fails_closed(tmp_path: Path) -> None:
    cases: list[list[CommandResult | Exception]] = [
        [_ok({"nameWithOwner": "acme/demo"})],
        [_ok({"url": "https://github.com/acme/demo"})],
        [_ok({"nameWithOwner": "no-slash", "url": "https://github.com/acme/demo"})],
        [_ok({"nameWithOwner": "acme/demo", "url": "not-a-url"})],
        [_ok({"nameWithOwner": "acme/demo", "url": "https://GitHub.COM/acme/demo"})],
        [_ok({"nameWithOwner": "a b/demo", "url": "https://github.com/acme/demo"})],
        [CommandResult(("gh",), "/repo", 0, "not json", "")],
        [
            _ok({"nameWithOwner": "acme/demo", "url": "https://github.com/acme/demo"}),
            _ok({"full_name": "acme/demo"}),
        ],
        [
            _ok({"nameWithOwner": "acme/demo", "url": "https://github.com/acme/demo"}),
            _ok({"id": "not-a-number", "full_name": "acme/demo"}),
        ],
    ]
    for results in cases:
        observer, _ = _observer(results)
        with pytest.raises(RepoForgeError) as failure:
            observer.observe(_repo(tmp_path), config_revision=_SHA)
        assert failure.value.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE, results


def test_an_unsafe_config_revision_is_refused_before_any_command_runs(tmp_path: Path) -> None:
    observer, executor = _observer([])

    for revision in ("", "not-a-sha", _SHA.upper()):
        with pytest.raises(RepoForgeError) as failure:
            observer.observe(_repo(tmp_path), config_revision=revision)
        assert failure.value.code is ErrorCode.CONFIG_INVALID
    assert executor.calls == []
