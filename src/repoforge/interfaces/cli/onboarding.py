"""CLI parsing and terminal presentation for guided multi-repository onboarding."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict
from enum import Enum
from pathlib import Path

from ...application.configuration.source import parse_source
from ...application.onboarding.coordinator import (
    OnboardingCommand,
    OnboardingCoordinator,
    OnboardingResult,
)
from ...application.onboarding.discover import DiscoveryResult, OnboardingDiscoveryService
from ...application.onboarding.inputs import parse_assignments
from ...application.onboarding.recommendations import recommend_safe_decisions
from .onboarding_review import (
    DefaultsMode,
    configuration_diff,
    discovery_rows,
    proposal_summary,
    resolve_defaults_mode,
)
from .onboarding_ui import (
    ChoiceItem,
    OnboardingUI,
    PlainOnboardingUI,
    UiBackendUnavailable,
    build_onboarding_ui,
)
from ...bootstrap import (
    build_configuration_store,
    build_onboarding_coordinator,
    build_repository_discovery,
    default_state_root,
)
from ...domain.errors import ConfigError
from ...domain.onboarding import (
    OnboardingOptions,
    OnboardingRepositoryState,
    OnboardingStatus,
    RepositoryProgress,
)
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


TerminalOperatorIO = PlainOnboardingUI


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
    onboard.add_argument(
        "--ui",
        choices=("auto", "rich", "plain"),
        default="auto",
        help="Interactive terminal backend; auto falls back to plain when optional UI libraries are absent",
    )
    onboard.add_argument(
        "--defaults",
        choices=("safe", "ask", "none"),
        default=None,
        help="Interactive recommendation policy; default is ask",
    )
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
        plan_only=getattr(args, "plan_only", False) if plan_only is None else plan_only,
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
    args: argparse.Namespace,
    roots: tuple[Path, ...],
    io: OnboardingUI,
    *,
    result: DiscoveryResult | None = None,
) -> None:
    if not roots:
        return
    discovered = result or _discover_result(args, roots)
    assignments = parse_assignments(args.repo_id, option="--repo-id")
    for derived_id, paths in discovered.duplicates:
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
                io.panel(
                    title="Invalid repository ID",
                    lines=(
                        "Use a non-empty safe ID containing letters, numbers, dots,",
                        "underscores, or hyphens.",
                    ),
                )
    args.repo_id = [
        f"{path}={repo_id}" for path, repo_id in sorted(assignments.items())
    ]


def _read_source_tunnel_id(config_path: Path) -> str | None:
    if not config_path.is_file():
        return None
    try:
        return parse_source(config_path.read_text(encoding="utf-8")).tunnel_id
    except (OSError, ValueError):
        return None


def _ensure_interactive_tunnel_id(
    args: argparse.Namespace,
    *,
    session_id: str | None,
    coordinator: OnboardingCoordinator,
    io: OnboardingUI,
) -> None:
    config_path = Path(args.config).expanduser().resolve()
    store = build_configuration_store(config_path, state_root=default_state_root())
    if store.current() is not None:
        return
    stored_tunnel_id = (
        coordinator.status(session_id).session.options.tunnel_id if session_id is not None else None
    )
    tunnel_id = args.tunnel_id or stored_tunnel_id or _read_source_tunnel_id(config_path)
    if tunnel_id is not None and tunnel_id.strip():
        args.tunnel_id = tunnel_id.strip()
        return
    io.panel(
        title="Initial tunnel setup",
        lines=(
            "No accepted RepoForge configuration exists yet.",
            "Enter the tunnel identifier from ChatGPT tunnel settings before reviewing repositories.",
        ),
    )
    args.tunnel_id = io.ask(prompt="Tunnel ID").strip()
    if not args.tunnel_id:
        raise ConfigError("INTERACTION_REQUIRED: tunnel ID is required for initial onboarding")


def _show_discovery(ui: OnboardingUI, result: DiscoveryResult) -> None:
    eligible, excluded = discovery_rows(result)
    ui.stage(index=1, total=6, title="Discovery")
    ui.table(
        title="Eligible repositories",
        headers=("ID", "Path", "Parent"),
        rows=eligible,
    )
    ui.table(
        title="Excluded paths",
        headers=("Path", "Reason", "Detail"),
        rows=excluded,
    )


def _show_preflight(ui: OnboardingUI, payload: dict[str, object]) -> None:
    warnings = payload.get("warnings")
    warning_values = warnings if isinstance(warnings, (list, tuple)) else []
    ui.panel(
        title="Environment preflight",
        lines=(
            f"rf: {payload.get('current_rf') or 'not found'}",
            f"Python: {payload.get('python') or 'unknown'}",
            f"Git: {payload.get('git_version') or 'not found'}",
            f"GitHub CLI: {payload.get('gh_version') or 'not found'}",
            f"GitHub authenticated: {payload.get('gh_authenticated')}",
            f"Tunnel client: {payload.get('tunnel_version') or 'not found'}",
            "Warnings: " + (", ".join(str(item) for item in warning_values) or "none"),
        ),
    )


def _recommendation_choices(
    repositories: tuple[OnboardingRepositoryState, ...],
    processed: set[str],
) -> tuple[tuple[ChoiceItem, ...], dict[str, str]]:
    choices: list[ChoiceItem] = []
    values: dict[str, str] = {}
    for state in repositories:
        if state.progress is not RepositoryProgress.NEEDS_DECISION:
            continue
        repo_id = state.candidate.repo_id
        for item in recommend_safe_decisions(state.required_decisions):
            key = f"{repo_id}.{item.code}"
            if key in processed:
                continue
            choices.append(
                ChoiceItem(
                    key,
                    f"{repo_id}: {item.code}={item.value}",
                    item.rationale,
                    selected=True,
                )
            )
            values[key] = item.value
    return tuple(choices), values


def _apply_recommendations(
    ui: OnboardingUI,
    mode: DefaultsMode,
    result: OnboardingResult,
    decisions: tuple[tuple[str, str], ...],
    processed: set[str],
) -> tuple[tuple[tuple[str, str], ...], bool]:
    choices, values = _recommendation_choices(result.session.repositories, processed)
    if not choices:
        return decisions, False
    processed.update(choice.value for choice in choices)
    ui.stage(index=2, total=6, title="Safe defaults")
    if mode is DefaultsMode.NONE:
        ui.panel(
            title="Safe defaults disabled",
            lines=("Every unresolved repository decision will be asked explicitly.",),
        )
        return decisions, False
    selected = (
        tuple(choice.value for choice in choices)
        if mode is DefaultsMode.SAFE
        else ui.select_many(
            prompt="Select fail-closed recommendations to apply",
            choices=choices,
        )
    )
    selected_set = set(selected)
    updated = dict(decisions)
    for key in selected:
        updated[key] = values[key]
    ui.table(
        title="Applied safe defaults",
        headers=("Repository decision", "Value"),
        rows=tuple(
            (key, values[key]) for key in _choices_to_keys(choices, selected_set)
        ),
    )
    return tuple(sorted(updated.items())), bool(selected)


def _choices_to_keys(
    choices: tuple[ChoiceItem, ...], selected: set[str]
) -> tuple[str, ...]:
    return tuple(choice.value for choice in choices if choice.value in selected)


def _show_repository_summary(
    ui: OnboardingUI, state: OnboardingRepositoryState
) -> None:
    summary = proposal_summary(state)
    ui.panel(
        title=f"Repository review: {summary.repo_id}",
        lines=(
            f"Path: {summary.path}",
            f"Confidence: {summary.confidence} | mode: {summary.mode}",
            f"Remote: {summary.remote} | base: {summary.base} | publishing: {summary.publishing}",
            f"Profiles: {summary.profiles}",
            f"Change budget: {summary.budget}",
            f"Findings: {summary.findings}",
        ),
    )


def _collect_working_directory_override(
    args: argparse.Namespace, repo_id: str, ui: OnboardingUI
) -> None:
    while True:
        selected = ui.ask(
            prompt=f"{repo_id}: relative working directory for scoped verification"
        )
        normalized = selected.strip().replace("\\", "/")
        if (
            normalized
            and not normalized.startswith(("/", "-"))
            and "," not in normalized
            and ".." not in normalized.split("/")
        ):
            break
        ui.panel(
            title="Invalid working directory",
            lines=("Use one safe repository-relative directory without '..'.",),
        )
    overrides = parse_assignments(args.policy_override, option="--policy-override")
    overrides[f"{repo_id}.working_directory"] = normalized
    args.policy_override = [
        f"{key}={value}" for key, value in sorted(overrides.items())
    ]


def _resolve_ambiguous_decisions(
    args: argparse.Namespace,
    ui: OnboardingUI,
    result: OnboardingResult,
    decisions: tuple[tuple[str, str], ...],
    templates: dict[str, str],
    skips: set[str],
    coordinator: OnboardingCoordinator,
    render: Render,
) -> tuple[tuple[tuple[str, str], ...], bool, int | None]:
    pending = tuple(
        state
        for state in result.session.repositories
        if state.progress
        in {RepositoryProgress.NEEDS_DECISION, RepositoryProgress.BLOCKED}
    )
    if not pending:
        return decisions, False, None
    ui.stage(index=3, total=6, title="Ambiguous decisions")
    updated = dict(decisions)
    changed = False
    for state in pending:
        repo_id = state.candidate.repo_id
        if state.progress is RepositoryProgress.NEEDS_DECISION:
            for code, prompt, choices in state.required_decisions:
                key = f"{repo_id}.{code}"
                if key in updated:
                    continue
                if code == "working_directory_override":
                    _collect_working_directory_override(args, repo_id, ui)
                    changed = True
                    continue
                updated[key] = ui.choose(
                    prompt=f"{repo_id}: {prompt}",
                    choices=choices,
                )
                changed = True
            continue
        _show_repository_summary(ui, state)
        while True:
            choice = ui.choose(
                prompt=f"{repo_id} is blocked",
                choices=(
                    ChoiceItem("r", "Enroll read-only"),
                    ChoiceItem("k", "Skip repository"),
                    ChoiceItem("v", "View full proposal"),
                    ChoiceItem("q", "Pause onboarding"),
                ),
                default="q",
            )
            if choice != "v":
                break
            ui.show_json(
                {
                    "repository": repo_id,
                    "status": "blocked",
                    "proposal": json.loads(state.proposal_json or "{}"),
                }
            )
        if choice == "r":
            templates[repo_id] = "read_only"
            changed = True
        elif choice == "k":
            skips.add(repo_id)
            changed = True
        else:
            paused = coordinator.pause(result.session.session_id)
            render(result_payload(paused))
            return tuple(sorted(updated.items())), changed, 3
    return tuple(sorted(updated.items())), changed, None


def _approval_states(result: OnboardingResult) -> tuple[OnboardingRepositoryState, ...]:
    return tuple(
        state
        for state in result.session.repositories
        if state.progress is RepositoryProgress.NEEDS_APPROVAL
    )


def _show_approval_table(
    ui: OnboardingUI, states: tuple[OnboardingRepositoryState, ...]
) -> None:
    summaries = tuple(proposal_summary(state) for state in states)
    ui.stage(index=4, total=6, title="Repository summaries")
    ui.table(
        title="Repositories awaiting exact approval",
        headers=("ID", "Mode", "Confidence", "Remote", "Base", "Profiles", "Findings"),
        rows=tuple(summary.row() for summary in summaries),
    )


def _select_repository(
    ui: OnboardingUI,
    states: tuple[OnboardingRepositoryState, ...],
    *,
    prompt: str,
) -> OnboardingRepositoryState:
    by_id = {state.candidate.repo_id: state for state in states}
    selected = ui.choose(
        prompt=prompt,
        choices=tuple(ChoiceItem(repo_id, repo_id) for repo_id in sorted(by_id)),
    )
    return by_id[selected]


def _review_approvals(
    ui: OnboardingUI,
    result: OnboardingResult,
    approvals: tuple[str, ...],
    templates: dict[str, str],
    skips: set[str],
    coordinator: OnboardingCoordinator,
    render: Render,
) -> tuple[tuple[str, ...], bool, int | None]:
    states = _approval_states(result)
    if not states:
        return approvals, False, None
    _show_approval_table(ui, states)
    summaries = {state.candidate.repo_id: proposal_summary(state) for state in states}
    choices = tuple(
        ChoiceItem(
            state.candidate.repo_id,
            state.candidate.repo_id,
            (
                f"{summaries[state.candidate.repo_id].mode}; "
                f"profiles {summaries[state.candidate.repo_id].profiles}; "
                f"publishing {summaries[state.candidate.repo_id].publishing}"
            ),
            selected=False,
        )
        for state in states
    )
    selected = set(
        ui.select_many(
            prompt="Select repositories to approve with their exact proposal IDs",
            choices=choices,
        )
    )
    if selected:
        current = set(approvals)
        for state in states:
            if state.candidate.repo_id in selected and state.proposal_id:
                current.add(f"approve:{state.proposal_id}")
        return tuple(sorted(current)), True, None
    action = ui.choose(
        prompt="No repository was selected for approval",
        choices=(
            ChoiceItem("a", "Adjust one repository"),
            ChoiceItem("v", "View one full proposal"),
            ChoiceItem("p", "Pause and resume later"),
        ),
        default="p",
    )
    if action == "v":
        state = _select_repository(ui, states, prompt="Proposal to display")
        ui.show_json(
            {
                "repository": state.candidate.repo_id,
                "proposal": json.loads(state.proposal_json or "{}"),
            }
        )
        return approvals, True, None
    if action == "a":
        state = _select_repository(ui, states, prompt="Repository to adjust")
        repo_id = state.candidate.repo_id
        adjustment = ui.choose(
            prompt=f"Adjust {repo_id}",
            choices=(
                ChoiceItem("s", "Use strict template"),
                ChoiceItem("r", "Use read-only template"),
                ChoiceItem("k", "Skip repository"),
            ),
        )
        if adjustment == "s":
            templates[repo_id] = "strict"
        elif adjustment == "r":
            templates[repo_id] = "read_only"
        else:
            skips.add(repo_id)
        return approvals, True, None
    paused = coordinator.pause(result.session.session_id)
    render(result_payload(paused))
    return approvals, False, 3


def _current_source_text(config_path: Path) -> str:
    try:
        return config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    except OSError:
        return ""


def _show_config_diff(
    ui: OnboardingUI, config_path: Path, result: OnboardingResult
) -> None:
    assert result.plan is not None
    ui.stage(index=5, total=6, title="Config diff")
    ui.code(
        title="Reviewed source configuration",
        text=configuration_diff(
            _current_source_text(config_path), result.plan.source_text
        ),
        lexer="diff",
    )
    ui.panel(
        title="Batch impact",
        lines=(
            f"Repositories: {', '.join(result.plan.repo_ids)}",
            f"Capability delta: {result.plan.capability_delta}",
            f"Combined proposal: {result.plan.combined_proposal_id}",
            "Configuration and runtime remain unchanged until Apply is confirmed.",
        ),
    )


def _run_interactive(
    args: argparse.Namespace,
    session_id: str | None,
    roots: tuple[Path, ...],
    render: Render,
) -> int:
    try:
        ui = build_onboarding_ui(
            getattr(args, "ui", "auto"),
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except UiBackendUnavailable as exc:
        raise ConfigError(f"INTERACTION_REQUIRED: {exc}") from exc
    if not ui.interactive:
        raise ConfigError(
            "INTERACTION_REQUIRED: use --non-interactive with explicit decisions and approvals"
        )
    defaults_mode = resolve_defaults_mode(
        getattr(args, "defaults", None), non_interactive=False
    )
    config_path = Path(args.config).expanduser().resolve()
    coordinator = build_onboarding_coordinator(config_path)
    _ensure_interactive_tunnel_id(
        args, session_id=session_id, coordinator=coordinator, io=ui
    )
    display_roots = roots
    if session_id is not None and not display_roots:
        display_roots = tuple(
            Path(root) for root in coordinator.status(session_id).session.roots
        )
    discovered = _discover_result(args, display_roots)
    _show_discovery(ui, discovered)
    if session_id is None:
        _resolve_interactive_duplicate_ids(args, roots, ui, result=discovered)
    approvals = tuple(args.approve)
    decisions = tuple(
        sorted(parse_assignments(args.decision, option="--decision").items())
    )
    templates: dict[str, str] = {}
    skips: set[str] = set()
    processed_defaults: set[str] = set()
    current_session = session_id
    shown_preflight = False
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
        if result.preflight and not shown_preflight:
            _show_preflight(ui, result.preflight)
            shown_preflight = True
        decisions, changed = _apply_recommendations(
            ui,
            defaults_mode,
            result,
            decisions,
            processed_defaults,
        )
        if changed:
            continue
        decisions, changed, exit_code = _resolve_ambiguous_decisions(
            args,
            ui,
            result,
            decisions,
            templates,
            skips,
            coordinator,
            render,
        )
        if exit_code is not None:
            return exit_code
        if changed:
            continue
        approvals, changed, exit_code = _review_approvals(
            ui,
            result,
            approvals,
            templates,
            skips,
            coordinator,
            render,
        )
        if exit_code is not None:
            return exit_code
        if changed:
            continue
        if result.session.status is OnboardingStatus.READY and result.plan is not None:
            _show_config_diff(ui, config_path, result)
            ui.stage(index=6, total=6, title="Apply")
            if getattr(args, "plan_only", False):
                ui.panel(
                    title="Plan-only complete",
                    lines=(
                        "No configuration generation or runtime state was changed.",
                    ),
                )
                render(result_payload(result))
                return 0
            if not ui.confirm(prompt="Apply this reviewed batch?", default=False):
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
            ui.panel(
                title="Onboarding completed",
                lines=(
                    f"Enrolled repositories: {completed.summary.enrolled}",
                    f"Accepted generation: {completed.session.accepted_generation}",
                    f"Active generation: {completed.session.active_generation or 'unchanged'}",
                ),
            )
            render(result_payload(completed))
            return 0
        if result.session.status is OnboardingStatus.COMPLETED:
            ui.panel(
                title="Nothing to enroll",
                lines=(
                    "All discovered repositories are already enrolled or excluded.",
                ),
            )
            render(result_payload(result))
            return 0
        render(result_payload(result))
        return 3


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
    try:
        resolve_defaults_mode(getattr(args, "defaults", None), non_interactive=True)
    except ValueError as exc:
        raise ConfigError(f"INTERACTION_REQUIRED: {exc}") from exc
    result = coordinator.run(_command(args, roots=roots, resume_session_id=session_id))
    render(result_payload(result))
    return (
        3
        if result.session.status
        in {OnboardingStatus.AWAITING_DECISIONS, OnboardingStatus.AWAITING_APPROVAL}
        else 0
    )
