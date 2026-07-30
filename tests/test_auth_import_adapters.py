"""Isolated discovery of already-configured GitHub accounts and SSH aliases.

Discovery is strictly read-only. It never runs `gh auth switch`, never writes Git or SSH
configuration, and never lets a token reach a candidate, an error, a repr, or a payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.git.ambient_auth import GitAmbientAuthConflictReader
from repoforge.adapters.git.ssh_alias_discovery import SshCommandAliasDiscovery
from repoforge.adapters.github.account_discovery import GhCliNamedAccountDiscovery
from repoforge.domain.auth_migration import NamedAccountCandidate, SshAliasCandidate
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.repository_auth_broker import EphemeralSecret
from repoforge.ports.command import CommandResult

_TOKEN = "gho_named_account_token_canary_4242"
_OTHER_TOKEN = "gho_other_account_token_canary_9999"

_AUTH_STATUS = """github.com
  ✓ Logged in to github.com account personal-user (keyring)
  - Active account: false
  - Git operations protocol: https
  - Token: gho_************
  - Token scopes: 'gist', 'read:org', 'repo'

  ✓ Logged in to github.com account work-user (keyring)
  - Active account: true
  - Git operations protocol: ssh
  - Token: gho_************
  - Token scopes: 'read:org', 'repo'
