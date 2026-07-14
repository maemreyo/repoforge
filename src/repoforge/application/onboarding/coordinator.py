"""Transactional guided-onboarding orchestration."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ...domain.config_generation import ApprovalEvent, ConfigGeneration, ConfigMutation, sha256_text
from ...domain.errors import ConfigError, operation_error_from_exception
from ...domain.onboarding import (
    OnboardingBatchPlan,
    OnboardingOptions,
    OnboardingRepositoryState,
    OnboardingSession,
    OnboardingStatus,
    OnboardingSummary,
    RepositoryProgress,
    summarize_session,
    transition_session,
)
from ...domain.redaction import redact_text
from ...domain.repository_proposal import EnrollmentMode
from ...ports.clock import Clock
from ...ports.configuration import ConfigurationStore
from ...ports.ids import IdGenerator
from ...ports.onboarding_store import OnboardingStore
from ...ports.repository_discovery import DiscoveryRequest
from ..configuration.source import SourceConfiguration, parse_source
from .discover import OnboardingDiscoveryService
from .inputs import for_repository
from .planner import OnboardingPlanner, PlanningInput
from .preflight import OnboardingPreflightService


@dataclass(frozen=True, slots=True)
class OnboardingCommand:
    config_path: Path
    roots: tuple[Path, ...]
    options: OnboardingOptions
    decisions: tuple[tuple[str, str], ...] = ()
    overrides: tuple[tuple[str, str], ...] = ()
    approvals: tuple[str, ...] = ()
    resume_session_id: str | None = None
    plan_only: bool = False
    tunnel_id: str | None = None
    profile: str = "repoforge"
    templates: tuple[tuple[str, str], ...] = ()
    skips: tuple[str, ...] = ()
    repo_ids: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    session: OnboardingSession
    plan: OnboardingBatchPlan | None
    summary: OnboardingSummary
    activation: dict[str, object] | None
    preflight: dict[str, object] | None = None


def _resolve_initial_tunnel_id(
    current_source: SourceConfiguration | None,
    *,
    command_tunnel_id: str | None,
    session_tunnel_id: str | None,
) -> str | None:
    if current_source is not None:
        return current_source.tunnel_id
    value = command_tunnel_id or session_tunnel_id
    if value is None or not value.strip():
        raise ConfigError("INPUT_REQUIRED: --tunnel-id is required for initial onboarding")
    return value.strip()


class OnboardingCoordinator:
    def __init__(
        self,
        *,
        sessions: OnboardingStore,
        discovery: OnboardingDiscoveryService,
        preflight: OnboardingPreflightService,
        planner: OnboardingPlanner,
        configs: ConfigurationStore,
        clock: Clock,
        ids: IdGenerator,
        smoke: Callable[[str, tuple[str, ...]], tuple[dict[str, object], ...]],
        activate: Callable[[ConfigGeneration, str, bool, bool], dict[str, object]],
    ) -> None:
        self._sessions = sessions
        self._discovery = discovery
        self._preflight = preflight
        self._planner = planner
        self._configs = configs
        self._clock = clock
        self._ids = ids
        self._smoke = smoke
        self._activate = activate

    def _save(self, previous: OnboardingSession, updated: OnboardingSession) -> OnboardingSession:
        return self._sessions.save(updated, expected_revision=previous.revision)

    def _current_source(self) -> SourceConfiguration | None:
        current = self._configs.current()
        if current is None:
            return None
        try:
            return parse_source(self._configs.read_source_text())
        except ValueError as exc:
            raise ConfigError(
                "CONFIG_CHANGED: guided onboarding requires source configuration v2"
            ) from exc

    def run(self, command: OnboardingCommand) -> OnboardingResult:
        now = self._clock.now_iso()
        preflight = self._preflight.inspect(command.config_path)
        if command.resume_session_id:
            session = self._sessions.read(command.resume_session_id)
            if session is None:
                raise ConfigError("SESSION_NOT_FOUND: onboarding session does not exist")
            if str(command.config_path.expanduser().resolve()) != session.config_path:
                raise ConfigError("SESSION_STALE: session config path does not match command")
        else:
            session = OnboardingSession.new(
                session_id=self._ids.new_hex(24),
                created_at=now,
                config_path=str(command.config_path.expanduser().resolve()),
                roots=tuple(str(root.expanduser().resolve()) for root in command.roots),
                options=command.options,
            )
            session = self._sessions.create(session)
        current = self._configs.current()
        current_source = self._current_source()
        actual_source_sha = sha256_text(self._configs.read_source_text()) if current else None
        if (
            command.resume_session_id
            and session.expected_source_sha256 is not None
            and actual_source_sha != session.expected_source_sha256
        ):
            raise ConfigError(
                "CONFIG_CHANGED: source configuration changed since this session was planned"
            )
        if (
            command.resume_session_id
            and current is not None
            and session.expected_generation not in {0, current.generation}
        ):
            raise ConfigError(
                "CONFIG_CHANGED: accepted generation changed since this session was planned"
            )
        enrolled = (
            tuple((item.repo_id, item.path) for item in current_source.repositories)
            if current_source
            else ()
        )
        request = DiscoveryRequest(
            tuple(Path(root) for root in session.roots),
            session.options.max_depth,
            session.options.include,
            session.options.exclude,
            (Path.home() / ".local/share/repoforge/workspaces",),
        )
        discovery = self._discovery.discover(request, enrolled=enrolled)
        repo_id_overrides = {
            str(Path(path).expanduser().resolve()): repo_id for path, repo_id in command.repo_ids
        }
        if repo_id_overrides:
            eligible = tuple(
                replace(item, repo_id=repo_id_overrides.get(item.identity.path, item.repo_id))
                for item in discovery.eligible
            )
            discovery = replace(discovery, eligible=eligible)
        prior_by_path = {item.candidate.identity.path: item for item in session.repositories}
        repositories: list[OnboardingRepositoryState] = []
        for item in discovery.eligible:
            prior = prior_by_path.get(item.identity.path)
            repositories.append(
                replace(prior, candidate=item)
                if prior is not None
                else OnboardingRepositoryState(item, template=session.options.template)
            )
        discovered = replace(
            session,
            status=OnboardingStatus.DISCOVERED,
            updated_at=now,
            expected_generation=current.generation if current else 0,
            expected_source_sha256=actual_source_sha,
            repositories=tuple(repositories),
            exclusions=discovery.exclusions,
            warnings=tuple(preflight.warnings),
        )
        session = self._save(session, discovered)
        preflight_payload: dict[str, object] = {
            "current_rf": preflight.current_rf,
            "python": preflight.python,
            "virtual_env": preflight.virtual_env,
            "uv_tool_rf": preflight.uv_tool_rf,
            "git_version": preflight.git_version,
            "gh_version": preflight.gh_version,
            "gh_authenticated": preflight.gh_authenticated,
            "tunnel_version": preflight.tunnel_version,
            "config_exists": preflight.config_exists,
            "api_key_available": preflight.api_key_available,
            "warnings": list(preflight.warnings),
        }
        if not session.repositories:
            previous = session
            session = self._save(
                previous,
                transition_session(session, OnboardingStatus.COMPLETED, now=self._clock.now_iso()),
            )
            return OnboardingResult(
                session, None, summarize_session(session), None, preflight_payload
            )
        tunnel_id = _resolve_initial_tunnel_id(
            current_source,
            command_tunnel_id=command.tunnel_id,
            session_tunnel_id=session.options.tunnel_id,
        )
        if current_source is None and session.options.tunnel_id != tunnel_id:
            previous = session
            session = self._save(
                previous,
                replace(
                    session,
                    options=replace(session.options, tunnel_id=tunnel_id),
                    updated_at=now,
                ),
            )
        decisions = dict(command.decisions)
        overrides = dict(command.overrides)
        templates = dict(command.templates)
        inputs = []
        for state in session.repositories:
            repo_id = state.candidate.repo_id
            template = templates.get(repo_id, state.template or session.options.template)
            merged_decisions = dict(state.decisions)
            merged_decisions.update(for_repository(decisions, repo_id))
            merged_overrides = dict(state.overrides)
            merged_overrides.update(for_repository(overrides, repo_id))
            inputs.append(
                (
                    repo_id,
                    PlanningInput(
                        EnrollmentMode(template),
                        tuple(sorted(merged_decisions.items())),
                        tuple(sorted(merged_overrides.items())),
                        command.approvals,
                        repo_id in command.skips or state.progress is RepositoryProgress.SKIPPED,
                    ),
                )
            )
        resolved = self._configs.read_resolved_text(current.generation) if current else None
        previous = session
        session, plan = self._planner.plan(
            session,
            current_source=current_source,
            current_resolved_text=resolved,
            current_generation=current,
            inputs=tuple(inputs),
            now=now,
            tunnel_id=tunnel_id,
            profile=session.options.profile,
        )
        session = self._save(previous, session)
        if session.status is OnboardingStatus.READY and plan is None:
            previous = session
            session = self._save(
                previous,
                transition_session(session, OnboardingStatus.COMPLETED, now=self._clock.now_iso()),
            )
            return OnboardingResult(
                session, None, summarize_session(session), None, preflight_payload
            )
        if session.status is not OnboardingStatus.READY or plan is None or command.plan_only:
            return OnboardingResult(
                session, plan, summarize_session(session), None, preflight_payload
            )
        try:
            self._smoke(plan.resolved_text, plan.repo_ids)
            previous = session
            session = self._save(
                previous,
                transition_session(session, OnboardingStatus.APPLYING, now=self._clock.now_iso()),
            )
            approval_digest = hashlib.sha256("\n".join(plan.approval_hashes).encode()).hexdigest()
            accepted = self._configs.accept(
                ConfigMutation(
                    plan.source_text,
                    plan.resolved_text,
                    plan.repository_fingerprints,
                    "guided onboarding approved repository batch",
                    self._clock.now_iso(),
                    session.expected_generation,
                    session.expected_source_sha256,
                    plan.combined_proposal_id,
                    ApprovalEvent(
                        os.environ.get("USER", "local-user"),
                        self._clock.now_iso(),
                        plan.combined_proposal_id,
                        approval_digest,
                    ),
                    self._ids.new_hex(24),
                )
            )
            activation: dict[str, object] | None = None
            if session.options.activate != "never":
                previous = session
                session = self._save(
                    previous,
                    transition_session(
                        session, OnboardingStatus.ACTIVATING, now=self._clock.now_iso()
                    ),
                )
                activation = self._activate(
                    accepted,
                    session.options.activate,
                    session.options.wait,
                    session.options.rollback_on_failure,
                )
            active_value = activation.get("active_generation") if activation else None
            repos = tuple(
                replace(item, progress=RepositoryProgress.ENROLLED)
                if item.progress is RepositoryProgress.APPROVED
                else item
                for item in session.repositories
            )
            completed = replace(
                session,
                repositories=repos,
                accepted_generation=accepted.generation,
                active_generation=int(active_value) if isinstance(active_value, int) else None,
            )
            previous = session
            session = self._save(
                previous,
                transition_session(
                    completed, OnboardingStatus.COMPLETED, now=self._clock.now_iso()
                ),
            )
            return OnboardingResult(
                session, plan, summarize_session(session), activation, preflight_payload
            )
        except Exception as exc:
            envelope = operation_error_from_exception(exc)
            secrets = (os.environ.get("CONTROL_PLANE_API_KEY", ""),)
            failed = replace(
                session,
                last_error=(
                    ("error_code", envelope.code.value),
                    ("what_happened", redact_text(envelope.what_happened, secrets=secrets)),
                    ("safe_next_action", redact_text(envelope.safe_next_action, secrets=secrets)),
                ),
            )
            if failed.status in {OnboardingStatus.APPLYING, OnboardingStatus.ACTIVATING}:
                failed = transition_session(
                    failed, OnboardingStatus.FAILED_RECOVERABLE, now=self._clock.now_iso()
                )
            session = self._save(session, failed)
            raise

    def status(self, session_id: str) -> OnboardingResult:
        session = self._sessions.read(session_id)
        if session is None:
            raise ConfigError("SESSION_NOT_FOUND: onboarding session does not exist")
        return OnboardingResult(session, None, summarize_session(session), None)

    def pause(self, session_id: str) -> OnboardingResult:
        session = self._sessions.read(session_id)
        if session is None:
            raise ConfigError("SESSION_NOT_FOUND: onboarding session does not exist")
        paused = self._sessions.save(
            transition_session(session, OnboardingStatus.PAUSED, now=self._clock.now_iso()),
            expected_revision=session.revision,
        )
        return OnboardingResult(paused, None, summarize_session(paused), None)

    def cancel(self, session_id: str) -> OnboardingResult:
        session = self._sessions.read(session_id)
        if session is None:
            raise ConfigError("SESSION_NOT_FOUND: onboarding session does not exist")
        cancelled = self._sessions.cancel(
            session_id, expected_revision=session.revision, updated_at=self._clock.now_iso()
        )
        return OnboardingResult(cancelled, None, summarize_session(cancelled), None)
