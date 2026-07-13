from __future__ import annotations

import argparse
import importlib
import io
from dataclasses import replace
from pathlib import Path

from repoforge.application.onboarding.coordinator import OnboardingResult
from repoforge.domain.onboarding import (
    OnboardingOptions,
    OnboardingSession,
    OnboardingStatus,
    summarize_session,
)
from repoforge.interfaces.cli.onboarding import TerminalOperatorIO

cli = importlib.import_module("repoforge.interfaces.cli.main")


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeCoordinator:
    def __init__(self, session):
        self.session = session

    def run(self, command):
        return OnboardingResult(self.session, None, summarize_session(self.session), None, {})

    def status(self, session_id):
        return OnboardingResult(self.session, None, summarize_session(self.session), None)

    def cancel(self, session_id):
        self.session = replace(self.session, status=OnboardingStatus.CANCELLED)
        return OnboardingResult(self.session, None, summarize_session(self.session), None)


def make_session(status=OnboardingStatus.AWAITING_APPROVAL):
    return replace(
        OnboardingSession.new(
            session_id="a" * 24,
            created_at="now",
            config_path="/tmp/c",
            roots=("/repos",),
            options=OnboardingOptions(),
        ),
        status=status,
    )


def test_parser_exposes_onboard_and_repo_discover() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["onboard", "/repos", "--non-interactive"]).command == "onboard"
    args = parser.parse_args(["repo", "discover", "/repos"])
    assert args.repo_command == "discover"


def test_noninteractive_onboard_returns_three_when_approval_missing(monkeypatch, capsys) -> None:
    fake = FakeCoordinator(make_session())
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_coordinator", lambda path: fake
    )
    assert (
        cli.main(
            ["--config", "/tmp/c", "onboard", "/repos", "--non-interactive", "--tunnel-id", "t"]
        )
        == 3
    )
    assert "awaiting_approval" in capsys.readouterr().out


def test_status_and_cancel_commands(monkeypatch, capsys) -> None:
    fake = FakeCoordinator(make_session(OnboardingStatus.PAUSED))
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_coordinator", lambda path: fake
    )
    assert cli.main(["--config", "/tmp/c", "onboard", "status", "a" * 24, "--non-interactive"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", "/tmp/c", "onboard", "cancel", "a" * 24, "--non-interactive"]) == 0
    assert "cancelled" in capsys.readouterr().out


def test_terminal_operator_io_choices_and_confirmation() -> None:
    stdin = TTY("2\ny\n")
    stdout = TTY()
    stderr = TTY()
    io_adapter = TerminalOperatorIO(stdin, stdout, stderr)
    assert io_adapter.interactive
    assert io_adapter.choose(prompt="pick", choices=("one", "two")) == "two"
    assert io_adapter.confirm(prompt="apply") is True


def test_interactive_duplicate_ids_are_collected(monkeypatch) -> None:
    from repoforge.application.onboarding.discover import DiscoveryResult
    from repoforge.domain.onboarding import DiscoveryCandidate, DiscoveryIdentity
    from repoforge.interfaces.cli.onboarding import _resolve_interactive_duplicate_ids

    first = DiscoveryCandidate(
        DiscoveryIdentity("/one/api", "/one/api", "/one/api/.git", True, False), "api"
    )
    second = DiscoveryCandidate(
        DiscoveryIdentity("/two/api", "/two/api", "/two/api/.git", True, False), "api"
    )
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding._discover_result",
        lambda args, roots: DiscoveryResult(
            (first, second), (), (("api", ("/one/api", "/two/api")),)
        ),
    )
    args = argparse.Namespace(repo_id=[])
    io_adapter = TerminalOperatorIO(TTY("client-api\nlegacy-api\n"), TTY(), TTY())
    _resolve_interactive_duplicate_ids(args, (Path("/repos"),), io_adapter)
    assert args.repo_id == ["/one/api=client-api", "/two/api=legacy-api"]
