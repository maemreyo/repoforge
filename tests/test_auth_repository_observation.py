"""Observing the stable identity of a configured repository, read-only.

Identity resolution is anchored to the provider host plus the stable numeric repository ID, so
the observer must read both from the provider rather than trusting a configured name. A rename
must be observable as the same repository; a missing or ambiguous answer must fail closed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.github.repository_observation import GhCliRepositoryObserver
from repoforge.config import RepositoryConfig
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_remote_identity import (
    ReviewedSshEndpoint,
    SshAliasDefinition,
    SshKeyProof,
    SshPrincipalProof,
)
from repoforge.domain.repository_auth_broker import ProcessAuthContext
from repoforge.domain.repository_identity import AuthTargetKind, RepositoryProvider
from repoforge.ports.auth_inspection import RepositoryObservationTarget
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


def _remote(value: str) -> CommandResult:
    return CommandResult(("git",), "/repo", 0, value + "\n", "")


def _repo(tmp_path: Path) -> RepositoryConfig:
    root = tmp_path / "demo"
    root.mkdir(parents=True, exist_ok=True)
    return RepositoryConfig(repo_id="demo", path=root)


def _observer(results: list[CommandResult | Exception]) -> tuple[GhCliRepositoryObserver, Any]:
    executor = RecordingExecutor(results)
    return GhCliRepositoryObserver(executor, clock=FixedClock(NOW)), executor


def _selected_context(token: str = "gho_selected_profile_canary_888") -> ProcessAuthContext:
    return ProcessAuthContext(
        profile_id="personal",
        material_id="material-selected",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="987654",
        environment=(
            ("GH_TOKEN", token),
            ("PATH", "/safe/bin"),
            ("GH_PROMPT_DISABLED", "1"),
            ("GIT_TERMINAL_PROMPT", "0"),
        ),
        _secret_values=(token,),
    )


def _reviewed_endpoint(
    raw_url: str = "git@github-work:acme/demo.git",
) -> ReviewedSshEndpoint:
    fingerprint = "SHA256:" + "A" * 43
    alias = SshAliasDefinition(
        alias="github-work",
        canonical_host="github.com",
        user="git",
        port=22,
        identity_file="/home/demo/.ssh/id_rsa_work",
        source_config_digest="b" * 64,
        selected_block_digest="c" * 64,
    )
    key = SshKeyProof(
        canonical_path=alias.identity_file,
        public_key_fingerprint=fingerprint,
        owner_uid=501,
        mode=0o600,
        observed_at=NOW,
    )
    principal = SshPrincipalProof(
        provider_host="github.com",
        principal_kind="github_account",
        principal_login="personal-user",
        expected_actor_id="987654",
        key_fingerprint=fingerprint,
        observed_at=NOW,
        proof_digest="d" * 64,
    )
    return ReviewedSshEndpoint(
        schema_version=1,
        raw_host="github-work",
        canonical_host="github.com",
        user="git",
        port=22,
        owner="acme",
        repository="demo",
        raw_url_digest=hashlib.sha256(raw_url.encode("utf-8")).hexdigest(),
        alias=alias,
        key=key,
        principal=principal,
        proof_digest="e" * 64,
    )


def test_repository_observation_target_has_deterministic_canonical_name() -> None:
    target = RepositoryObservationTarget(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        owner="acme",
        repository="demo",
    )

    assert target.canonical_name == "github.com/acme/demo"


def test_selected_context_not_globally_active_account_observes_private_repository(
    tmp_path: Path,
) -> None:
    selected_token = "gho_selected_profile_canary_888"

    class AccountSensitiveExecutor(RecordingExecutor):
        def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
            self.calls.append({"argv": tuple(argv), **kwargs})
            if argv[:3] == ["git", "config", "--local"]:
                return CommandResult(
                    tuple(argv), str(kwargs["cwd"]), 0, "git@github.com:acme/demo.git\n", ""
                )
            environment = kwargs["environment"]
            observed_id = 987654 if environment.get("GH_TOKEN") == selected_token else 111111
            return CommandResult(
                tuple(argv),
                str(kwargs["cwd"]),
                0,
                json.dumps({"id": observed_id, "full_name": "acme/demo"}),
                "",
            )

    executor = AccountSensitiveExecutor([])
    observer = GhCliRepositoryObserver(executor, clock=FixedClock(NOW))

    observed = observer.observe(
        _repo(tmp_path),
        expected_provider_host="github.com",
        config_revision=_SHA,
        context=_selected_context(selected_token),
    )

    assert observed.repository_id == "987654"
    assert [call["argv"] for call in executor.calls] == [
        ("git", "config", "--local", "--get", "remote.origin.url"),
        ("gh", "api", "--hostname", "github.com", "repos/acme/demo"),
    ]
    for call in executor.calls:
        environment = call["environment"]
        assert environment.get("HOME") != "/home/demo"
        assert environment.get("GH_CONFIG_DIR") != "/home/demo/.config/gh"
        assert "GH_HOST" not in environment
        assert "GITHUB_TOKEN" not in environment
        assert "SSH_AUTH_SOCK" not in environment
        assert "switch" not in call["argv"]
    assert executor.calls[1]["environment"]["GH_TOKEN"] == selected_token
    assert selected_token in executor.calls[1]["secrets"]


def test_repository_observation_keeps_synthetic_home_outside_the_repository(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    class HomeCreatingExecutor(RecordingExecutor):
        def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
            environment = kwargs["environment"]
            Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
            Path(environment["GH_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
            return super().run_isolated(argv, **kwargs)

    executor = HomeCreatingExecutor(
        [
            _remote("https://github.com/acme/demo.git"),
            _ok({"id": 987654, "full_name": "acme/demo"}),
        ]
    )
    observer = GhCliRepositoryObserver(executor, clock=FixedClock(NOW))

    observer.observe(
        repo,
        expected_provider_host="github.com",
        config_revision=_SHA,
        context=_selected_context(),
    )

    assert not (repo.path / ".repoforge-empty-home").exists()
    assert not (repo.path / ".repoforge-empty-gh-config").exists()


def test_observation_anchors_on_the_provider_host_and_stable_numeric_id(tmp_path: Path) -> None:
    observer, executor = _observer(
        [
            _remote("https://github.com/acme/demo.git"),
            _ok({"id": 987654, "full_name": "acme/demo"}),
        ]
    )

    observed = observer.observe(
        _repo(tmp_path),
        expected_provider_host="github.com",
        config_revision=_SHA,
        context=_selected_context(),
    )

    assert observed.provider is RepositoryProvider.GITHUB
    assert observed.provider_host == "github.com"
    assert observed.repository_id == "987654"
    assert observed.canonical_name == "github.com/acme/demo"
    assert observed.exists is True
    assert observed.observed_at == NOW
    assert observed.config_revision == _SHA
    assert [call["argv"] for call in executor.calls] == [
        ("git", "config", "--local", "--get", "remote.origin.url"),
        ("gh", "api", "--hostname", "github.com", "repos/acme/demo"),
    ]
    assert "GH_TOKEN" not in executor.calls[0]["environment"]
    assert executor.calls[1]["environment"]["GH_TOKEN"].startswith("gho_selected")
    for call in executor.calls:
        assert "GH_HOST" not in call["environment"]
        assert "SSH_AUTH_SOCK" not in call["environment"]
        assert call["environment"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "switch" not in call["argv"]


def test_observation_reports_a_rename_as_the_same_stable_repository(tmp_path: Path) -> None:
    # The provider answers with a new name for the same numeric id.
    observer, _ = _observer(
        [
            _remote("git@github.com:acme/demo.git"),
            _ok({"id": 987654, "full_name": "acme/demo-renamed"}),
        ]
    )

    observed = observer.observe(
        _repo(tmp_path),
        expected_provider_host="github.com",
        config_revision=_SHA,
        context=_selected_context(),
    )

    assert observed.repository_id == "987654"
    assert observed.canonical_name == "github.com/acme/demo-renamed"


def test_observation_supports_an_enterprise_host(tmp_path: Path) -> None:
    observer, executor = _observer(
        [
            _remote("ssh://git@github.acme-corp.net/acme/demo.git"),
            _ok({"id": 42, "full_name": "acme/demo"}),
        ]
    )

    observed = observer.observe(
        _repo(tmp_path),
        expected_provider_host="github.acme-corp.net",
        config_revision=_SHA,
        context=_selected_context(),
    )

    assert observed.provider_host == "github.acme-corp.net"
    assert observed.canonical_name == "github.acme-corp.net/acme/demo"
    assert executor.calls[1]["argv"][3] == "github.acme-corp.net"


def test_observer_accepts_an_alias_only_with_a_matching_reviewed_endpoint(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor(
        [
            _remote("git@github-work:acme/demo.git"),
            _ok({"id": 987654, "full_name": "acme/demo"}),
        ]
    )
    observer = GhCliRepositoryObserver(executor, clock=FixedClock(NOW))

    observed = observer.observe(
        _repo(tmp_path),
        expected_provider_host="github.com",
        config_revision=_SHA,
        context=_selected_context(),
        reviewed_ssh_endpoint=_reviewed_endpoint(),
    )

    assert observed.provider_host == "github.com"
    assert observed.canonical_name == "github.com/acme/demo"
    assert executor.calls[1]["argv"][3] == "github.com"
    assert all(call["argv"][0] != "ssh" for call in executor.calls)


def test_observer_fails_closed_when_an_alias_has_no_reviewed_endpoint(tmp_path: Path) -> None:
    executor = RecordingExecutor([_remote("git@github-work:acme/demo.git")])
    observer = GhCliRepositoryObserver(executor, clock=FixedClock(NOW))

    with pytest.raises(RepoForgeError) as failure:
        observer.target(_repo(tmp_path), expected_provider_host="github.com")

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH


def test_a_repository_the_provider_does_not_confirm_is_observed_as_absent(
    tmp_path: Path,
) -> None:
    observer, _ = _observer(
        [
            _remote("https://github.com/acme/demo"),
            RepoForgeError("404", code=ErrorCode.NOT_FOUND),
        ]
    )

    observed = observer.observe(
        _repo(tmp_path),
        expected_provider_host="github.com",
        config_revision=_SHA,
        context=_selected_context(),
    )

    # `exists=False` is a real observation, and the resolver fails closed on it.
    assert observed.exists is False
    assert observed.repository_id == "0"


def test_a_malformed_or_unsafe_provider_answer_fails_closed(tmp_path: Path) -> None:
    cases: list[list[CommandResult | Exception]] = [
        [_remote("")],
        [_remote("not-a-url")],
        [_remote("file:///tmp/demo")],
        [_remote("https://GitHub.COM/acme/demo")],
        [_remote("https://github.com/a b/demo")],
        [
            _remote("https://github.com/acme/demo"),
            CommandResult(("gh",), "/repo", 0, "not json", ""),
        ],
        [_remote("https://github.com/acme/demo"), _ok({"full_name": "acme/demo"})],
        [
            _remote("https://github.com/acme/demo"),
            _ok({"id": "not-a-number", "full_name": "acme/demo"}),
        ],
        [
            _remote("https://github.com/acme/demo"),
            _ok({"id": 123, "full_name": "no-slash"}),
        ],
    ]
    for results in cases:
        observer, _ = _observer(results)
        with pytest.raises(RepoForgeError) as failure:
            observer.observe(
                _repo(tmp_path),
                expected_provider_host="github.com",
                config_revision=_SHA,
                context=_selected_context(),
            )
        assert failure.value.code in {
            ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
            ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
        }, results


def test_an_unsafe_config_revision_is_refused_before_any_command_runs(tmp_path: Path) -> None:
    observer, executor = _observer([])

    for revision in ("", "not-a-sha", _SHA.upper()):
        with pytest.raises(RepoForgeError) as failure:
            observer.observe(
                _repo(tmp_path),
                expected_provider_host="github.com",
                config_revision=revision,
                context=_selected_context(),
            )
        assert failure.value.code is ErrorCode.CONFIG_INVALID
    assert executor.calls == []
