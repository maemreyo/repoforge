"""Pure guided-onboarding state, discovery, and batch-plan models."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class OnboardingStatus(str, Enum):
    CREATED = "created"
    DISCOVERED = "discovered"
    AWAITING_DECISIONS = "awaiting_decisions"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    APPLYING = "applying"
    ACTIVATING = "activating"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED_RECOVERABLE = "failed_recoverable"
    CANCELLED = "cancelled"
    INVALID = "invalid"


class RepositoryProgress(str, Enum):
    DISCOVERED = "discovered"
    EXCLUDED = "excluded"
    NEEDS_DECISION = "needs_decision"
    NEEDS_APPROVAL = "needs_approval"
    APPROVED = "approved"
    SKIPPED = "skipped"
    ENROLLED = "enrolled"
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"
    FAILED = "failed"


class ExclusionReason(str, Enum):
    ALREADY_ENROLLED = "already_enrolled"
    LINKED_WORKTREE = "linked_worktree"
    REPOFORGE_MANAGED_WORKSPACE = "repoforge_managed_workspace"
    BARE_REPOSITORY = "bare_repository"
    GENERATED_WORKTREE_DIRECTORY = "generated_worktree_directory"
    NESTED_DUPLICATE_CHECKOUT = "nested_duplicate_checkout"
    OUTSIDE_ALLOWED_ROOT = "outside_allowed_root"
    INVALID_GIT_REPOSITORY = "invalid_git_repository"
    UNREADABLE_PATH = "unreadable_path"
    OPERATOR_EXCLUDED = "operator_excluded"


@dataclass(frozen=True, slots=True)
class DiscoveryIdentity:
    path: str
    worktree_root: str
    git_common_dir: str
    primary: bool
    bare: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    identity: DiscoveryIdentity
    repo_id: str
    parent_repo_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryExclusion:
    path: str
    reason: ExclusionReason
    detail: str = ""
    repo_id: str | None = None


@dataclass(frozen=True, slots=True)
class OnboardingOptions:
    max_depth: int = 8
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    template: str = "standard"
    activate: str = "auto"
    wait: bool = True
    rollback_on_failure: bool = True
    tunnel_id: str | None = None
    profile: str = "repoforge"

    def __post_init__(self) -> None:
        if self.max_depth < 0 or self.max_depth > 64:
            raise ValueError("max_depth must be between 0 and 64")
        if self.template not in {"read_only", "standard", "strict"}:
            raise ValueError("Unsupported onboarding template")
        if self.activate not in {"auto", "always", "never"}:
            raise ValueError("Unsupported activation mode")
        if not self.wait and self.rollback_on_failure:
            raise ValueError("--no-wait requires --no-rollback-on-failure")
        if not self.profile:
            raise ValueError("Tunnel profile cannot be empty")


@dataclass(frozen=True, slots=True)
class OnboardingRepositoryState:
    candidate: DiscoveryCandidate
    progress: RepositoryProgress = RepositoryProgress.DISCOVERED
    template: str = "standard"
    decisions: tuple[tuple[str, str], ...] = ()
    overrides: tuple[tuple[str, str], ...] = ()
    proposal_id: str | None = None
    facts_fingerprint: str | None = None
    approval_sha256: str | None = None
    required_decisions: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    proposal_json: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class OnboardingSession:
    schema_version: int
    session_id: str
    revision: int
    created_at: str
    updated_at: str
    status: OnboardingStatus
    config_path: str
    roots: tuple[str, ...]
    options: OnboardingOptions
    expected_source_sha256: str | None = None
    expected_generation: int = 0
    repositories: tuple[OnboardingRepositoryState, ...] = ()
    exclusions: tuple[DiscoveryExclusion, ...] = ()
    warnings: tuple[str, ...] = ()
    accepted_generation: int | None = None
    active_generation: int | None = None
    last_error: tuple[tuple[str, str], ...] = ()

    @classmethod
    def new(
        cls,
        *,
        session_id: str,
        created_at: str,
        config_path: str,
        roots: tuple[str, ...],
        options: OnboardingOptions,
    ) -> OnboardingSession:
        if not session_id or not created_at or not config_path or not roots:
            raise ValueError("Onboarding session identity is incomplete")
        return cls(
            1,
            session_id,
            0,
            created_at,
            created_at,
            OnboardingStatus.CREATED,
            config_path,
            roots,
            options,
        )


@dataclass(frozen=True, slots=True)
class OnboardingBatchPlan:
    source_text: str
    resolved_text: str
    repo_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...]
    combined_proposal_id: str
    approval_hashes: tuple[str, ...]
    repository_fingerprints: tuple[tuple[str, str], ...]
    capability_delta: str


@dataclass(frozen=True, slots=True)
class OnboardingSummary:
    discovered: int
    excluded: int
    approved: int
    skipped: int
    enrolled: int
    unchanged: int
    blocked: int
    failed: int


_ALLOWED_TRANSITIONS: dict[OnboardingStatus, set[OnboardingStatus]] = {
    OnboardingStatus.CREATED: {
        OnboardingStatus.DISCOVERED,
        OnboardingStatus.PAUSED,
        OnboardingStatus.CANCELLED,
    },
    OnboardingStatus.DISCOVERED: {
        OnboardingStatus.AWAITING_DECISIONS,
        OnboardingStatus.AWAITING_APPROVAL,
        OnboardingStatus.READY,
        OnboardingStatus.COMPLETED,
        OnboardingStatus.PAUSED,
        OnboardingStatus.CANCELLED,
    },
    OnboardingStatus.AWAITING_DECISIONS: {
        OnboardingStatus.DISCOVERED,
        OnboardingStatus.AWAITING_APPROVAL,
        OnboardingStatus.READY,
        OnboardingStatus.PAUSED,
        OnboardingStatus.CANCELLED,
    },
    OnboardingStatus.AWAITING_APPROVAL: {
        OnboardingStatus.DISCOVERED,
        OnboardingStatus.AWAITING_DECISIONS,
        OnboardingStatus.READY,
        OnboardingStatus.PAUSED,
        OnboardingStatus.CANCELLED,
    },
    OnboardingStatus.READY: {
        OnboardingStatus.DISCOVERED,
        OnboardingStatus.APPLYING,
        OnboardingStatus.COMPLETED,
        OnboardingStatus.PAUSED,
        OnboardingStatus.CANCELLED,
    },
    OnboardingStatus.APPLYING: {
        OnboardingStatus.ACTIVATING,
        OnboardingStatus.COMPLETED,
        OnboardingStatus.FAILED_RECOVERABLE,
    },
    OnboardingStatus.ACTIVATING: {OnboardingStatus.COMPLETED, OnboardingStatus.FAILED_RECOVERABLE},
    OnboardingStatus.PAUSED: {
        OnboardingStatus.DISCOVERED,
        OnboardingStatus.AWAITING_DECISIONS,
        OnboardingStatus.AWAITING_APPROVAL,
        OnboardingStatus.READY,
        OnboardingStatus.CANCELLED,
    },
    OnboardingStatus.FAILED_RECOVERABLE: {
        OnboardingStatus.DISCOVERED,
        OnboardingStatus.AWAITING_DECISIONS,
        OnboardingStatus.AWAITING_APPROVAL,
        OnboardingStatus.READY,
        OnboardingStatus.CANCELLED,
    },
}


def detect_duplicate_repo_ids(
    candidates: tuple[DiscoveryCandidate, ...],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.repo_id, []).append(candidate.identity.path)
    return {key: tuple(sorted(paths)) for key, paths in sorted(grouped.items()) if len(paths) > 1}


def transition_session(
    session: OnboardingSession, target: OnboardingStatus, *, now: str
) -> OnboardingSession:
    if target == session.status:
        return replace(session, updated_at=now)
    if target not in _ALLOWED_TRANSITIONS.get(session.status, set()):
        raise ValueError(f"Invalid onboarding transition: {session.status.value} -> {target.value}")
    return replace(session, status=target, updated_at=now)


def summarize_session(session: OnboardingSession) -> OnboardingSummary:
    counts = {item: 0 for item in RepositoryProgress}
    for repository in session.repositories:
        counts[repository.progress] += 1
    return OnboardingSummary(
        discovered=len(session.repositories),
        excluded=len(session.exclusions),
        approved=counts[RepositoryProgress.APPROVED],
        skipped=counts[RepositoryProgress.SKIPPED],
        enrolled=counts[RepositoryProgress.ENROLLED],
        unchanged=counts[RepositoryProgress.UNCHANGED],
        blocked=counts[RepositoryProgress.BLOCKED],
        failed=counts[RepositoryProgress.FAILED],
    )