"""


class RecordingExecutor:
    """Scripted isolated executor that records every argv, environment, and secret set."""

    def __init__(self, results: list[CommandResult | Exception]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "HOME": "/home/demo",
            "PATH": "/safe/bin",
            # Ambient state discovery must never inherit or observe.
            "GH_HOST": "wrong.example",
            "GH_TOKEN": _OTHER_TOKEN,
            "SSH_AUTH_SOCK": "/tmp/wrong-agent",
            **dict(extra or {}),
        }

    def _next(self, mode: str, argv: list[str], kwargs: dict[str, Any]) -> CommandResult:
        self.calls.append({"mode": mode, "argv": tuple(argv), **kwargs})
        if not self.results:
            raise AssertionError(f"unhandled {mode} command: {argv}")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
        return self._next("isolated", argv, kwargs)

    def run_secret_text(self, argv: list[str], **kwargs: Any) -> EphemeralSecret:
        return EphemeralSecret.from_text(self._next("secret_text", argv, kwargs).stdout.strip())


def _ok(stdout: str) -> CommandResult:
    return CommandResult(("gh",), "/repo", 0, stdout, "")


def _discovery(results: list[CommandResult | Exception]) -> tuple[Any, RecordingExecutor]:
    executor = RecordingExecutor(results)
    return GhCliNamedAccountDiscovery(executor, cwd=Path("/repo")), executor


# ---------------------------------------------------------------------------
# GitHub named accounts
# ---------------------------------------------------------------------------


def test_named_accounts_are_enumerated_without_switching_the_active_account() -> None:
    discovery, executor = _discovery([_ok(_AUTH_STATUS)])

    candidates = discovery.candidates(host="github.com")

    assert candidates == (
        NamedAccountCandidate(
            host="github.com",
            login="personal-user",
            active=False,
            token_scopes=("gist", "read:org", "repo"),
        ),
        NamedAccountCandidate(
            host="github.com",
            login="work-user",
            active=True,
            token_scopes=("read:org", "repo"),
        ),
    )
    assert [call["argv"] for call in executor.calls] == [
        ("gh", "auth", "status", "--hostname", "github.com")
    ]
    for call in executor.calls:
        assert "switch" not in call["argv"]
        assert "GH_TOKEN" not in call["environment"]
        assert "GH_HOST" not in call["environment"]


def test_verifying_a_named_account_uses_only_that_account_token() -> None:
    discovery, executor = _discovery(
        [
            _ok(_AUTH_STATUS),
            _ok(_TOKEN),
            _ok(json.dumps({"id": 4242, "login": "personal-user"})),
        ]
    )

    verified = discovery.verify(host="github.com", login="personal-user")

    assert verified.login == "personal-user"
    assert verified.actor_id == "4242"
    assert verified.active is False
    token_call, probe_call = executor.calls[1], executor.calls[2]
    assert token_call["argv"] == (
        "gh",
        "auth",
        "token",
        "--hostname",
        "github.com",
        "--user",
        "personal-user",
    )
    assert probe_call["argv"] == ("gh", "api", "--hostname", "github.com", "user")
    # The probe runs under the named account's own token, not the globally active one.
    assert probe_call["environment"]["GH_TOKEN"] == _TOKEN
    assert probe_call["secrets"] == (_TOKEN,)


def test_verifying_never_leaks_a_token_into_candidates_or_errors() -> None:
    discovery, _ = _discovery(
        [
            _ok(_AUTH_STATUS),
            _ok(_TOKEN),
            _ok(json.dumps({"id": 4242, "login": "personal-user"})),
        ]
    )

    verified = discovery.verify(host="github.com", login="personal-user")

    rendered = json.dumps(verified.payload(), sort_keys=True) + repr(verified)
    assert _TOKEN not in rendered
    assert _OTHER_TOKEN not in rendered

    failing, _ = _discovery([_ok(_AUTH_STATUS), _ok(_TOKEN), _ok("not json at all")])
    with pytest.raises(RepoForgeError) as failure:
        failing.verify(host="github.com", login="personal-user")
    assert failure.value.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE
    assert _TOKEN not in str(failure.value)


def test_named_account_verification_rejects_actor_and_login_drift() -> None:
    # The named login resolves, but the live actor behind the token is someone else.
    discovery, _ = _discovery(
        [
            _ok(_AUTH_STATUS),
            _ok(_TOKEN),
            _ok(json.dumps({"id": 4242, "login": "someone-else"})),
        ]
    )

    with pytest.raises(RepoForgeError) as failure:
        discovery.verify(host="github.com", login="personal-user")

    assert failure.value.code is ErrorCode.GITHUB_API_ACTOR_MISMATCH


def test_missing_and_duplicate_named_accounts_fail_closed() -> None:
    missing, _ = _discovery([_ok(_AUTH_STATUS)])
    with pytest.raises(RepoForgeError) as absent:
        missing.verify(host="github.com", login="absent-user")
    assert absent.value.code is ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND

    duplicated = _AUTH_STATUS.replace("work-user", "personal-user")
    ambiguous, _ = _discovery([_ok(duplicated)])
    with pytest.raises(RepoForgeError) as duplicate:
        ambiguous.candidates(host="github.com")
    assert duplicate.value.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE


def test_named_account_discovery_rejects_malformed_and_oversized_status_output() -> None:
    for stdout in (
        "",
        "not a gh auth status listing",
        "  ✓ Logged in to github.com account bad login (keyring)\n",
        "  ✓ Logged in to github.com account ok-user (keyring)\n" * 200,
    ):
        discovery, _ = _discovery([_ok(stdout)])
        with pytest.raises(RepoForgeError) as failure:
            discovery.candidates(host="github.com")
        assert failure.value.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE


def test_named_account_discovery_reports_a_provider_failure_as_unavailable() -> None:
    discovery, _ = _discovery([RepoForgeError("gh timed out", code=ErrorCode.COMMAND_TIMEOUT)])

    with pytest.raises(RepoForgeError) as failure:
        discovery.candidates(host="github.com")

    assert failure.value.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE
    assert failure.value.retryable is True


# ---------------------------------------------------------------------------
# SSH aliases
# ---------------------------------------------------------------------------


def _ssh_output(**overrides: str) -> str:
    values = {
        "host": "github.com",
        "user": "git",
        "identityfile": "/home/demo/.ssh/id_ed25519_work",
        "port": "22",
    }
    values.update(overrides)
    return "".join(f"{key} {value}\n" for key, value in values.items())


def _ssh(results: list[CommandResult | Exception]) -> tuple[SshCommandAliasDiscovery, Any]:
    executor = RecordingExecutor(results)
    return SshCommandAliasDiscovery(executor, cwd=Path("/repo")), executor


def test_ssh_alias_discovery_returns_one_concrete_pinned_candidate() -> None:
    discovery, executor = _ssh([_ok(_ssh_output())])

    candidate = discovery.inspect("github-work")

    assert candidate == SshAliasCandidate(
        alias="github-work",
        hostname="github.com",
        user="git",
        identity_file="/home/demo/.ssh/id_ed25519_work",
    )
    assert [call["argv"] for call in executor.calls] == [("ssh", "-G", "github-work")]
    # Discovery is read-only: there is no write path for SSH configuration at all.
    assert not hasattr(discovery, "write")
    assert not hasattr(discovery, "configure")


def test_ssh_alias_discovery_rejects_unsafe_or_ambiguous_configuration() -> None:
    cases = (
        _ssh_output(host="*"),
        _ssh_output(host="GitHub.COM"),
        _ssh_output(identityfile="relative/id_ed25519"),
        _ssh_output(
            identityfile="/home/demo/.ssh/id_ed25519_work\nidentityfile /home/demo/.ssh/other"
        ),
        _ssh_output(identityfile="~/.ssh/%r_id"),
        _ssh_output(identityfile="/home/demo/.ssh/${USER}_id"),
        _ssh_output() + "proxycommand /usr/bin/nc %h %p\n",
        _ssh_output() + "proxyjump bastion.example\n",
        _ssh_output() + "identityagent /tmp/agent.sock\n",
        _ssh_output(host="github.com\x00evil"),
        "host github.com\n",
        _ssh_output().replace("identityfile /home/demo/.ssh/id_ed25519_work\n", ""),
        "host x\n" * 5_000,
    )
    for stdout in cases:
        discovery, _ = _ssh([_ok(stdout)])
        with pytest.raises(RepoForgeError) as failure:
            discovery.inspect("github-work")
        assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH, stdout


def test_ssh_alias_discovery_rejects_unsafe_alias_names() -> None:
    for alias in ("", "-oProxyCommand=x", "a b", "x" * 300, "alias\x00"):
        discovery, executor = _ssh([])
        with pytest.raises(RepoForgeError) as failure:
            discovery.inspect(alias)
        assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH
        assert executor.calls == []


def test_ssh_alias_candidate_accepts_a_missing_user_but_not_a_local_transport() -> None:
    discovery, _ = _ssh([_ok(_ssh_output().replace("user git\n", ""))])

    candidate = discovery.inspect("github-work")

    assert candidate.user is None

    for stdout in (_ssh_output(host="localhost"), _ssh_output(host="127.0.0.1")):
        local, _ = _ssh([_ok(stdout)])
        with pytest.raises(RepoForgeError):
            local.inspect("github-work")


# ---------------------------------------------------------------------------
# Ambient Git and environment state
# ---------------------------------------------------------------------------


def test_ambient_reader_returns_scoped_git_config_values_without_mutating_anything() -> None:
    stdout = "file:/home/demo/.gitconfig\tosxkeychain\nfile:/repos/demo/.git/config\tstore\n"
    executor = RecordingExecutor([_ok(stdout)])
    reader = GitAmbientAuthConflictReader(executor, environ={"GH_TOKEN": _TOKEN, "PATH": "/bin"})

    values = reader.git_config_values(Path("/repos/demo"), "credential.helper")

    assert values == (
        ("file:/home/demo/.gitconfig", "osxkeychain"),
        ("file:/repos/demo/.git/config", "store"),
    )
    assert executor.calls[0]["argv"] == (
        "git",
        "config",
        "--show-origin",
        "--get-all",
        "credential.helper",
    )
    # Read-only: no scope flag that could write, and failures are absorbed as "not set".
    assert "--global" not in executor.calls[0]["argv"]
    assert "--system" not in executor.calls[0]["argv"]
    assert "--replace-all" not in executor.calls[0]["argv"]


def test_ambient_reader_reports_environment_names_but_never_values() -> None:
    reader = GitAmbientAuthConflictReader(
        RecordingExecutor([]), environ={"GH_TOKEN": _TOKEN, "PATH": "/bin"}
    )

    names = reader.environment_names()

    assert "GH_TOKEN" in names and "PATH" in names
    assert _TOKEN not in json.dumps(list(names))


def test_ambient_reader_treats_an_unset_key_and_unsafe_output_as_nothing_configured() -> None:
    unset = GitAmbientAuthConflictReader(
        RecordingExecutor([RepoForgeError("exit 1", code=ErrorCode.COMMAND_FAILED)]),
        environ={},
    )
    assert unset.git_config_values(Path("/repos/demo"), "user.email") == ()

    # A line without the origin separator cannot be attributed to a scope, so it is dropped.
    malformed = GitAmbientAuthConflictReader(
        RecordingExecutor([_ok("no-separator-here\nfile:/home/demo/.gitconfig\tok@example.com\n")]),
        environ={},
    )
    assert malformed.git_config_values(Path("/repos/demo"), "user.email") == (
        ("file:/home/demo/.gitconfig", "ok@example.com"),
    )


def test_ambient_reader_rejects_an_unsafe_config_key() -> None:
    reader = GitAmbientAuthConflictReader(RecordingExecutor([]), environ={})

    for key in ("", "--global", "user email", "x" * 300, "user.email\x00"):
        with pytest.raises(RepoForgeError) as failure:
            reader.git_config_values(Path("/repos/demo"), key)
        assert failure.value.code is ErrorCode.SECURITY_POLICY_VIOLATION
