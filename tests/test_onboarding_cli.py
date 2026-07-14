from __future__ import annotations

import argparse
import importlib
import io
import json
from dataclasses import replace
from pathlib import Path

from repoforge.application.onboarding.coordinator import OnboardingResult
from repoforge.application.onboarding.discover import DiscoveryResult
from repoforge.domain.onboarding import (
    DiscoveryCandidate,
    DiscoveryIdentity,
    OnboardingBatchPlan,
    OnboardingOptions,
    OnboardingRepositoryState,
    OnboardingSession,
    OnboardingStatus,
    RepositoryProgress,
    summarize_session,
)
from repoforge.interfaces.cli.onboarding import (
    TerminalOperatorIO,
    _ensure_interactive_tunnel_id,
    _run_interactive,
)

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
    parsed = parser.parse_args(
        ["onboard", "/repos", "--ui", "rich", "--defaults", "safe"]
    )
    assert parsed.command == "onboard"
    assert parsed.ui == "rich"
    assert parsed.defaults == "safe"
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


def test_noninteractive_does_not_construct_optional_ui(monkeypatch, capsys) -> None:
    fake = FakeCoordinator(make_session())
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_coordinator", lambda path: fake
    )

    def fail_ui(*args, **kwargs):
        raise AssertionError("interactive UI must not be constructed")

    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_ui", fail_ui
    )
    assert (
        cli.main(
            [
                "--config",
                "/tmp/c",
                "onboard",
                "/repos",
                "--non-interactive",
                "--tunnel-id",
                "t",
            ]
        )
        == 3
    )
    assert "awaiting_approval" in capsys.readouterr().out


