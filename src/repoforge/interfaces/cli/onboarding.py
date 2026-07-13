"""CLI parsing and terminal presentation for guided multi-repository onboarding."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import TextIO

from ...application.configuration.source import parse_source
from ...application.onboarding.coordinator import OnboardingCommand, OnboardingResult
from ...application.onboarding.discover import DiscoveryResult, OnboardingDiscoveryService
from ...application.onboarding.inputs import parse_assignments
from ...bootstrap import (
    build_configuration_store,
    build_onboarding_coordinator,
    build_repository_discovery,
    default_state_root,
)
from ...domain.errors import ConfigError
from ...domain.onboarding import OnboardingOptions, OnboardingStatus, RepositoryProgress
from ...domain.policy import slugify
from ...domain.repository_proposal import EnrollmentMode
from ...ports.repository_discovery import DiscoveryRequest

Render = Callable[[object], None]


def _plain(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def result_payload(result: OnboardingResult) -> dict[str, object]:
    return {
        "status": result.session.status.value,
        "session_id": result.session.session_id,
        "session": _plain(asdict(result.session)),
        "summary": _plain(asdict(result.summary)),
        "plan": _plain(asdict(result.plan)) if result.plan else None,
        "activation": result.activation,
        "preflight": result.preflight,
        "safe_next_action": _next_action(result),
    }


def _next_action(result: OnboardingResult) -> str:
    status = result.session.status
    if status is OnboardingStatus.AWAITING_DECISIONS:
        return (
            f"Resolve repository decisions and run `rf onboard resume {result.session.session_id}`."
        )
    if status is OnboardingStatus.AWAITING_APPROVAL:
        return f"Review each proposal and resume session {result.session.session_id} with exact approvals."
    if status is OnboardingStatus.READY:
        return f"Resume session {result.session.session_id} without --plan-only to apply the reviewed batch."
    if status is OnboardingStatus.COMPLETED:
        return "Run `rf repo list` and `rf runtime status` to verify the active repository set."
    if status is OnboardingStatus.CANCELLED:
        return "Start a new `rf onboard ROOT` session when ready."
    return f"Inspect `rf onboard status {result.session.session_id}`."


class TerminalOperatorIO:
    def __init__(self, stdin: TextIO, stdout: TextIO, stderr: TextIO):
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr

    @property
    def interactive(self) -> bool:
        return self._stdin.isatty() and self._stderr.isatty()

    def show(self, event: dict[str, object]) -> None:
        print(
            json.dumps(_plain(event), indent=2, ensure_ascii=False, default=str), file=self._stdout
        )

    def choose(self, *, prompt: str, choices: tuple[str, ...]) -> str:
        while True:
            print(f"{prompt} [{'/'.join(choices)}]", file=self._stderr, end=": ", flush=True)
            value = self._stdin.readline().strip()
            if value in choices:
                return value
            if value.isdigit() and 1 <= int(value) <= len(choices):
                return choices[int(value) - 1]
            print("Choose one of: " + ", ".join(choices), file=self._stderr)

    def ask(self, *, prompt: str, secret: bool = False) -> str:
        if secret:
            return getpass.getpass(prompt + ": ", stream=self._stderr).strip()
        print(prompt, file=self._stderr, end=": ", flush=True)
        return self._stdin.readline().strip()

    def confirm(self, *, prompt: str, default: bool = False) -> bool:
        suffix = "Y/n" if default else "y/N"
        while True:
            print(f"{prompt} [{suffix}]", file=self._stderr, end=": ", flush=True)
            value = self._stdin.readline().strip().lower()
            if not value:
                return default
            if value in {"y", "yes"}:
                return True
            if value in {"n", "no"}:
                return False
            print("Answer yes or no.", file=self._stderr)


def add_onboarding_parsers(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    repo_sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    onboard = commands.add_parser("onboard", help="Discover, review, and enroll local repositories")
    onboard.add_argument("items", nargs="*", help="Roots, or status/resume/cancel SESSION_ID")
    onboard.add_argument("--resume", dest="resume_session_id")
    onboard.add_argument("--max-depth", type=int, default=8)
    onboard.add_argument("--include", action="append", default=[])
    onboard.add_argument("--exclude", action="append", default=[])
    onboard.add_argument(
        "--template", choices=[item.value for item in EnrollmentMode], default="standard"
    )
    onboard.add_argument("--activate", choices=("auto", "always", "never"), default="auto")
    onboard.add_argument("--plan-only", action="store_true")
    onboard.add_argument("--non-interactive", action="store_true")
    onboard.add_argument("--decision", action="append", default=[])
    onboard.add_argument("--policy-override", action="append", default=[])
    onboard.add_argument("--approve", action="append", default=[])
    onboard.add_argument(
        "--repo-id",
        action="append",
        default=[],
        metavar="PATH=ID",
        help="Resolve duplicate discovered IDs by assigning an explicit ID to a canonical path",
    )
    onboard.add_argument("--tunnel-id", default=os.environ.get("REPOFORGE_TUNNEL_ID"))
    onboard.add_argument(
        "--profile", default=os.environ.get("REPOFORGE_TUNNEL_PROFILE", "repoforge")
    )
    onboard.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    onboard.add_argument(
        "--rollback-on-failure", action=argparse.BooleanOptionalAction, default=True
    )

    discover = repo_sub.add_parser("discover", help="Read-only Git-aware repository discovery")
    discover.add_argument("roots", nargs="+")
    discover.add_argument("--max-depth", type=int, default=8)
    discover.add_argument("--include", action="append", default=[])
    discover.add_argument("--exclude", action="append", default=[])


def _options(args: argparse.Namespace) -> OnboardingOptions:
    return OnboardingOptions(
        max_depth=args.max_depth,
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        template=args.template,
        activate=args.activate,
        wait=args.wait,
        rollback_on_failure=args.rollback_on_failure,
        tunnel_id=args.tunnel_id,
        profile=args.profile,
    )


def _command(
    args: argparse.Namespace,
    *,
    roots: tuple[Path, ...],
    resume_session_id: str | None,
    approvals: tuple[str, ...] | None = None,
    decisions: tuple[tuple[str, str], ...] | None = None,
    templates: tuple[tuple[str, str], ...] = (),
    skips: tuple[str, ...] = (),
    plan_only: bool | None = None,
) -> OnboardingCommand:
    return OnboardingCommand(
        config_path=Path(args.config).expanduser().resolve(),
        roots=roots,
        options=_options(args),
        decisions=decisions
        if decisions is not None
        else tuple(sorted(parse_assignments(args.decision, option="--decision").items())),
        overrides=tuple(
            sorted(parse_assignments(args.policy_override, option="--policy-override").items())
        ),
        approvals=approvals if approvals is not None else tuple(args.approve),
        resume_session_id=resume_session_id,
        plan_only=args.plan_only if plan_only is None else plan_only,
        tunnel_id=args.tunnel_id,
        profile=args.profile,
        templates=templates,
        skips=skips,
        repo_ids=tuple(
            sorted(
                (str(Path(path).expanduser().resolve()), repo_id)
                for path, repo_id in parse_assignments(args.repo_id, option="--repo-id").items()
            )
        ),
    )


def _action(args: argparse.Namespace) -> tuple[str, str | None, tuple[Path, ...]]:
    items = list(args.items)
    if items and items[0] in {"status", "resume", "cancel"}:
        if len(items) != 2:
            raise ValueError(f"rf onboard {items[0]} requires exactly one SESSION_ID")
        return items[0], items[1], ()
    if args.resume_session_id:
        if items:
            raise ValueError("Do not supply roots together with --resume")
        return "resume", str(args.resume_session_id), ()
    if not items:
        raise ValueError("rf onboard requires at least one ROOT or a session action")
    return "run", None, tuple(Path(item).expanduser().resolve() for item in items)


def _discover_result(args: argparse.Namespace, roots: tuple[Path, ...]) -> DiscoveryResult:
    config_path = Path(args.config).expanduser().resolve()
    root = default_state_root()
    store = build_configuration_store(config_path, state_root=root)
    enrolled: tuple[tuple[str, str], ...] = ()
    if store.current() is not None:
        try:
            source = parse_source(store.read_source_text())
            enrolled = tuple((item.repo_id, item.path) for item in source.repositories)
        except ValueError:
            enrolled = ()
    request = DiscoveryRequest(
        roots,
        args.max_depth,
        tuple(args.include),
        tuple(args.exclude),
        (Path.home() / ".local/share/repoforge/workspaces",),
    )
    return OnboardingDiscoveryService(build_repository_discovery(root)).discover(
        request, enrolled=enrolled
    )


def run_repo_discover(args: argparse.Namespace, *, render: Render) -> int:
    roots = tuple(Path(item).expanduser().resolve() for item in args.roots)
    result = _discover_result(args, roots)
    render(
        {
            "status": "discovered",
            "eligible": _plain([asdict(item) for item in result.eligible]),
            "excluded": _plain([asdict(item) for item in result.exclusions]),
            "duplicates": _plain(dict(result.duplicates)),
            "unchanged_state": ["configuration", "runtime"],
        }
    )
    return 0


def _resolve_interactive_duplicate_ids(
    args: argparse.Namespace, roots: tuple[Path, ...], io: TerminalOperatorIO
) -> None:
    if not roots:
        return
    result = _discover_result(args, roots)
    assignments = parse_assignments(args.repo_id, option="--repo-id")
    for derived_id, paths in result.duplicates:
        for raw_path in paths:
            path = str(Path(raw_path).expanduser().resolve())
            if path in assignments:
                continue
            while True:
                selected = io.ask(
                    prompt=f"Unique repository ID for {path} (conflicts with {derived_id})"
                )
                if selected and slugify(selected) == selected:
                    assignments[path] = selected
                    break
                print(
                    "Use a non-empty safe ID containing letters, numbers, dots, underscores, or hyphens.",
                    file=sys.stderr,
                )
    args.repo_id = [f"{path}={repo_id}" for path, repo_id in sorted(assignments.items())]


def _run_interactive(
    args: argparse.Namespace, session_id: str | None, roots: tuple[Path, ...], render: Render
) -> int:
    io = TerminalOperatorIO(sys.stdin, sys.stdout, sys.stderr)
    if not io.interactive:
        raise ConfigError(
            "INTERACTION_REQUIRED: use --non-interactive with explicit decisions and approvals"
        )
    config_path = Path(args.config).expanduser().resolve()
    if session_id is None and not config_path.is_file() and not args.tunnel_id:
        args.tunnel_id = io.ask(prompt="Tunnel ID")
        if not args.tunnel_id:
            raise ConfigError("INTERACTION_REQUIRED: tunnel ID is required for initial onboarding")
    if session_id is None:
        _resolve_interactive_duplicate_ids(args, roots, io)
    coordinator = build_onboarding_coordinator(config_path)
    approvals = tuple(args.approve)
    decisions = tuple(sorted(parse_assignments(args.decision, option="--decision").items()))
    templates: dict[str, str] = {}
    skips: set[str] = set()
    current_session = session_id
    while True:
        result = coordinator.run(
            _command(
                args,
                roots=roots,
                resume_session_id=current_session,
                approvals=approvals,
                decisions=decisions,
                templates=tuple(sorted(templates.items())),
                skips=tuple(sorted(skips)),
                plan_only=True,
            )
        )
        current_session = result.session.session_id
        if result.preflight:
            io.show({"preflight": result.preflight})
        changed = False
        for state in result.session.repositories:
            repo_id = state.candidate.repo_id
            if state.progress is RepositoryProgress.NEEDS_DECISION:
                for code, prompt, choices in state.required_decisions:
                    choice = io.choose(prompt=f"{repo_id}: {prompt}", choices=choices)
                    values = dict(decisions)
                    values[f"{repo_id}.{code}"] = choice
                    decisions = tuple(sorted(values.items()))
                    changed = True
            elif state.progress is RepositoryProgress.BLOCKED:
                io.show(
                    {
                        "repository": repo_id,
                        "status": "blocked",
                        "proposal": json.loads(state.proposal_json or "{}"),
                    }
                )
                choice = io.choose(prompt=f"{repo_id} is blocked", choices=("r", "k", "q"))
                if choice == "r":
                    templates[repo_id] = "read_only"
                    changed = True
                elif choice == "k":
                    skips.add(repo_id)
                    changed = True
                else:
                    paused = coordinator.pause(result.session.session_id)
                    render(result_payload(paused))
                    return 3
            elif state.progress is RepositoryProgress.NEEDS_APPROVAL:
                io.show(
                    {"repository": repo_id, "proposal": json.loads(state.proposal_json or "{}")}
                )
                choice = io.choose(prompt=f"Review {repo_id}", choices=("y", "s", "r", "k", "q"))
                if choice == "y" and state.proposal_id:
                    approvals = (*approvals, f"approve:{state.proposal_id}")
                    changed = True
                elif choice == "s":
                    templates[repo_id] = "strict"
                    changed = True
                elif choice == "r":
                    templates[repo_id] = "read_only"
                    changed = True
                elif choice == "k":
                    skips.add(repo_id)
                    changed = True
                else:
                    paused = coordinator.pause(result.session.session_id)
                    render(result_payload(paused))
                    return 3
        if changed:
            continue
        if result.session.status is OnboardingStatus.READY:
            render(result_payload(result))
            if not io.confirm(prompt="Apply this reviewed batch?", default=False):
                return 3
            completed = coordinator.run(
                _command(
                    args,
                    roots=roots,
                    resume_session_id=current_session,
                    approvals=approvals,
                    decisions=decisions,
                    templates=tuple(sorted(templates.items())),
                    skips=tuple(sorted(skips)),
                    plan_only=False,
                )
            )
            render(result_payload(completed))
            return 0
        render(result_payload(result))
        return 0 if result.session.status is OnboardingStatus.COMPLETED else 3


def run_onboarding_command(args: argparse.Namespace, *, render: Render) -> int:
    action, session_id, roots = _action(args)
    coordinator = build_onboarding_coordinator(Path(args.config))
    if action == "status":
        assert session_id is not None
        render(result_payload(coordinator.status(session_id)))
        return 0
    if action == "cancel":
        assert session_id is not None
        render(result_payload(coordinator.cancel(session_id)))
        return 0
    if not args.non_interactive:
        return _run_interactive(args, session_id, roots, render)
    result = coordinator.run(_command(args, roots=roots, resume_session_id=session_id))
    render(result_payload(result))
    return (
        3
        if result.session.status
        in {OnboardingStatus.AWAITING_DECISIONS, OnboardingStatus.AWAITING_APPROVAL}
        else 0
    )