def test_noninteractive_rejects_interactive_default_inference(monkeypatch, capsys) -> None:
    fake = FakeCoordinator(make_session())
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_coordinator", lambda path: fake
    )
    assert (
        cli.main(
            [
                "--config",
                "/tmp/c",
                "onboard",
                "/repos",
                "--non-interactive",
                "--defaults",
                "ask",
                "--tunnel-id",
                "t",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "interactive-only" in output.out or "interactive-only" in output.err


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


def test_tunnel_prompt_uses_accepted_generation_not_config_file_presence(
    monkeypatch, tmp_path
) -> None:
    class EmptyConfigurationStore:
        def current(self):
            return None

    config = tmp_path / "config.toml"
    config.write_text("# placeholder, not an accepted generation\n", encoding="utf-8")
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_configuration_store",
        lambda path, state_root: EmptyConfigurationStore(),
    )
    args = argparse.Namespace(config=str(config), tunnel_id=None)
    stderr = TTY()
    io_adapter = TerminalOperatorIO(TTY("tunnel_123\n"), TTY(), stderr)
    _ensure_interactive_tunnel_id(
        args, session_id=None, coordinator=FakeCoordinator(make_session()), io=io_adapter
    )
    assert args.tunnel_id == "tunnel_123"
    assert "Initial tunnel setup" in stderr.getvalue()


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


class BatchReviewUI:
    interactive = True
    backend_name = "fake"

    def __init__(self, *, confirm_apply: bool = True):
        self.stages = []
        self.confirm_apply = confirm_apply
        self.confirm_calls = 0
        self.json_events = []

    def show_json(self, event):
        self.json_events.append(event)

    def stage(self, *, index, total, title):
        self.stages.append((index, total, title))

    def panel(self, *, title, lines):
        pass

    def table(self, *, title, headers, rows):
        pass

    def code(self, *, title, text, lexer="text"):
        pass

    def choose(self, *, prompt, choices, default=None):
        values = tuple(getattr(choice, "value", choice) for choice in choices)
        if "base branch" in prompt:
            assert "main" in values
            return "main"
        raise AssertionError(f"unexpected choice prompt: {prompt}")

    def select_many(self, *, prompt, choices):
        values = tuple(choice.value for choice in choices)
        if values == ("demo.dependency_install",):
            return values
        if values == ("demo",):
            return values
        raise AssertionError(f"unexpected multi-select: {prompt} {values}")

    def ask(self, *, prompt, secret=False, default=None):
        raise AssertionError(f"unexpected text prompt: {prompt}")

    def confirm(self, *, prompt, default=False):
        assert prompt == "Apply this reviewed batch?"
        assert default is False
        self.confirm_calls += 1
        return self.confirm_apply


class BatchReviewCoordinator:
    def __init__(self, *, config_path: str):
        identity = DiscoveryIdentity(
            "/repos/demo", "/repos/demo", "/repos/demo/.git", True, False
        )
        self.candidate = DiscoveryCandidate(identity, "demo")
        self.calls = []
        self.config_path = config_path
        self.proposal_json = json.dumps(
            {
                "path": "/repos/demo",
                "confidence": "high",
                "policy": {
                    "mode": "standard",
                    "remote": "origin",
                    "default_base": "main",
                    "publish_enabled": False,
                    "profiles": [{"name": "full"}],
                    "max_changed_files": 20,
                    "max_diff_lines": 1000,
                    "max_total_changed_bytes": 100000,
                },
                "findings": [],
            }
        )

    def _session(self, status, state):
        return replace(
            OnboardingSession.new(
                session_id="a" * 24,
                created_at="now",
                config_path=self.config_path,
                roots=("/repos",),
                options=OnboardingOptions(tunnel_id="tunnel_123"),
            ),
            status=status,
            repositories=(state,),
        )

    def run(self, command):
        self.calls.append(command)
        index = len(self.calls)
        if index <= 2:
            state = OnboardingRepositoryState(
                self.candidate,
                progress=RepositoryProgress.NEEDS_DECISION,
                proposal_id="proposal-1",
                required_decisions=(
                    (
                        "dependency_install",
                        "Dependency setup may access the network.",
                        ("include_non_verification", "exclude", "block"),
                    ),
                    (
                        "default_base",
                        "Choose the allowlisted base branch.",
                        ("main", "read_only"),
                    ),
                ),
                proposal_json=self.proposal_json,
            )
            session = self._session(OnboardingStatus.AWAITING_DECISIONS, state)
            return OnboardingResult(
                session, None, summarize_session(session), None, {"warnings": []}
            )
        if index == 3:
            state = OnboardingRepositoryState(
                self.candidate,
                progress=RepositoryProgress.NEEDS_APPROVAL,
                proposal_id="proposal-1",
                proposal_json=self.proposal_json,
            )
            session = self._session(OnboardingStatus.AWAITING_APPROVAL, state)
            return OnboardingResult(session, None, summarize_session(session), None, None)
        plan = OnboardingBatchPlan(
            'version = 2\n\n[tunnel]\nid = "tunnel_123"\n',
            "resolved",
            ("demo",),
            ("proposal-1",),
            "combined",
            ("approval-hash",),
            (("demo", "fingerprint"),),
            "expansion",
        )
        if index == 4:
            state = OnboardingRepositoryState(
                self.candidate,
                progress=RepositoryProgress.APPROVED,
                proposal_id="proposal-1",
                approval_sha256="approval-hash",
                proposal_json=self.proposal_json,
            )
            session = self._session(OnboardingStatus.READY, state)
            return OnboardingResult(session, plan, summarize_session(session), None, None)
        state = OnboardingRepositoryState(
            self.candidate,
            progress=RepositoryProgress.ENROLLED,
            proposal_id="proposal-1",
            approval_sha256="approval-hash",
            proposal_json=self.proposal_json,
        )
        session = replace(
            self._session(OnboardingStatus.COMPLETED, state),
            accepted_generation=1,
            active_generation=1,
        )
        return OnboardingResult(
            session, plan, summarize_session(session), {"active_generation": 1}, None
        )


def _batch_args(config: Path, *, plan_only: bool = False):
    return argparse.Namespace(
        config=str(config),
        ui="auto",
        defaults="ask",
        max_depth=8,
        include=[],
        exclude=[],
        template="standard",
        activate="auto",
        plan_only=plan_only,
        decision=[],
        policy_override=[],
        approve=[],
        repo_id=[],
        tunnel_id="tunnel_123",
        profile="repoforge",
        wait=True,
        rollback_on_failure=True,
    )


def test_interactive_batch_review_runs_all_six_stages(monkeypatch, tmp_path) -> None:
    config = tmp_path / "config.toml"
    ui = BatchReviewUI()
    coordinator = BatchReviewCoordinator(config_path=str(config.resolve()))
    discovered = DiscoveryResult((coordinator.candidate,), (), ())
    rendered = []
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_ui",
        lambda *args, **kwargs: ui,
    )
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_coordinator",
        lambda path: coordinator,
    )
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding._ensure_interactive_tunnel_id",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding._discover_result",
        lambda args, roots: discovered,
    )

    assert _run_interactive(
        _batch_args(config), None, (Path("/repos"),), rendered.append
    ) == 0
    assert [title for _index, _total, title in ui.stages] == [
        "Discovery",
        "Safe defaults",
        "Ambiguous decisions",
        "Repository summaries",
        "Config diff",
        "Apply",
    ]
    assert dict(coordinator.calls[1].decisions)["demo.dependency_install"] == "exclude"
    assert dict(coordinator.calls[2].decisions)["demo.default_base"] == "main"
    assert coordinator.calls[3].approvals == ("approve:proposal-1",)
    assert coordinator.calls[4].plan_only is False
    assert ui.confirm_calls == 1
    assert rendered[-1]["status"] == "completed"


def test_interactive_plan_only_stops_after_config_diff(monkeypatch, tmp_path) -> None:
    config = tmp_path / "config.toml"
    ui = BatchReviewUI(confirm_apply=False)
    coordinator = BatchReviewCoordinator(config_path=str(config.resolve()))
    discovered = DiscoveryResult((coordinator.candidate,), (), ())
    rendered = []
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_ui",
        lambda *args, **kwargs: ui,
    )
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding.build_onboarding_coordinator",
        lambda path: coordinator,
    )
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding._ensure_interactive_tunnel_id",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "repoforge.interfaces.cli.onboarding._discover_result",
        lambda args, roots: discovered,
    )

    assert _run_interactive(
        _batch_args(config, plan_only=True),
        None,
        (Path("/repos"),),
        rendered.append,
    ) == 0
    assert len(coordinator.calls) == 4
    assert ui.confirm_calls == 0
    assert rendered[-1]["status"] == "ready"
