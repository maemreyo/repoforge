"""Strict request and response models for the static 29-tool Forge v2 surface."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from string import Formatter
from typing import Annotated, Literal

from pydantic import Field, model_validator

# Re-exported, not redefined: `application` needs the same names and does not depend on
# the contract layer, so the single declaration lives in `domain`.
from ..domain.context_sections import DEFAULT_CONTEXT_SECTIONS, ContextSectionName
from .common import (
    AuthSelectionInput,
    ByteBudget,
    ChangeMetrics,
    CommandEvidence,
    CommitSummary,
    Cursor,
    DiffFile,
    ExecutionEvidenceModel,
    Freshness,
    GitObjectId,
    GitRef,
    Identifier,
    KeyValue,
    LongText,
    OperationEvidence,
    OperationState,
    OutcomeReceiptEvidence,
    ProviderEvidence,
    ReadFileRequest,
    ReadFileResult,
    RelativePath,
    RepoId,
    RepositorySummary,
    SearchMatch,
    SearchMode,
    Sha256,
    ShortText,
    StrictModel,
    ToolResponse,
    TreeEntry,
    WorkspaceSummary,
)


class ContextSection(StrictModel):
    name: ContextSectionName
    freshness: Freshness
    complete: bool
    truncated: bool = False
    facts: tuple[KeyValue, ...] = Field(default=(), max_length=200)


class RepoTaskContextInput(StrictModel):
    repo_id: RepoId
    issue_number: int | None = Field(default=None, ge=1)
    workspace_id: Identifier | None = None
    sections: tuple[ContextSectionName, ...] = Field(
        default=DEFAULT_CONTEXT_SECTIONS,
        min_length=1,
        max_length=5,
    )
    byte_budget: ByteBudget = 120_000


class RepoTaskContextOutput(ToolResponse):
    repo_id: RepoId
    sections: tuple[ContextSection, ...] = Field(default=(), max_length=5)
    truncated: bool = False
    next_cursor: Cursor | None = None


class RepoReadInput(StrictModel):
    repo_id: RepoId
    files: tuple[ReadFileRequest, ...] = Field(min_length=1, max_length=20)
    ref: GitRef | None = None
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class RepoReadOutput(ToolResponse):
    repo_id: RepoId
    resolved_ref: GitRef
    commit_sha: GitObjectId
    files: tuple[ReadFileResult, ...] = Field(default=(), max_length=20)
    truncated: bool = False
    next_cursor: Cursor | None = None


class RepoSearchInput(StrictModel):
    repo_id: RepoId
    query: str = Field(min_length=1, max_length=4000)
    mode: SearchMode = SearchMode.LITERAL
    ref: GitRef | None = None
    path_glob: str | None = Field(default=None, max_length=4096)
    max_results: int = Field(default=100, ge=1, le=200)
    context_lines: int = Field(default=0, ge=0, le=5)
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class RepoSearchOutput(ToolResponse):
    repo_id: RepoId
    resolved_ref: GitRef
    commit_sha: GitObjectId
    mode: SearchMode
    matches: tuple[SearchMatch, ...] = Field(default=(), max_length=200)
    omitted_count: int = Field(default=0, ge=0)
    source_truncated: bool = False
    truncated: bool = False
    next_cursor: Cursor | None = None
    truncation_reason: (
        Literal[
            "search_deadline_exceeded",
            "result_count_limit",
            "result_transport_budget",
            "source_limit",
        ]
        | None
    ) = None
    scanned_path_count: int = Field(default=0, ge=0)
    candidate_path_count: int = Field(default=0, ge=0)
    remaining_path_count: int = Field(default=0, ge=0)
    completed_providers: tuple[str, ...] = Field(default=(), max_length=10)
    recommended_scope: ShortText | None = None


class RepoTreeInput(StrictModel):
    repo_id: RepoId
    ref: GitRef | None = None
    subtree: RelativePath | None = None
    max_entries: int = Field(default=500, ge=1, le=2000)
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class RepoTreeOutput(ToolResponse):
    repo_id: RepoId
    resolved_ref: GitRef
    commit_sha: GitObjectId
    subtree: RelativePath | None = None
    entries: tuple[TreeEntry, ...] = Field(default=(), max_length=2000)
    omitted_count: int = Field(default=0, ge=0)
    source_truncated: bool = False
    truncated: bool = False
    next_cursor: Cursor | None = None


class HistoryMode(str, Enum):
    COMMIT = "commit"
    LOG = "log"
    COMPARE = "compare"


class FileChange(StrictModel):
    path: RelativePath
    status: Literal["added", "modified", "deleted", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class HistoryComparison(StrictModel):
    base_sha: GitObjectId
    head_sha: GitObjectId
    merge_base_sha: GitObjectId
    ahead: int = Field(ge=0)
    behind: int = Field(ge=0)
    files: tuple[FileChange, ...] = Field(default=(), max_length=500)


class RepoHistoryInput(StrictModel):
    repo_id: RepoId
    mode: HistoryMode
    ref: GitRef | None = None
    base_ref: GitRef | None = None
    head_ref: GitRef | None = None
    path_glob: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=20, ge=1, le=200)
    include_patch: bool = False
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class RepoHistoryOutput(ToolResponse):
    repo_id: RepoId
    mode: HistoryMode
    commit: CommitSummary | None = None
    commits: tuple[CommitSummary, ...] = Field(default=(), max_length=200)
    comparison: HistoryComparison | None = None
    truncated: bool = False
    next_cursor: Cursor | None = None


class IssueMode(str, Enum):
    READ = "read"
    SPEC = "spec"
    GRAPH = "graph"
    NEXT = "next"
    COMMENT = "comment"
    CLOSE = "close"
    REOPEN = "reopen"
    LINK = "link"
    CREATE = "create"
    MANAGE = "manage"


IssueGraphClientRef = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"),
]
IssueGraphProposalId = Annotated[str, Field(pattern=r"^igp-[a-f0-9]{24}$")]
IssueGraphPlanId = Annotated[str, Field(pattern=r"^igplan-[a-f0-9]{24}$")]
IssueGraphPublicationId = Annotated[str, Field(pattern=r"^igpub-[a-f0-9]{24}$")]
ApprovalRequestId = Annotated[str, Field(pattern=r"^apr-[a-f0-9]{24}$")]


class IssueGraphNodeInput(StrictModel):
    client_ref: IssueGraphClientRef
    title: str = Field(min_length=1, max_length=1_000)
    ticket_type: Literal["program", "epic", "task"]
    priority: Literal["p0", "p1", "p2", "p3"]
    status: Literal["planned", "ready", "in_progress", "blocked", "done"]
    parent_ref: IssueGraphClientRef | None = None
    body: str = Field(min_length=1, max_length=20_000)


class IssueGraphEdgeInput(StrictModel):
    source_ref: IssueGraphClientRef
    target_ref: IssueGraphClientRef
    kind: Literal["blocked_by", "relates", "supersedes"]


class IssueGraphManagePlanInput(StrictModel):
    action: Literal["plan"]
    root_ref: IssueGraphClientRef
    nodes: tuple[IssueGraphNodeInput, ...] = Field(min_length=1, max_length=100)
    edges: tuple[IssueGraphEdgeInput, ...] = Field(default=(), max_length=500)
    adopt_refs: tuple[IssueGraphClientRef, ...] = Field(default=(), max_length=100)
    expires_in_seconds: int = Field(default=3_600, ge=60, le=86_400)


class IssueGraphManageApplyInput(StrictModel):
    action: Literal["apply"]
    proposal_id: IssueGraphProposalId
    proposal_hash: Sha256
    plan_id: IssueGraphPlanId
    effect_plan_hash: Sha256
    approval_request_id: ApprovalRequestId


class IssueGraphManageStatusInput(StrictModel):
    action: Literal["status"]
    publication_id: IssueGraphPublicationId


class IssueGraphManageReconcileInput(StrictModel):
    action: Literal["reconcile"]
    publication_id: IssueGraphPublicationId


IssueGraphManageInput = Annotated[
    IssueGraphManagePlanInput
    | IssueGraphManageApplyInput
    | IssueGraphManageStatusInput
    | IssueGraphManageReconcileInput,
    Field(discriminator="action"),
]


class IssueLinkType(str, Enum):
    SUB_ISSUE = "sub_issue"
    BLOCKED_BY = "blocked_by"
    SUPERSEDE = "supersede"


class IssueState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class IssueEvidence(StrictModel):
    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1000)
    state: IssueState
    body: str = Field(default="", max_length=60_000)
    labels: tuple[str, ...] = Field(default=(), max_length=100)
    freshness: Freshness


class IssueGraphNode(StrictModel):
    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1000)
    status: str = Field(min_length=1, max_length=100)
    priority: str | None = Field(default=None, max_length=30)
    blockers: tuple[int, ...] = Field(default=(), max_length=100)
    children: tuple[int, ...] = Field(default=(), max_length=100)


class IssueDrift(StrictModel):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)
    issue_number: int = Field(ge=1)


class GraphEvidenceCapability(str, Enum):
    ISSUE = "issue"
    COMMENTS = "comments"
    SUB_ISSUES = "sub_issues"
    DEPENDENCIES = "dependencies"
    PROJECT_OVERLAY = "project_overlay"


class GraphEvidenceCapabilityCoverage(StrictModel):
    """Completeness of one independently-observed GitHub read, scoped to the
    issues it actually touched -- so a caller can tell exactly which
    capability is missing instead of one blanket `evidence_complete` flag."""

    capability: GraphEvidenceCapability
    complete: bool
    unavailable: tuple[int, ...] = Field(default=(), max_length=200)
    truncated: bool = False


class CapabilityReadStat(StrictModel):
    """Shared participation of one capability in the graph observation.

    The process/byte/duration values are shared attribution: one batched
    GraphQL request is counted for every capability it carried, so
    per-capability values overlap and must not be summed."""

    capability: GraphEvidenceCapability
    provider_processes: int = Field(ge=0)
    captured_stdout_bytes: int = Field(ge=0)
    provider_process_duration_ms: float = Field(ge=0)


class TicketGraphReadStats(StrictModel):
    """Bounded provider-traffic evidence for one graph observation.

    ``source`` is ``live_full`` for a fresh batched read or ``cache`` for a
    TTL cache hit that performed no provider calls. ``provider_processes``
    counts ``gh`` subprocess launches against the provider; it is a process
    count, not an HTTPS request count (the transport is not directly
    instrumented, and higher-level operations such as ``gh project
    item-list`` may perform more than one network request).
    ``captured_stdout_bytes`` is the size of the stdout captured for the
    payload and ``provider_process_duration_ms`` the elapsed wall time of the
    launches, so benchmark claims never overstate request evidence.
    Per-capability entries are shared participation counts for the batched
    requests that carried each capability's evidence."""

    source: Literal["live_full", "cache"] = "live_full"
    provider_processes: int = Field(ge=0)
    captured_stdout_bytes: int = Field(ge=0)
    provider_process_duration_ms: float = Field(ge=0)
    per_capability: tuple[CapabilityReadStat, ...] = Field(default=(), max_length=5)
    cache_hit_reason: str | None = Field(default=None, max_length=60)
    cache_miss_reason: str | None = Field(default=None, max_length=60)
    cache_age_ms: float | None = Field(default=None, ge=0)


class RepoIssueInput(AuthSelectionInput):
    repo_id: RepoId
    mode: IssueMode
    issue_number: int | None = Field(default=None, ge=1)
    root_issue: int | None = Field(default=None, ge=1)
    status: str | None = Field(default=None, max_length=100)
    priority: str | None = Field(default=None, max_length=30)
    initiative: int | None = Field(default=None, ge=1)
    limit: int = Field(default=10, ge=1, le=100)
    fresh: bool = False
    cursor: Cursor | None = None
    body: str | None = Field(default=None, min_length=1, max_length=20_000)
    title: str | None = Field(default=None, min_length=1, max_length=1_000)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=1_000)
    target_issue: int | None = Field(default=None, ge=1)
    link_type: IssueLinkType | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)
    approval_request_id: str | None = Field(default=None, min_length=1, max_length=160)
    manage: IssueGraphManageInput | None = None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> RepoIssueInput:
        write_modes = {
            IssueMode.COMMENT,
            IssueMode.CLOSE,
            IssueMode.REOPEN,
            IssueMode.LINK,
            IssueMode.CREATE,
        }
        issue_modes = write_modes - {IssueMode.CREATE}
        if self.mode not in write_modes and self.mode is not IssueMode.MANAGE:
            # A read mode performs no external mutation, so choosing an identity for it would
            # promise something the call does not do.
            self.reject_selector_on_read(f"repo_issue {self.mode.value}")
        if self.mode is IssueMode.MANAGE:
            if self.manage is None:
                raise ValueError("repo_issue manage requires manage")
            if (
                any(
                    value is not None
                    for value in (
                        self.issue_number,
                        self.root_issue,
                        self.status,
                        self.priority,
                        self.initiative,
                        self.cursor,
                        self.body,
                        self.title,
                        self.evidence_ref,
                        self.target_issue,
                        self.link_type,
                        self.idempotency_key,
                        self.approval_request_id,
                    )
                )
                or self.fresh
                or self.limit != 10
            ):
                raise ValueError("repo_issue manage does not accept read or write branch fields")
        elif self.manage is not None:
            raise ValueError("manage is only valid for repo_issue manage")
        if self.mode in {IssueMode.READ, IssueMode.SPEC} and self.issue_number is None:
            raise ValueError(f"repo_issue {self.mode.value} requires issue_number")
        if self.mode in issue_modes and self.issue_number is None:
            raise ValueError(f"repo_issue {self.mode.value} requires issue_number")
        if self.mode in write_modes and self.idempotency_key is None:
            raise ValueError(f"repo_issue {self.mode.value} requires idempotency_key")
        if self.mode in write_modes and self.evidence_ref is None:
            raise ValueError(f"repo_issue {self.mode.value} requires evidence_ref")
        if self.mode is IssueMode.COMMENT and self.body is None:
            raise ValueError("repo_issue comment requires body")
        if self.mode is IssueMode.LINK and (self.target_issue is None or self.link_type is None):
            raise ValueError("repo_issue link requires target_issue and link_type")
        if self.mode is IssueMode.CREATE and (self.title is None or self.body is None):
            raise ValueError("repo_issue create requires title and body")
        if self.mode is not IssueMode.LINK and (
            self.target_issue is not None or self.link_type is not None
        ):
            raise ValueError("target_issue and link_type are only valid for repo_issue link")
        if self.mode is not IssueMode.CREATE and self.title is not None:
            raise ValueError("title is only valid for repo_issue create")
        if self.mode not in {IssueMode.COMMENT, IssueMode.CREATE} and self.body is not None:
            raise ValueError("body is only valid for repo_issue comment or create")
        if self.mode not in write_modes and (
            self.evidence_ref is not None
            or self.idempotency_key is not None
            or self.approval_request_id is not None
        ):
            raise ValueError("write fields are only valid for repo_issue write modes")
        return self


class IssueMutationEvidence(StrictModel):
    operation: Literal["comment", "close", "reopen", "link", "create"]
    result: Literal["applied", "reconciled", "pending_approval"]
    issue_number: int | None = Field(default=None, ge=1)
    target_issue: int | None = Field(default=None, ge=1)
    link_type: IssueLinkType | None = None
    marker: str = Field(min_length=1, max_length=200)
    external_writes: int = Field(default=0, ge=0, le=20)
    idempotent_replay: bool = False
    approval_request_id: str | None = Field(default=None, max_length=160)
    url: str | None = Field(default=None, max_length=2_000)


class IssueGraphWorkflowEvidence(StrictModel):
    action: Literal["plan", "apply", "status", "reconcile"]
    state: Literal[
        "planned",
        "pending_approval",
        "publishing",
        "paused",
        "partial_failed",
        "manual_recovery_required",
        "succeeded",
        "stale",
    ]
    proposal_id: IssueGraphProposalId | None = None
    proposal_hash: Sha256 | None = None
    plan_id: IssueGraphPlanId | None = None
    effect_plan_hash: Sha256 | None = None
    approval_request_id: ApprovalRequestId | None = None
    approval_status: (
        Literal[
            "pending",
            "accepted",
            "declined",
            "cancelled",
            "expired",
            "invalidated",
        ]
        | None
    ) = None
    publication_id: IssueGraphPublicationId | None = None
    publication_state: (
        Literal[
            "running",
            "paused",
            "manual_recovery_required",
            "succeeded",
        ]
        | None
    ) = None
    operation_id: Identifier | None = None
    receipt_id: Identifier | None = None
    result_reference: str | None = Field(default=None, max_length=256)
    retry_at: str | None = Field(default=None, max_length=80)
    complete: bool
    external_writes: int = Field(default=0, ge=0, le=1_000)
    recovery_action: ShortText | None = None


class RepoIssueOutput(ToolResponse):
    repo_id: RepoId
    mode: IssueMode
    graph_status: Literal["available", "graph_unavailable", "not_requested"]
    graph_unavailable_reason: (
        Literal["configuration_unavailable", "provider_unavailable", "evidence_incomplete"] | None
    ) = None
    issue: IssueEvidence | None = None
    nodes: tuple[IssueGraphNode, ...] = Field(default=(), max_length=500)
    selected: tuple[IssueGraphNode, ...] = Field(default=(), max_length=100)
    drift: tuple[IssueDrift, ...] = Field(default=(), max_length=100)
    mutation: IssueMutationEvidence | None = None
    workflow: IssueGraphWorkflowEvidence | None = None
    outcome: OutcomeReceiptEvidence | None = None
    next_action: ShortText | None = None
    truncated: bool = False
    next_cursor: Cursor | None = None
    capability_coverage: tuple[GraphEvidenceCapabilityCoverage, ...] = Field(
        default=(), max_length=5
    )
    read_stats: TicketGraphReadStats | None = None


class PullRequestEvidence(StrictModel):
    number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1000)
    state: str = Field(min_length=1, max_length=80)
    draft: bool
    head_sha: GitObjectId
    base_ref: GitRef
    review_decision: str | None = Field(default=None, max_length=80)
    freshness: Freshness


class RepoPrReadInput(StrictModel):
    repo_id: RepoId
    pr_number: int = Field(ge=1)
    fresh: bool = False
    detail: Literal["overview", "files", "checks", "reviews"] = "overview"
    cursor: Cursor | None = None


class RepoPrReadOutput(ToolResponse):
    repo_id: RepoId
    pull_request: PullRequestEvidence
    facts: tuple[KeyValue, ...] = Field(default=(), max_length=500)
    truncated: bool = False
    next_cursor: Cursor | None = None


class RepositorySelectionOutcome(str, Enum):
    EXACT_MATCH = "exact_match"
    SINGLE_ENROLLED = "single_enrolled"
    INPUT_REQUIRED = "input_required"
    NO_MATCH = "no_match"


class RepositorySelectionCandidate(StrictModel):
    repo_id: RepoId
    display_name: ShortText


class RepositorySelection(StrictModel):
    outcome: RepositorySelectionOutcome
    repo_id: RepoId | None = None
    candidates: tuple[RepositorySelectionCandidate, ...] = Field(default=(), max_length=200)
    guidance: ShortText
    repo_selection_id: Identifier | None = None
    selection_generation: int | None = Field(default=None, ge=1)
    capability_digest: Sha256 | None = None
    expires_at: datetime | None = None


class SelectionPrompt(StrictModel):
    """Deterministic fallback text for an INPUT_REQUIRED decision, present regardless of
    negotiated Elicitation support (bounded, never gated on client capability)."""

    status: Literal["INPUT_REQUIRED"] = "INPUT_REQUIRED"
    fallback_for: Literal["elicitation"] = "elicitation"
    decision_id: Identifier
    prompt: ShortText
    allowed_options: tuple[ShortText, ...] = Field(min_length=1, max_length=32)


class RepoListInput(StrictModel):
    detail: bool = False
    cursor: Cursor | None = None
    limit: int = Field(default=50, ge=1, le=100)
    requested_repo: ShortText | None = None


class RepoListOutput(ToolResponse):
    repositories: tuple[RepositorySummary, ...] = Field(default=(), max_length=100)
    truncated: bool = False
    next_cursor: Cursor | None = None
    selection: RepositorySelection
    selection_prompt: SelectionPrompt | None = None


class PolicyAction(str, Enum):
    PREVIEW = "preview"
    APPLY = "apply"


class PolicyMutation(StrictModel):
    section: Literal["profile", "diagnostic", "formatter", "override"]
    name: str = Field(min_length=1, max_length=160)
    operation: Literal["set", "remove"]
    value: str | None = Field(default=None, max_length=20_000)


# The contracts package deliberately imports no domain module, so these must be kept
# equal to MAX_ADHOC_RUNNERS, MAX_ADHOC_TIMEOUT_SECONDS and the domain's runner-basename
# pattern by test rather than by import. A schema that accepts what `domain.adhoc` then
# refuses reads to the caller as an arbitrary failure.
_MAX_ADHOC_RUNNERS = 32
_MAX_ADHOC_TIMEOUT_SECONDS = 3_600
_AdhocRunnerName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]


class ExecutionPolicyDeclaration(StrictModel):
    """Relaxed-mode ad-hoc execution settings for one repository.

    This is the reviewed way for an agent to ask for a runner it does not have, rather
    than working around a missing one. Widening any field here is a capability
    expansion, so `repo_policy` apply returns `pending_approval` and the change waits
    on an operator; narrowing applies through the same pipeline as a restriction.

    A runner is a bare executable basename resolved through the constrained runtime
    PATH -- never a path, never a shell string. `adhoc_runners` never grants shell
    syntax on its own, but an operator could still separately allowlist `bash`/`sh`
    there; RepoForge content-inspects `git` argv only, so under any shell runner the
    git guards do not see what the shell runs (`workspace_verify`/`workspace_exec`
    report `content_inspected = false`). `adhoc_shell_runners` is the reviewed,
    narrower path for the same capability: a separate, empty-by-default allowlist of
    interpreters `workspace_exec`'s `script` form may use, kept distinct from
    `adhoc_runners` precisely so enabling it is its own explicit, auditable decision.
    """

    execution_mode: Literal["strict", "relaxed"] | None = None
    adhoc_runners: tuple[_AdhocRunnerName, ...] | None = Field(
        default=None, max_length=_MAX_ADHOC_RUNNERS
    )
    adhoc_shell_runners: tuple[_AdhocRunnerName, ...] | None = Field(
        default=None, max_length=_MAX_ADHOC_RUNNERS
    )
    adhoc_timeout_seconds: int | None = Field(default=None, ge=1, le=_MAX_ADHOC_TIMEOUT_SECONDS)


class GeneratedPathDeclaration(StrictModel):
    glob: str = Field(min_length=1, max_length=512)
    regeneration_command: tuple[str, ...] = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)


class IssueWritePolicyDeclaration(StrictModel):
    enabled_ops: tuple[Literal["comment", "close", "reopen", "link", "create", "update"], ...] = (
        Field(default=("comment",), max_length=6)
    )
    approval_required_ops: tuple[
        Literal["comment", "close", "reopen", "link", "create", "update"], ...
    ] = Field(default=(), max_length=6)
    operation_semantics_version: Literal[1, 2] = 1
    max_writes_per_call: int = Field(default=2, ge=1, le=20)
    max_writes_per_window: int = Field(default=20, ge=1, le=10_000)
    window_seconds: int = Field(default=3_600, ge=60, le=604_800)
    create_title_prefix: str = Field(default="[TASK]", min_length=1, max_length=80)
    create_body_template: str = Field(
        default="## Objective\n{body}\n\n## Evidence\n{evidence_ref}",
        min_length=1,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def validate_policy(self) -> IssueWritePolicyDeclaration:
        if len(set(self.enabled_ops)) != len(self.enabled_ops):
            raise ValueError("issue_writes enabled_ops contains duplicates")
        if len(set(self.approval_required_ops)) != len(self.approval_required_ops):
            raise ValueError("issue_writes approval_required_ops contains duplicates")
        if not set(self.approval_required_ops).issubset(self.enabled_ops):
            raise ValueError("issue_writes approval_required_ops must be enabled")
        if self.max_writes_per_call > self.max_writes_per_window:
            raise ValueError("issue_writes per-call limit cannot exceed the window limit")
        try:
            template_fields = {
                field_name
                for _, field_name, _, _ in Formatter().parse(self.create_body_template)
                if field_name is not None
            }
        except ValueError as exc:
            raise ValueError(
                "issue_writes create_body_template is not a valid format template"
            ) from exc
        if template_fields != {"body", "evidence_ref"}:
            raise ValueError(
                "issue_writes create_body_template must contain exactly body and evidence_ref"
            )
        return self


class RepoPolicyInput(StrictModel):
    repo_id: RepoId
    action: PolicyAction
    mutations: tuple[PolicyMutation, ...] = Field(default=(), max_length=100)
    generated_paths: tuple[GeneratedPathDeclaration, ...] = Field(default=(), max_length=64)
    issue_writes: IssueWritePolicyDeclaration | None = None
    execution: ExecutionPolicyDeclaration | None = None
    preview_token: str | None = Field(default=None, max_length=2048)


class RepoPolicyOutput(ToolResponse):
    repo_id: RepoId
    action: PolicyAction
    result: Literal["preview", "applied", "pending_approval", "no_change"]
    preview_token: str | None = Field(default=None, max_length=2048)
    generation: int | None = Field(default=None, ge=1)
    changes: tuple[PolicyMutation, ...] = Field(default=(), max_length=100)
    generated_paths: tuple[GeneratedPathDeclaration, ...] = Field(default=(), max_length=64)
    issue_writes: IssueWritePolicyDeclaration | None = None
    execution: ExecutionPolicyDeclaration | None = None
    operator_instruction: str | None = Field(default=None, max_length=1000)


class WorkspaceCreateInput(AuthSelectionInput):
    repo_id: RepoId
    task_slug: str = Field(min_length=1, max_length=160)
    base: GitRef | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=256)
    issue_ids: tuple[str, ...] = Field(default=(), max_length=100)
    adopt_branch: GitRef | None = Field(
        default=None,
        description=(
            "Work directly ON this existing branch instead of cutting a fresh ai/* one. "
            "Use it when the instruction names a branch already in progress. Mutually "
            "exclusive with `base` (there is nothing to branch from). The response carries "
            "a warning because isolation is reduced: commits land on a branch this "
            "workspace does not exclusively own, and workspace_remove will never delete "
            "it. A protected branch is still refused."
        ),
    )
    attach_branch: GitRef | None = Field(
        default=None,
        description=(
            "Attach to a worktree this repository's own git already tracks with this "
            "branch checked out -- typically the operator's primary checkout -- instead of "
            "creating or adopting anything. Mutually exclusive with `base`, "
            "`adopt_branch`, and `attach_checkout_alias`. Creates no branch, worktree, or "
            "file. Reattaching the same checkout returns the same workspace_id. Fails with "
            "actionable evidence if the branch is not checked out anywhere, is checked out "
            "in more than one worktree, the checkout is missing on disk, or the branch is "
            "protected."
        ),
    )
    attach_checkout_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description=(
            "Attach to a checkout the operator has explicitly registered under this alias "
            "(never a path -- only the operator, through repository configuration, can "
            "make a path selectable at all). Use this for a checkout outside "
            "workspace_root that isn't a worktree of this repository's own primary "
            "checkout, e.g. a separate clone. Mutually exclusive with `base`, "
            "`adopt_branch`, and `attach_branch`. Creates no branch, worktree, or file. "
            "Fails with actionable evidence if the alias is not registered, the checkout "
            "is missing, it is not the same repository, it is in detached HEAD state, or "
            "its branch is protected."
        ),
    )


class WorkspaceCreateOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    repo_id: RepoId
    branch: str = Field(min_length=1, max_length=512)
    base: GitRef
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    issue_ids: tuple[str, ...] = Field(default=(), max_length=100)
    adopted_branch: bool = Field(
        default=False,
        description="True when `branch` is a pre-existing branch this workspace adopted.",
    )
    attached: bool = Field(
        default=False,
        description=(
            "True when this workspace operates on an existing checkout it attached to "
            "rather than one it created or adopted a branch into."
        ),
    )
    warnings: tuple[str, ...] = Field(
        default=(),
        max_length=20,
        description=(
            "Non-blocking advisories about reduced guarantees, e.g. an adopted or attached "
            "branch. The call succeeded; these are conditions the caller should carry "
            "forward."
        ),
    )


class WorkspaceRemoveInput(StrictModel):
    workspace_id: Identifier
    delete_local_branch: bool = False


class WorkspaceRemoveOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    removed: bool
    local_branch_deleted: bool
    remote_untouched: bool = True
    tombstone: str = Field(min_length=1, max_length=1000)


class WorkspaceListInput(StrictModel):
    exists: bool | None = True
    lifecycle: str | None = Field(default=None, max_length=80)
    repo_id: RepoId | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Cursor | None = None


class WorkspaceListOutput(ToolResponse):
    workspaces: tuple[WorkspaceSummary, ...] = Field(default=(), max_length=100)
    cleanup_guidance: tuple[str, ...] = Field(default=(), max_length=100)
    truncated: bool = False
    next_cursor: Cursor | None = None


class RefreshAction(str, Enum):
    PREVIEW = "preview"
    APPLY = "apply"
    RECREATE_FROM_LATEST_BASE = "recreate_from_latest_base"


class RefreshResolution(StrictModel):
    path: RelativePath
    strategy: Literal["content", "ours", "theirs"] = Field(
        default="content",
        description=(
            "`content` writes the text supplied in `content`. `ours` and `theirs` keep one "
            "side of the conflict whole, read back from the commits the plan is bound to, so "
            "you never have to echo a whole file to say 'keep mine' -- the conflict evidence "
            "in a preview is clipped to a byte budget and cannot be used for that."
        ),
    )
    content: str | None = Field(
        default=None,
        max_length=2_000_000,
        description=(
            "Required for strategy='content' and refused for the others: an entry carrying "
            "both a side-picking strategy and a body states two different intentions, and "
            "picking one for you is how a reviewed resolution becomes an unreviewed write."
        ),
    )


class RefreshConflictEvidence(StrictModel):
    path: RelativePath
    kind: Literal[
        "content",
        "add_add",
        "delete_modify",
        "rename_delete",
        "binary",
        "generated",
    ]
    base: str | None = Field(default=None, max_length=60_000)
    ours: str | None = Field(default=None, max_length=60_000)
    theirs: str | None = Field(default=None, max_length=60_000)
    content_truncated: bool = False
    next_action: ShortText
    regeneration_command: tuple[str, ...] = Field(default=(), max_length=64)


class RefreshRegenerationReceipt(StrictModel):
    commands: tuple[tuple[str, ...], ...] = Field(max_length=64)
    generated_paths: tuple[RelativePath, ...] = Field(max_length=1100)
    source_identity: Sha256
    output_identity: Sha256
    deterministic: Literal[True] = True


class RefreshChangeMetrics(StrictModel):
    changed_files: int = Field(default=0, ge=0, le=1100)
    added_lines: int = Field(default=0, ge=0)
    deleted_lines: int = Field(default=0, ge=0)
    binary_files: int = Field(default=0, ge=0, le=1100)
    total_current_bytes: int = Field(default=0, ge=0)


class WorkspaceRefreshInput(AuthSelectionInput):
    workspace_id: Identifier
    action: RefreshAction
    expected_head_sha: GitObjectId
    expected_fingerprint: Sha256
    plan_token: str | None = Field(default=None, max_length=2048)
    resolutions: tuple[RefreshResolution, ...] = Field(default=(), max_length=100)


class WorkspaceRefreshOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    action: RefreshAction
    result: Literal["current", "preview", "applied", "conflict", "recreated"]
    plan_hash: Sha256
    plan_token: str | None = Field(default=None, max_length=2048)
    target_base_sha: GitObjectId
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    prediction_scope: Literal["committed_head", "latest_base_recreate"] = "committed_head"
    apply_blockers: tuple[str, ...] = Field(default=(), max_length=20)
    conflicts: tuple[RefreshConflictEvidence, ...] = Field(default=(), max_length=1100)
    conflict_scope: Literal["none", "semantic", "generated", "mixed"] = "none"
    semantic_conflict_count: int = Field(default=0, ge=0, le=100)
    generated_conflict_count: int = Field(default=0, ge=0, le=1000)
    semantic_conflict_paths: tuple[RelativePath, ...] = Field(default=(), max_length=100)
    generated_conflict_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    regeneration_receipts: tuple[RefreshRegenerationReceipt, ...] = Field(default=(), max_length=64)
    source_change_metrics: RefreshChangeMetrics = Field(default_factory=RefreshChangeMetrics)
    generated_change_metrics: RefreshChangeMetrics = Field(default_factory=RefreshChangeMetrics)
    warnings: tuple[str, ...] = Field(default=(), max_length=100)
    changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1100)
    verify_selector: tuple[RelativePath, ...] = Field(default=(), max_length=1100)
    invalidated_receipts: tuple[str, ...] = Field(default=(), max_length=100)
    transaction_id: Identifier | None = None
    recreate_eligible: bool = False
    recreate_blockers: tuple[str, ...] = Field(default=(), max_length=20)
    recommended_action: Literal[
        "continue",
        "restore_remote_connectivity",
        "recreate_from_latest_base",
        "refresh_preview",
    ] = "refresh_preview"
    previous_head_sha: GitObjectId | None = None


class WorkspaceStatusSection(str, Enum):
    LOCAL = "local"
    BASE = "base"
    HYGIENE = "hygiene"


class StatusReadConsistency(str, Enum):
    """Whether a status read held the workspace lock or read alongside a running command."""

    LOCKED = "locked"
    CONCURRENT_WRITE = "concurrent_write"


class StatusSectionEvidence(StrictModel):
    section: WorkspaceStatusSection
    freshness: Freshness
    facts: tuple[KeyValue, ...] = Field(default=(), max_length=200)
    violations: tuple[str, ...] = Field(default=(), max_length=200)


class WorkspaceStatusInput(StrictModel):
    workspace_id: Identifier
    sections: tuple[WorkspaceStatusSection, ...] = Field(
        default=(WorkspaceStatusSection.LOCAL,), min_length=1, max_length=3
    )
    byte_budget: ByteBudget = 120_000


class WorkspaceStatusOutput(ToolResponse):
    workspace_id: Identifier
    repo_id: RepoId
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    clean: bool
    sections: tuple[StatusSectionEvidence, ...] = Field(default=(), max_length=3)
    fingerprint_source: Literal["cache", "scan"]
    truncated: bool = False
    kind: Literal["managed_worktree", "adopted_worktree", "attached_shared"] = Field(
        description=(
            "managed_worktree: RepoForge created both the branch and the worktree. "
            "adopted_worktree: an existing branch, worktree created by RepoForge. "
            "attached_shared: an operator-owned checkout RepoForge did not create; "
            "concurrent drift is observed, not refused."
        )
    )
    read_consistency: StatusReadConsistency = StatusReadConsistency.LOCKED


class WorkspaceFormatChangedInput(StrictModel):
    workspace_id: Identifier
    expected_fingerprint: Sha256
    formatter_id: Identifier | None = None


class FormatterEvidence(StrictModel):
    formatter_id: Identifier
    selected_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    outcome: Literal["passed", "changed", "failed", "no_op"]


class WorkspaceFormatChangedOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    formatters: tuple[FormatterEvidence, ...] = Field(default=(), max_length=100)
    changed: bool
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    execution_evidence: ExecutionEvidenceModel | None = None


class WorkspaceReadInput(StrictModel):
    workspace_id: Identifier
    files: tuple[ReadFileRequest, ...] = Field(min_length=1, max_length=20)
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class WorkspaceReadOutput(ToolResponse):
    workspace_id: Identifier
    files: tuple[ReadFileResult, ...] = Field(default=(), max_length=20)
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    truncated: bool = False
    next_cursor: Cursor | None = None


class WorkspaceSearchInput(StrictModel):
    workspace_id: Identifier
    query: str = Field(min_length=1, max_length=4000)
    mode: SearchMode = SearchMode.LITERAL
    path_glob: str | None = Field(default=None, max_length=4096)
    max_results: int = Field(default=100, ge=1, le=200)
    context_lines: int = Field(default=0, ge=0, le=5)
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class WorkspaceSearchOutput(ToolResponse):
    workspace_id: Identifier
    mode: SearchMode
    matches: tuple[SearchMatch, ...] = Field(default=(), max_length=200)
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    omitted_count: int = Field(default=0, ge=0)
    source_truncated: bool = False
    truncated: bool = False
    next_cursor: Cursor | None = None
    truncation_reason: (
        Literal[
            "search_deadline_exceeded",
            "result_count_limit",
            "result_transport_budget",
            "source_limit",
        ]
        | None
    ) = None
    scanned_path_count: int = Field(default=0, ge=0)
    candidate_path_count: int = Field(default=0, ge=0)
    remaining_path_count: int = Field(default=0, ge=0)
    completed_providers: tuple[str, ...] = Field(default=(), max_length=10)
    recommended_scope: ShortText | None = None


class WorkspaceTreeInput(StrictModel):
    workspace_id: Identifier
    subtree: RelativePath | None = None
    max_entries: int = Field(default=500, ge=1, le=2000)
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class WorkspaceTreeOutput(ToolResponse):
    workspace_id: Identifier
    subtree: RelativePath | None = None
    entries: tuple[TreeEntry, ...] = Field(default=(), max_length=2000)
    omitted_count: int = Field(default=0, ge=0)
    source_truncated: bool = False
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    truncated: bool = False
    next_cursor: Cursor | None = None


class WorkspaceDiffInput(StrictModel):
    workspace_id: Identifier
    staged: bool = False
    path_glob: str | None = Field(default=None, max_length=4096)
    max_files: int = Field(default=20, ge=1, le=1000)
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None
    include_hunks: bool = Field(
        default=False,
        description=(
            "Include the patch text. Off by default: reviewing a diff is the largest "
            "consumer of response budget by far, and the file list with per-file "
            "added/deleted counts answers most questions at a fraction of the size. Turn "
            "it on -- ideally with `path_glob` narrowing to the files you actually need -- "
            "when you need the hunks themselves. Mirrors `include_patch` on repo_history."
        ),
    )


class WorkspaceDiffOutput(ToolResponse):
    workspace_id: Identifier
    staged: bool
    # Empty `hunks` is ambiguous on its own -- a binary file, a pure rename, or patch text
    # the caller did not ask for all look alike -- so the served shape is stated.
    hunks_included: bool = False
    files: tuple[DiffFile, ...] = Field(default=(), max_length=1000)
    change_metrics: ChangeMetrics
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    omitted_count: int = Field(default=0, ge=0)
    source_truncated: bool = False
    truncated: bool = False
    next_cursor: Cursor | None = None


class TextReplacementOperation(StrictModel):
    old_text: LongText
    new_text: str = Field(max_length=120_000)
    expected_occurrences: int = Field(default=1, ge=1, le=1000)


class ReplaceTextOperation(StrictModel):
    op: Literal["replace_text"]
    path: RelativePath
    expected_sha256: Sha256
    edits: tuple[TextReplacementOperation, ...] = Field(min_length=1, max_length=20)


class WriteOperation(StrictModel):
    op: Literal["write"]
    path: RelativePath
    expected_sha256: Sha256
    content: str = Field(max_length=2_000_000)


class CreateOperation(StrictModel):
    op: Literal["create"]
    path: RelativePath
    content: str = Field(max_length=2_000_000)
    mode: int = Field(default=0o644, ge=0, le=0o777)


class DeleteOperation(StrictModel):
    op: Literal["delete"]
    path: RelativePath
    expected_sha256: Sha256


class MoveOperation(StrictModel):
    op: Literal["move"]
    source: RelativePath
    destination: RelativePath
    expected_source_sha256: Sha256


class ApplyPatchOperation(StrictModel):
    op: Literal["apply_patch"]
    patch: LongText


class RestorePathExpectation(StrictModel):
    path: RelativePath
    # None asserts the caller expects this path to currently be absent from the working
    # tree; any other value must match the working tree's actual current content hash --
    # restore is a full-content overwrite from HEAD, so this is the same proof-of-current-
    # state guard every other destructive mutation (write/delete/move/replace_text)
    # already requires.
    expected_sha256: Sha256 | None = None


class RestoreOperation(StrictModel):
    op: Literal["restore"]
    entries: tuple[RestorePathExpectation, ...] = Field(min_length=1, max_length=100)


MutationOperation = Annotated[
    ReplaceTextOperation
    | WriteOperation
    | CreateOperation
    | DeleteOperation
    | MoveOperation
    | ApplyPatchOperation
    | RestoreOperation,
    Field(discriminator="op"),
]


class WorkspaceMutateInput(StrictModel):
    workspace_id: Identifier
    operations: tuple[MutationOperation, ...] = Field(min_length=1, max_length=100)
    expected_head_sha: GitObjectId
    expected_workspace_fingerprint: Sha256
    dry_run: bool = False
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


class MutationDiagnostic(StrictModel):
    index: int = Field(ge=0, le=99)
    op: Literal["replace_text", "write", "create", "delete", "move", "apply_patch", "restore"]
    path: str | None = Field(default=None, max_length=8192)
    status: Literal["ready", "no_op", "failed"]
    candidate_context: str | None = Field(default=None, max_length=4000)
    before_sha256: Sha256 | None = None
    after_sha256: Sha256 | None = None
    changed: bool
    failure_reason: str | None = Field(default=None, max_length=1000)
    repair_actions: tuple[str, ...] = Field(default=(), max_length=20)


class SyntaxDiagnosticState(str, Enum):
    OK = "ok"
    ERROR = "error"
    UNKNOWN = "unknown"


class SyntaxDiagnosticSeverity(str, Enum):
    ERROR = "error"


class SyntaxDiagnosticItem(StrictModel):
    path: RelativePath
    line: int = Field(ge=1, le=10_000_000)
    message: str = Field(min_length=1, max_length=500)
    severity: SyntaxDiagnosticSeverity


class SyntaxDiagnosticsEvidence(StrictModel):
    state: SyntaxDiagnosticState
    parse_ok: bool | None
    diagnostics: tuple[SyntaxDiagnosticItem, ...] = Field(default=(), max_length=100)
    analyzed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    unknown_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    truncated: bool = False
    legacy_receipt: bool = False

    @model_validator(mode="after")
    def validate_state(self) -> SyntaxDiagnosticsEvidence:
        if self.state is SyntaxDiagnosticState.OK:
            if (
                self.parse_ok is not True
                or self.diagnostics
                or self.unknown_paths
                or self.legacy_receipt
            ):
                raise ValueError("ok syntax evidence must be complete and error-free")
        elif self.state is SyntaxDiagnosticState.ERROR:
            if self.parse_ok is not False or not self.diagnostics or self.legacy_receipt:
                raise ValueError("error syntax evidence requires diagnostics")
        elif (
            self.parse_ok is not None
            or self.diagnostics
            or (not self.unknown_paths and not self.legacy_receipt)
        ):
            raise ValueError(
                "unknown syntax evidence requires unresolved paths or legacy provenance"
            )
        return self


class WorkspaceFreshnessPreflightEvidence(StrictModel):
    staleness: Literal[
        "current",
        "unavailable_remote",
        "local_base_stale",
        "remote_base_stale",
        "diverged",
    ]
    refresh_required: bool
    workspace_base_sha: GitObjectId
    latest_base_sha: GitObjectId
    head_sha: GitObjectId
    ahead_base: int = Field(ge=0)
    behind_base: int = Field(ge=0)
    upstream_changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    workspace_changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    overlap_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    generated_overlap_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    expected_evidence_invalidation: tuple[ShortText, ...] = Field(default=(), max_length=20)
    verify_selector: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    recommended_action: Literal[
        "continue",
        "restore_remote_connectivity",
        "recreate_from_latest_base",
        "refresh_preview",
    ]
    warning: ShortText | None = None
    recreate_eligible: bool
    recreate_blockers: tuple[ShortText, ...] = Field(default=(), max_length=20)
    remote_available: bool
    remote_error_code: str | None = Field(default=None, max_length=160)


class WorkspaceConcurrentObservationEvidence(StrictModel):
    """Present only for an attached_shared workspace (#374) whose HEAD or fingerprint no
    longer matched what the caller expected. The mutation still proceeded -- each
    operation's own expected_sha256 is the guard against a genuine collision with the
    specific file it targets; this is a report of what else changed, not a refusal."""

    expected_head_sha: GitObjectId | None
    observed_head_sha: GitObjectId
    expected_workspace_fingerprint: Sha256
    observed_workspace_fingerprint: Sha256
    dirty_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    untracked_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)


class WorkspaceMutateOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    dry_run: bool
    ready: bool
    changed: bool
    would_change: bool
    operation_count: int = Field(ge=1, le=100)
    operations: tuple[MutationDiagnostic, ...] = Field(default=(), max_length=100)
    changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    diff_stat: str = Field(default="", max_length=20_000)
    change_metrics: ChangeMetrics
    syntax_diagnostics: SyntaxDiagnosticsEvidence
    transaction_id: Identifier | None = None
    freshness_preflight: WorkspaceFreshnessPreflightEvidence | None = None
    concurrent_observation: WorkspaceConcurrentObservationEvidence | None = None


class VerifyMode(str, Enum):
    PLAN = "plan"
    AUTO = "auto"
    DIAGNOSTIC = "diagnostic"
    PROFILE = "profile"
    ADHOC = "adhoc"


class VerifyPlanAction(str, Enum):
    """Sub-action within `workspace_verify.mode = "plan"`.

    `PREVIEW` (the default) keeps today's read-only assessment-and-recommendations
    behavior. `CREATE`/`ACCEPT`/`EXECUTE` drive the immutable multi-stage execution
    plan lifecycle without adding a 29th tool to the static Forge v2 surface."""

    PREVIEW = "preview"
    CREATE = "create"
    ACCEPT = "accept"
    EXECUTE = "execute"


class ExecutionPlanStageEvidence(StrictModel):
    stage_id: Identifier
    kind: Literal["diagnostic", "profile"]
    target: str = Field(min_length=1, max_length=4096)
    selector: str | None = Field(default=None, max_length=256)
    dependencies: tuple[Identifier, ...] = Field(default=(), max_length=64)
    boundary: Literal["iteration", "final"]
    working_directory: RelativePath | None = None
    timeout_seconds: int = Field(ge=1)
    mutability: Literal["read_only", "workspace_write"]
    network_policy: str = Field(min_length=1, max_length=80)
    failure_policy: Literal["required", "optional"]
    artifact_paths: tuple[RelativePath, ...] = Field(default=(), max_length=100)


class ExecutionPlanEvidence(StrictModel):
    plan_id: Identifier
    plan_hash: Sha256
    task_id: Identifier | None = None
    ordered_stages: tuple[ExecutionPlanStageEvidence, ...] = Field(default=(), max_length=64)
    final_profile: Identifier
    stage_definition_hash: Sha256
    created_at: str = Field(min_length=1, max_length=80)
    expires_at: str | None = Field(default=None, max_length=80)
    accepted: bool
    acceptance_id: Identifier | None = None


class VerifyIntent(str, Enum):
    TDD_RED = "tdd_red"
    TDD_GREEN = "tdd_green"
    REFACTOR = "refactor"
    PRE_COMMIT = "pre_commit"
    FINAL = "final"


class VerifyExpectation(str, Enum):
    NONE = "none"
    PASS = "pass"
    FAIL = "fail"


class VerifyRecommendationEvidence(StrictModel):
    order: int = Field(ge=1, le=32)
    kind: Literal["diagnostic", "profile"]
    reason: str = Field(min_length=1, max_length=1000)
    diagnostic_id: Identifier | None = None
    profile_name: Identifier | None = None
    selector: str | None = Field(default=None, max_length=4096)


class VerifyStepEvidence(StrictModel):
    id: Identifier
    kind: Literal[
        "unknown",
        "hygiene",
        "static_analysis",
        "typecheck",
        "business_tests",
        "security",
        "contract",
        "build",
    ]
    status: Literal["completed", "failed", "not_run"]
    duration_ms: float | None = Field(default=None, ge=0)
    cumulative_duration_ms: float | None = Field(default=None, ge=0)
    failure_domain: str | None = Field(default=None, max_length=160)


class WorkspaceFreshnessPreflightValue(StrictModel):
    staleness: Literal[
        "current",
        "unavailable_remote",
        "local_base_stale",
        "remote_base_stale",
        "diverged",
    ]
    refresh_required: bool
    workspace_base_sha: GitObjectId
    latest_base_sha: GitObjectId
    head_sha: GitObjectId
    ahead_base: int = Field(ge=0)
    behind_base: int = Field(ge=0)
    upstream_changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    workspace_changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    overlap_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    generated_overlap_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    expected_evidence_invalidation: tuple[Identifier, ...] = Field(default=(), max_length=32)
    verify_selector: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    recommended_action: Literal[
        "continue",
        "restore_remote_connectivity",
        "recreate_from_latest_base",
        "refresh_preview",
    ]
    warning: str | None = Field(default=None, max_length=2000)
    recreate_eligible: bool
    recreate_blockers: tuple[Identifier, ...] = Field(default=(), max_length=32)
    remote_available: bool
    remote_error_code: str | None = Field(default=None, max_length=128)


class WorkspaceFreshnessEvidence(StrictModel):
    status: Literal["current", "partial", "unavailable", "not_applicable"]
    coverage: Literal["complete", "partial", "none"]
    value: WorkspaceFreshnessPreflightValue | None = None
    error_code: str | None = Field(default=None, max_length=128)
    safe_fallback: str | None = Field(default=None, max_length=1000)


class WorkspaceVerifyAssessment(StrictModel):
    snapshot_id: Sha256
    current: bool
    changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    risk_score: int = Field(ge=0, le=100)
    risk_level: Literal["low", "medium", "high", "critical"]
    uncertainties: tuple[str, ...] = Field(default=(), max_length=64)
    refresh_required: bool
    behind_base: int = Field(ge=0)
    base_freshness: WorkspaceFreshnessEvidence
    provider: ProviderEvidence | None = None
    final_profile: Identifier
    manual_review_required: bool
    evidence_coverage: tuple[KeyValue, ...] = Field(default=(), max_length=32)


_SelectorItem = Annotated[str, Field(min_length=1, max_length=4096)]
_SelectorItems = Annotated[tuple[_SelectorItem, ...], Field(max_length=100)]
_Selector = _SelectorItem | _SelectorItems

# An ad-hoc argv is bounded far more tightly than a selector, and the schema must say
# so: advertising a selector's 100x4096 here let a caller build a request the schema
# accepted and `domain.adhoc` then rejected, which reads as an arbitrary failure. These
# must equal MAX_ADHOC_ARGV_ELEMENTS and MAX_ADHOC_ARGV_ELEMENT_LENGTH; the contracts
# package deliberately imports no domain module, so a test pins the two together.
_MAX_ADHOC_ARGV_ELEMENTS = 32
_MAX_ADHOC_ARGV_ELEMENT_LENGTH = 512
_MAX_ADHOC_STDIN_LENGTH = 64_000
# These must equal domain.adhoc's MAX_ADHOC_SCRIPT_LENGTH and MAX_ADHOC_SEQUENCE_LENGTH,
# same reason as the pair above: the contracts package imports no domain module, so a
# test pins these together instead.
_MAX_ADHOC_SCRIPT_LENGTH = 64_000
_MAX_ADHOC_SEQUENCE_LENGTH = 8
_AdhocArgvItem = Annotated[str, Field(min_length=1, max_length=_MAX_ADHOC_ARGV_ELEMENT_LENGTH)]
_AdhocArgv = Annotated[
    tuple[_AdhocArgvItem, ...], Field(min_length=1, max_length=_MAX_ADHOC_ARGV_ELEMENTS)
]


class WorkspaceVerifyInput(StrictModel):
    workspace_id: Identifier
    mode: VerifyMode = VerifyMode.AUTO
    diagnostic_id: Identifier | None = None
    selector: _Selector | None = None
    selector2: _Selector | None = None
    profile_name: Identifier | None = None
    argv: tuple[_AdhocArgvItem, ...] | None = Field(
        default=None, max_length=_MAX_ADHOC_ARGV_ELEMENTS
    )
    working_directory: RelativePath | None = None
    stdin_text: str | None = Field(
        default=None,
        max_length=_MAX_ADHOC_STDIN_LENGTH,
        description=(
            "Optional standard input for a mode=adhoc command; omitted leaves the command "
            "with no input. May contain newlines, unlike an argv element. Input larger than "
            "the limit belongs in a workspace file the command reads."
        ),
    )
    expected_fingerprint: Sha256 | None = None
    expected_head_sha: GitObjectId | None = None
    mutability: Literal["read_only", "workspace"] = "read_only"
    background: bool = False
    intent: VerifyIntent = VerifyIntent.FINAL
    expectation: VerifyExpectation = VerifyExpectation.NONE
    expected_failure_class: Identifier | None = None
    force_rerun: bool = False
    rerun: Literal["failed"] | None = None
    impact_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    artifact_output_path: RelativePath | None = None
    plan_action: VerifyPlanAction = VerifyPlanAction.PREVIEW
    plan_id: Identifier | None = None
    plan_task_id: Identifier | None = None
    plan_expires_at: str | None = Field(default=None, max_length=80)
    plan_through: Literal["iteration", "full"] = "iteration"

    @model_validator(mode="after")
    def validate_mode_fields(self) -> WorkspaceVerifyInput:
        if self.mode is VerifyMode.DIAGNOSTIC and self.diagnostic_id is None:
            raise ValueError("diagnostic mode requires diagnostic_id")
        if self.mode is VerifyMode.ADHOC and not self.argv:
            raise ValueError("adhoc mode requires argv")
        if self.stdin_text is not None and self.mode is not VerifyMode.ADHOC:
            raise ValueError("stdin_text is only valid for mode=adhoc")
        if self.mutability == "workspace":
            if self.mode is not VerifyMode.ADHOC:
                raise ValueError("mutability='workspace' is only valid for mode=adhoc")
            if self.expected_head_sha is None or self.expected_fingerprint is None:
                raise ValueError(
                    "mutability='workspace' requires both expected_head_sha and "
                    "expected_fingerprint to bind the run to reviewed state"
                )
        if self.mode is VerifyMode.PLAN and (self.background or self.artifact_output_path):
            raise ValueError("plan mode is read-only")
        if self.background and self.artifact_output_path is not None:
            raise ValueError("background verification cannot write a synchronous artifact")
        if self.rerun is not None:
            if self.mode is not VerifyMode.DIAGNOSTIC or self.diagnostic_id is None:
                raise ValueError("rerun=failed requires diagnostic mode and diagnostic_id")
            if self.selector is not None or self.selector2 is not None:
                raise ValueError("rerun=failed restores the exact recorded selectors")
        if (
            self.expected_failure_class is not None
            and self.expectation is not VerifyExpectation.FAIL
        ):
            raise ValueError("expected_failure_class requires expectation=fail")
        if self.plan_action is not VerifyPlanAction.PREVIEW and self.mode is not VerifyMode.PLAN:
            raise ValueError("plan_action requires mode=plan")
        if (
            self.plan_action in {VerifyPlanAction.ACCEPT, VerifyPlanAction.EXECUTE}
            and self.plan_id is None
        ):
            raise ValueError(f"plan_action={self.plan_action.value} requires plan_id")
        if self.plan_action is VerifyPlanAction.PREVIEW and self.plan_id is not None:
            raise ValueError("plan_id is only valid for plan_action accept or execute")
        if self.plan_task_id is not None and self.plan_action not in {
            VerifyPlanAction.CREATE,
            VerifyPlanAction.ACCEPT,
        }:
            raise ValueError("plan_task_id is only valid for plan_action create or accept")
        if self.plan_expires_at is not None and self.plan_action is not VerifyPlanAction.CREATE:
            raise ValueError("plan_expires_at is only valid for plan_action create")
        if self.plan_through != "iteration" and self.plan_action is not VerifyPlanAction.EXECUTE:
            raise ValueError("plan_through is only valid for plan_action execute")
        return self


class FailureLocationEvidence(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    code: str | None = Field(default=None, min_length=1, max_length=64)


class AdhocEvidence(StrictModel):
    """Policy facts for a `mode="adhoc"` run, absent for every other mode.

    `content_inspected` is the field that keeps this surface honest: RepoForge
    content-inspects `git` argv only, so any other runner -- a shell above all -- runs
    without the guards that block force pushes and history rewrites. When it is false,
    the exact-state lock and `read_only_violation` are the whole safety story.
    """

    mutability: Literal["read_only", "workspace"]
    command_class: Literal["read_only", "mutating"] | None = None
    content_inspected: bool
    fingerprint_changed: bool
    read_only_violation: bool = Field(
        description=(
            "True when a command that was classified or declared read-only nonetheless "
            "changed the workspace fingerprint. Treat the run's own claim about what it "
            "touched as unreliable and re-read workspace_status."
        )
    )
    # A plain bounded string, not RelativePath: these paths come from parsing git status
    # output, and one unusual shape (a quoted rename, a non-UTF-8 name) must not cost the
    # caller the entire verify response.
    changed_paths: tuple[str, ...] = Field(default=(), max_length=200)
    changed_paths_truncated: bool = False
    network_policy: Literal["advisory_local_only"]
    verification_invalidated: bool = False


class WorkspaceVerifyOutput(ToolResponse):
    workspace_id: Identifier
    requested_mode: VerifyMode
    selected_mode: VerifyMode
    routing_reason: str = Field(min_length=1, max_length=1000)
    impact_evidence: ProviderEvidence | None = None
    assessment: WorkspaceVerifyAssessment | None = None
    recommendations: tuple[VerifyRecommendationEvidence, ...] = Field(default=(), max_length=32)
    staleness_warning: str | None = Field(default=None, max_length=1000)
    next_action: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "What to do with this response when it did not finish the work -- above all, "
            "the exact `operation` wait to issue for a background run. Present whenever "
            "the run continues after the call returns; null for a completed verify, "
            "whose outcome is the answer."
        ),
    )
    operation: OperationEvidence | None = None
    commands: tuple[CommandEvidence, ...] = Field(default=(), max_length=100)
    steps: tuple[VerifyStepEvidence, ...] = Field(default=(), max_length=100)
    failed_step: VerifyStepEvidence | None = None
    failure_domain: str | None = Field(default=None, max_length=160)
    business_tests_ran: bool = False
    valid_tdd_red_evidence: bool = False
    failure_reused: bool = False
    artifact_paths: tuple[RelativePath, ...] = Field(default=(), max_length=100)
    outcome: Literal["planned", "passed", "failed", "running", "fallback_full"]
    satisfies_commit_gate: bool
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    plan: ExecutionPlanEvidence | None = None
    execution_evidence: ExecutionEvidenceModel | None = None
    adhoc_evidence: AdhocEvidence | None = None
    failed_selectors: tuple[_SelectorItem, ...] = Field(default=(), max_length=100)
    output_artifact_reference: str | None = Field(
        default=None,
        pattern=r"^failure-output:[a-f0-9]{64}$",
    )
    failure_provider: (
        Literal["pytest", "unittest", "ruff", "mypy", "build", "schema", "custom"] | None
    ) = None
    selector_coverage: Literal["not_applicable", "complete", "partial", "unavailable"] = (
        "not_applicable"
    )
    selectors_unavailable_reason: (
        Literal[
            "output_unrecognized",
            "provider_not_supported",
            "selectors_truncated",
            "artifact_unavailable",
        ]
        | None
    ) = None
    failure_locations: tuple[FailureLocationEvidence, ...] = Field(default=(), max_length=100)
    output_artifact_status: Literal[
        "not_applicable",
        "available",
        "oversized",
        "persistence_failed",
        "source_truncated",
        "source_unavailable",
    ] = "not_applicable"
    failure_expectation: Literal["expected_red", "unexpected"] | None = None
    failure_chain_id: str | None = Field(
        default=None,
        pattern=r"^failure-chain-[a-f0-9]{24}$",
    )
    rerun_of_selectors: tuple[_SelectorItem, ...] = Field(default=(), max_length=100)


class WorkspaceExecInput(StrictModel):
    """First-class ad-hoc command execution (#376), superseding
    `workspace_verify(mode="adhoc")` for the run-a-command intent -- see
    docs/architecture/autonomy-policy-model.md §9. `workspace_verify` keeps
    `mode="adhoc"` working during a deprecation window rather than losing it in the
    same change; new callers should reach for this tool instead."""

    workspace_id: Identifier
    argv: tuple[_AdhocArgvItem, ...] | None = Field(
        default=None, min_length=1, max_length=_MAX_ADHOC_ARGV_ELEMENTS
    )
    argv_sequence: tuple[_AdhocArgv, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_ADHOC_SEQUENCE_LENGTH,
        description=(
            "A bounded ordered list of argv commands run in one call with fail-fast "
            "semantics (#443): execution stops at the first non-zero exit, and each "
            "element's exit code and truncated output are reported independently. "
            "Additive to the single-argv form, not a substitute for shell syntax -- "
            "each element still goes through the same content inspection a single "
            "argv command would. Mutually exclusive with argv and script."
        ),
    )
    script: str | None = Field(
        default=None,
        max_length=_MAX_ADHOC_SCRIPT_LENGTH,
        description=(
            "A reviewed multiline shell-script body (#377), run through the interpreter "
            "named in `shell`. Bounded like stdin_text, not like an argv element -- "
            "newlines are the whole point. Unlike argv, script content is never "
            "inspected for git forms: classify_adhoc_command only content-inspects "
            "when argv[0] == 'git' literally, so this does not remove a safety "
            "guarantee the argv form had either. Requires the repository to configure "
            "a non-empty adhoc_shell_runners allowlist. Mutually exclusive with argv "
            "and argv_sequence."
        ),
    )
    shell: Identifier | None = Field(
        default=None,
        description="The interpreter to run `script` with (e.g. 'sh', 'bash'); required with script.",
    )
    working_directory: RelativePath | None = None
    stdin_text: str | None = Field(
        default=None,
        max_length=_MAX_ADHOC_STDIN_LENGTH,
        description=(
            "Piped to the process's standard input. Never logged or echoed in audit "
            "records -- only its length is recorded."
        ),
    )
    expected_fingerprint: Sha256 | None = None
    expected_head_sha: GitObjectId | None = None
    mutability: Literal["read_only", "workspace"] = Field(
        default="read_only",
        description=(
            "Declares intent, not a guarantee: a command classified read_only that "
            "changes the tree anyway is reported as read_only_violation, not silently "
            "accepted. mutability='workspace' requires expected_head_sha and "
            "expected_fingerprint -- an exact-state lock, since a mutating run cannot be "
            "replayed against a workspace someone else already changed."
        ),
    )
    background: bool = False

    @model_validator(mode="after")
    def validate_command_form(self) -> WorkspaceExecInput:
        forms_set = sum(value is not None for value in (self.argv, self.argv_sequence, self.script))
        if forms_set != 1:
            raise ValueError("Exactly one of argv, argv_sequence, or script must be provided")
        if (self.script is None) != (self.shell is None):
            raise ValueError("shell is required with script, and only valid with script")
        if self.argv_sequence is not None and self.stdin_text is not None:
            raise ValueError(
                "stdin_text is not supported with argv_sequence -- each element runs "
                "with no input; use a single argv or script call, or write the input "
                "to a workspace file the sequence's commands can read"
            )
        if self.mutability == "workspace" and (
            self.expected_head_sha is None or self.expected_fingerprint is None
        ):
            raise ValueError(
                "mutability='workspace' requires expected_head_sha and expected_fingerprint"
            )
        return self


class WorkspaceExecOutput(ToolResponse):
    """Every command run through this tool is evidence-only: `satisfies_commit_gate`
    is always False, and a mutating run invalidates any prior verification receipt on
    this workspace -- generic shell evidence never becomes a typed commit/push/PR/
    verification receipt merely because it exited successfully."""

    workspace_id: Identifier
    outcome: Literal["passed", "failed", "running"]
    next_action: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "What to do when this response did not finish the work -- the exact "
            "`operation` wait to issue for a background run. Null once outcome is "
            "passed or failed."
        ),
    )
    operation: OperationEvidence | None = None
    commands: tuple[CommandEvidence, ...] = Field(
        default=(),
        max_length=_MAX_ADHOC_SEQUENCE_LENGTH,
        description=(
            "One entry for a single argv/script run; up to _MAX_ADHOC_SEQUENCE_LENGTH "
            "entries, in order, for argv_sequence (#443) -- fail-fast means this may be "
            "shorter than the requested sequence if an earlier element failed."
        ),
    )
    satisfies_commit_gate: bool = False
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    execution_evidence: ExecutionEvidenceModel | None = None
    adhoc_evidence: AdhocEvidence | None = None


class ShippingChangeLimits(StrictModel):
    max_changed_files: int = Field(ge=1)
    max_diff_lines: int = Field(ge=1)
    max_total_changed_bytes: int = Field(ge=1)


class ShippingChangeMetrics(StrictModel):
    changed_files: int = Field(ge=0)
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    diff_lines: int = Field(ge=0)
    binary_files: int = Field(ge=0)
    total_current_bytes: int = Field(ge=0)
    limits: ShippingChangeLimits
    within_limits: bool


class WorkspaceCommitInput(AuthSelectionInput):
    workspace_id: Identifier
    message: str = Field(min_length=1, max_length=1000)
    expected_head_sha: GitObjectId | None = None
    expected_fingerprint: Sha256 | None = None


class WorkspaceCommitOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    branch: str = Field(min_length=1, max_length=512)
    commit: str = Field(min_length=1, max_length=20_000)
    previous_head_sha: GitObjectId
    head_sha: GitObjectId
    committed: bool
    verified_profile: Identifier | None = None
    verification_fingerprint: Sha256
    change_metrics: ShippingChangeMetrics
    command_source_paths_committed: tuple[RelativePath, ...] = Field(default=(), max_length=100)


class WorkspacePushInput(AuthSelectionInput):
    workspace_id: Identifier
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=256)
    expected_remote_head: GitObjectId | None = None


class WorkspacePushOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    branch: str = Field(min_length=1, max_length=512)
    head_sha: GitObjectId
    remote: str = Field(min_length=1, max_length=160)
    remote_head_before: GitObjectId | None = None
    remote_head_after: GitObjectId
    pushed: bool
    retryable_rejection: bool = False
    output: str = Field(default="", max_length=12_000)


class WorkspacePrAction(str, Enum):
    CREATE_DRAFT = "create_draft"
    UPDATE = "update"
    COMMENT = "comment"
    WATCH = "watch"
    RECONCILE = "reconcile"


class PrIssueDisposition(str, Enum):
    CLOSES = "closes"
    ADVANCES = "advances"
    SUPERSEDES = "supersedes"
    RELATES = "relates"


class PrIssueDispositionInput(StrictModel):
    issue_number: int = Field(ge=1)
    disposition: PrIssueDisposition
    acceptance_evidence_ref: str = Field(min_length=1, max_length=1_000)


class PrIssueSnapshotEvidence(StrictModel):
    issue_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1_000)
    state: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2_000)
    acceptance_evidence_ref: str = Field(min_length=1, max_length=1_000)


class PrIssueCompletionEvidence(StrictModel):
    intent_complete: bool
    closes: tuple[int, ...] = Field(default=(), max_length=100)
    advances: tuple[int, ...] = Field(default=(), max_length=100)
    supersedes: tuple[int, ...] = Field(default=(), max_length=100)
    relates: tuple[int, ...] = Field(default=(), max_length=100)
    snapshots: tuple[PrIssueSnapshotEvidence, ...] = Field(default=(), max_length=100)


class PrIssueClosureResult(StrictModel):
    issue_number: int = Field(ge=1)
    result: str = Field(min_length=1, max_length=80)
    external_writes: int = Field(ge=0, le=20)
    marker: str = Field(min_length=1, max_length=200)
    approval_request_id: str | None = Field(default=None, max_length=160)
    operation_id: Identifier | None = None
    receipt_id: Identifier | None = None
    result_reference: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_durable_outcome_references(self) -> PrIssueClosureResult:
        references = (self.operation_id, self.receipt_id, self.result_reference)
        if any(value is not None for value in references) and not all(
            value is not None for value in references
        ):
            raise ValueError("closure durable outcome references must be complete")
        if self.result in {"applied", "reconciled"} and not all(
            value is not None for value in references
        ):
            raise ValueError("applied closure requires durable outcome references")
        return self


class PrIssueReconciliationEvidence(StrictModel):
    merge_status: Literal["merged", "not_merged"]
    closed_correctly: tuple[int, ...] = Field(default=(), max_length=100)
    implemented_still_open: tuple[int, ...] = Field(default=(), max_length=100)
    intentionally_advanced: tuple[int, ...] = Field(default=(), max_length=100)
    superseded: tuple[int, ...] = Field(default=(), max_length=100)
    acceptance_review_required: tuple[int, ...] = Field(default=(), max_length=100)
    closure_results: tuple[PrIssueClosureResult, ...] = Field(default=(), max_length=100)


class PrCommentEvidence(StrictModel):
    result: Literal["created", "reconciled"]
    url: str | None = Field(default=None, max_length=2_000)
    marker: str = Field(min_length=1, max_length=200)
    idempotent_replay: bool
    review_comment_id: int | None = Field(default=None, ge=1)


class WorkspacePrInput(AuthSelectionInput):
    workspace_id: Identifier
    action: WorkspacePrAction
    title: str | None = Field(default=None, max_length=1000)
    body: str | None = Field(default=None, max_length=60_000)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=1_000)
    review_comment_id: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=256)
    expected_remote_version: str | None = Field(default=None, min_length=1, max_length=256)
    until: Literal["all_completed", "first_failure"] = "all_completed"
    timeout_seconds: int = Field(default=900, ge=5, le=7200)
    event_cursor: Cursor | None = None
    issue_dispositions: tuple[PrIssueDispositionInput, ...] = Field(default=(), max_length=100)
    apply_closures: bool = False

    @model_validator(mode="after")
    def validate_action_fields(self) -> WorkspacePrInput:
        write_actions = {
            WorkspacePrAction.CREATE_DRAFT,
            WorkspacePrAction.UPDATE,
            WorkspacePrAction.COMMENT,
        }
        if self.action in write_actions and self.idempotency_key is None:
            raise ValueError(f"workspace_pr {self.action.value} requires idempotency_key")
        if (
            self.action is WorkspacePrAction.RECONCILE
            and self.apply_closures
            and (self.idempotency_key is None)
        ):
            raise ValueError("workspace_pr reconcile apply_closures requires idempotency_key")
        if self.action is WorkspacePrAction.CREATE_DRAFT and (
            self.title is None or self.body is None
        ):
            raise ValueError("workspace_pr create_draft requires title and body")
        if self.action is WorkspacePrAction.UPDATE and self.title is None and self.body is None:
            raise ValueError("workspace_pr update requires title or body")
        if self.action in {
            WorkspacePrAction.UPDATE,
            WorkspacePrAction.COMMENT,
            WorkspacePrAction.RECONCILE,
        } and (self.expected_remote_version is None):
            raise ValueError(f"workspace_pr {self.action.value} requires expected_remote_version")
        if self.action is WorkspacePrAction.COMMENT and (
            self.body is None or self.evidence_ref is None
        ):
            raise ValueError("workspace_pr comment requires body and evidence_ref")
        if self.action is not WorkspacePrAction.COMMENT and (
            self.evidence_ref is not None or self.review_comment_id is not None
        ):
            raise ValueError("comment fields are only valid for workspace_pr comment")
        if self.action is WorkspacePrAction.WATCH:
            self.reject_selector_on_read("workspace_pr watch")
            if any(value is not None for value in (self.title, self.body, self.idempotency_key)):
                raise ValueError("workspace_pr watch does not accept write fields")
            if self.event_cursor is None and self.expected_remote_version is None:
                raise ValueError(
                    "workspace_pr watch requires expected_remote_version when starting"
                )
            if self.event_cursor is not None and self.expected_remote_version is not None:
                raise ValueError("workspace_pr watch resume uses the version bound to event_cursor")
        if self.action is not WorkspacePrAction.WATCH and self.event_cursor is not None:
            raise ValueError("event_cursor is only valid for workspace_pr watch")
        if self.action not in {WorkspacePrAction.CREATE_DRAFT, WorkspacePrAction.UPDATE} and (
            self.issue_dispositions
        ):
            raise ValueError("issue_dispositions are only valid for PR create or update")
        issue_numbers = [item.issue_number for item in self.issue_dispositions]
        if len(issue_numbers) != len(set(issue_numbers)):
            raise ValueError("issue_dispositions must contain each issue exactly once")
        if self.action is WorkspacePrAction.RECONCILE and any(
            value is not None
            for value in (self.title, self.body, self.evidence_ref, self.review_comment_id)
        ):
            raise ValueError("workspace_pr reconcile does not accept PR content fields")
        if self.action is not WorkspacePrAction.RECONCILE and self.apply_closures:
            raise ValueError("apply_closures is only valid for workspace_pr reconcile")
        return self


class WorkspacePrOutput(ToolResponse):
    outcome: OutcomeReceiptEvidence | None = None
    workspace_id: Identifier
    action: WorkspacePrAction
    pull_request: PullRequestEvidence | None = None
    comment: PrCommentEvidence | None = None
    operation: OperationEvidence | None = None
    remote_version: str | None = Field(default=None, max_length=256)
    event_cursor: Cursor | None = None
    terminal_reason: str | None = Field(default=None, max_length=500)
    issue_completion: PrIssueCompletionEvidence | None = None
    reconciliation: PrIssueReconciliationEvidence | None = None


class PrEvidenceDetail(str, Enum):
    OVERVIEW = "overview"
    CHECK = "check"
    FAILURE = "failure"


class CheckEvidence(StrictModel):
    selector: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=500)
    status: Literal["pass", "fail", "pending", "skipped"]
    required: bool
    annotations: tuple[str, ...] = Field(default=(), max_length=200)


class WorkspacePrEvidenceInput(StrictModel):
    workspace_id: Identifier
    detail: PrEvidenceDetail = PrEvidenceDetail.OVERVIEW
    check_selector: str | None = Field(default=None, max_length=2048)
    since: Cursor | None = None
    max_excerpt_lines: int = Field(default=80, ge=1, le=200)

    @model_validator(mode="after")
    def validate_detail_fields(self) -> WorkspacePrEvidenceInput:
        if self.detail in {PrEvidenceDetail.CHECK, PrEvidenceDetail.FAILURE}:
            if self.check_selector is None:
                raise ValueError(
                    f"workspace_pr_evidence {self.detail.value} requires check_selector"
                )
        elif self.check_selector is not None:
            raise ValueError("check_selector is only valid for check or failure detail")
        return self


class WorkspacePrEvidenceOutput(ToolResponse):
    workspace_id: Identifier
    pull_request: PullRequestEvidence
    checks: tuple[CheckEvidence, ...] = Field(default=(), max_length=500)
    failure_excerpt: tuple[str, ...] = Field(default=(), max_length=200)
    failure_provider: (
        Literal["pytest", "unittest", "ruff", "mypy", "build", "schema", "custom"] | None
    ) = None
    selector_coverage: Literal["not_applicable", "complete", "partial", "unavailable"] = (
        "not_applicable"
    )
    selectors_unavailable_reason: (
        Literal[
            "output_unrecognized",
            "provider_not_supported",
            "selectors_truncated",
            "artifact_unavailable",
        ]
        | None
    ) = None
    failed_selectors: tuple[_SelectorItem, ...] = Field(default=(), max_length=100)
    failure_locations: tuple[FailureLocationEvidence, ...] = Field(default=(), max_length=100)
    output_artifact_reference: str | None = Field(
        default=None,
        pattern=r"^failure-output:[a-f0-9]{64}$",
    )
    output_artifact_status: Literal[
        "not_applicable",
        "available",
        "oversized",
        "persistence_failed",
        "source_truncated",
        "source_unavailable",
    ] = "not_applicable"
    remote_version: str = Field(min_length=1, max_length=256)
    delta_token: Cursor
    changed_since: bool
    truncated: bool = False


class OperationAction(str, Enum):
    GET = "get"
    WAIT = "wait"
    LIST = "list"
    CANCEL = "cancel"
    FAILURE_EVIDENCE = "failure_evidence"


class FailureEvidenceWorkspaceIdentity(StrictModel):
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    config_generation: Sha256
    policy_hash: Sha256


class RuntimeLogSource(str, Enum):
    AUDIT = "audit"
    RUNTIME = "runtime"
    FAILURE_ARTIFACT = "failure_artifact"


# Moved above the failure recovery-action union: `RuntimeLogsReadRecoveryAction`
# embeds this input model, and the union is a runtime expression, so the model must
# already exist by the time it is evaluated.


class RuntimeLogsReadInput(StrictModel):
    source: RuntimeLogSource = RuntimeLogSource.AUDIT
    limit: int = Field(default=50, ge=1, le=200)
    action: str | None = Field(default=None, max_length=160)
    only_failed: bool = False
    min_duration_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    start_time: str | None = Field(default=None, max_length=80)
    end_time: str | None = Field(default=None, max_length=80)
    cursor: Cursor | None = None
    artifact_reference: str | None = Field(
        default=None,
        pattern=r"^failure-output:[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def validate_time_range(self) -> RuntimeLogsReadInput:
        parsed: dict[str, datetime] = {}
        for field, value in (("start_time", self.start_time), ("end_time", self.end_time)):
            if value is None:
                continue
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
            if timestamp.tzinfo is None:
                raise ValueError(f"{field} must include a timezone offset")
            parsed[field] = timestamp
        if (
            "start_time" in parsed
            and "end_time" in parsed
            and parsed["start_time"] > parsed["end_time"]
        ):
            raise ValueError("start_time must not be after end_time")
        if self.source is RuntimeLogSource.FAILURE_ARTIFACT:
            if self.artifact_reference is None:
                raise ValueError("failure_artifact source requires artifact_reference")
            if (
                self.action is not None
                or self.only_failed
                or self.min_duration_ms is not None
                or self.start_time is not None
                or self.end_time is not None
            ):
                raise ValueError("failure_artifact source does not accept log filters")
        elif self.artifact_reference is not None:
            raise ValueError("artifact_reference is only valid for failure_artifact source")
        return self


class OperationRecoveryAction(StrictModel):
    kind: Literal["operation"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: OperationInput


class WorkspaceStatusRecoveryAction(StrictModel):
    kind: Literal["workspace_status"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: WorkspaceStatusInput


class WorkspaceVerifyRecoveryAction(StrictModel):
    kind: Literal["workspace_verify"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: WorkspaceVerifyInput


class WorkspaceRefreshRecoveryAction(StrictModel):
    kind: Literal["workspace_refresh"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: WorkspaceRefreshInput


class WorkspaceMutateRecoveryAction(StrictModel):
    kind: Literal["workspace_mutate"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: WorkspaceMutateInput


class ConfigInspectRecoveryAction(StrictModel):
    kind: Literal["config_inspect"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: ConfigInspectInput


class RuntimeLogsReadRecoveryAction(StrictModel):
    """Read the complete persisted stdout and stderr of the failing command.

    The excerpt on failure evidence is bounded, and a failure whose selectors could not
    be extracted is exactly the one whose full output the caller needs. This action names
    the retrieval so nobody has to re-run a suite to recover what was already recorded.
    """

    kind: Literal["runtime_logs_read"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: RuntimeLogsReadInput


FailureRecoveryAction = Annotated[
    OperationRecoveryAction
    | WorkspaceStatusRecoveryAction
    | WorkspaceVerifyRecoveryAction
    | WorkspaceRefreshRecoveryAction
    | WorkspaceMutateRecoveryAction
    | ConfigInspectRecoveryAction
    | RuntimeLogsReadRecoveryAction,
    Field(discriminator="kind"),
]


class FailureAffectedScope(StrictModel):
    paths: tuple[RelativePath, ...] = Field(default=(), max_length=100)
    tests: tuple[str, ...] = Field(default=(), max_length=100)
    symbols: tuple[str, ...] = Field(default=(), max_length=100)


class FailureEvidenceDetail(StrictModel):
    """One exact, private, content-addressed failure -- bounded, secret-redacted,
    restart-safe -- with normalized failure class, stable error code, exact
    pre/post identities, affected scope, and ordered typed recovery actions that
    never contain arbitrary command text."""

    failure_id: Identifier
    operation_id: Identifier
    plan_id: Identifier
    plan_hash: Sha256
    stage_id: Identifier
    receipt_id: Identifier | None = None
    pre_identity: FailureEvidenceWorkspaceIdentity
    post_identity: FailureEvidenceWorkspaceIdentity
    environment_identity: Sha256 | None = None
    compatibility_binding: Sha256
    failure_class: Literal[
        "tool_missing",
        "dependency_missing",
        "environment_mismatch",
        "configuration_invalid",
        "timeout",
        "cancelled",
        "lint_failure",
        "type_failure",
        "test_failure",
        "build_failure",
        "network_failure",
        "permission_failure",
        "policy_failure",
        "stale_workspace",
        "stale_plan",
        "unexpected_mutation",
        "provider_failure",
        "flaky_suspected",
        "unknown",
    ]
    stable_error_code: Identifier
    first_diagnostic: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(min_length=1, max_length=4_000)
    excerpt_sha256: Sha256
    excerpt_reference: str = Field(min_length=1, max_length=500)
    affected_scope: FailureAffectedScope
    reproducibility: Literal["reproducible", "intermittent", "unknown"]
    files_changed: bool
    retryable: bool
    confidence: int = Field(ge=0, le=100)
    uncertainty: tuple[str, ...] = Field(default=(), max_length=100)
    safe_actions: tuple[FailureRecoveryAction, ...] = Field(min_length=1, max_length=20)
    source_digest: Sha256
    created_at: str = Field(min_length=1, max_length=80)
    schema_version: int = Field(ge=1)


class OperationInput(StrictModel):
    action: OperationAction
    operation_id: Identifier | None = None
    scope: str | None = Field(default=None, max_length=300)
    state: OperationState | None = None
    expected_updated_at: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: Cursor | None = None
    failure_id: Identifier | None = None
    since_updated_at: str | None = Field(default=None, max_length=80)
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    until: Literal["progress", "terminal"] = Field(
        default="progress",
        description=(
            "What ends the wait. 'progress' returns on the next durable progress delta -- "
            "one per step start and completion -- so a multi-step gate wakes the caller "
            "many times. 'terminal' returns only when the operation finishes, or on "
            "timeout with the current evidence and a pacing hint; prefer it when the only "
            "question is the outcome."
        ),
    )

    @model_validator(mode="after")
    def validate_action_fields(self) -> OperationInput:
        if self.action in {OperationAction.GET, OperationAction.WAIT, OperationAction.CANCEL}:
            if self.operation_id is None:
                raise ValueError(f"operation {self.action.value} requires operation_id")
        elif self.operation_id is not None:
            raise ValueError("operation_id is only valid for get, wait, or cancel")
        if self.action is not OperationAction.LIST and any(
            value is not None for value in (self.scope, self.state, self.cursor)
        ):
            raise ValueError("scope, state, and cursor are only valid for operation list")
        if self.action is not OperationAction.CANCEL and self.expected_updated_at is not None:
            raise ValueError("expected_updated_at is only valid for operation cancel")
        if self.action is not OperationAction.WAIT and any(
            value is not None for value in (self.since_updated_at, self.timeout_seconds)
        ):
            raise ValueError(
                "since_updated_at and timeout_seconds are only valid for operation wait"
            )
        if self.action is not OperationAction.WAIT and self.until != "progress":
            raise ValueError("until is only valid for operation wait")
        if self.action is OperationAction.FAILURE_EVIDENCE and self.failure_id is None:
            raise ValueError("operation failure_evidence requires failure_id")
        if self.action is not OperationAction.FAILURE_EVIDENCE and self.failure_id is not None:
            raise ValueError("failure_id is only valid for operation failure_evidence")
        return self


class OperationOutput(ToolResponse):
    action: OperationAction
    operation: OperationEvidence | None = None
    operations: tuple[OperationEvidence, ...] = Field(default=(), max_length=200)
    cancellation_requested: bool = False
    truncated: bool = False
    next_cursor: Cursor | None = None
    failure_evidence: FailureEvidenceDetail | None = None
    changed_since: bool = False
    timed_out: bool = False
    progress_delivery: Literal["pushed", "poll"] | None = Field(
        default=None,
        description=(
            "Wait only. Which mechanism this wait actually used: 'pushed' means live "
            "progress notifications were sent on the open request, so a long wait is "
            "worth re-issuing; 'poll' means this client cannot consume notifications "
            "and should pace itself with suggested_poll_after_s instead."
        ),
    )
    next_since_updated_at: str | None = Field(
        default=None,
        max_length=80,
        description=(
            "Wait only. Pass back as since_updated_at to resume where this wait stopped. "
            "Null when the operation is terminal and there is nothing left to wait for."
        ),
    )
    suggested_poll_after_s: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Wait only. How long to wait before the next call. Present even when the "
            "wait returns no operation evidence, which is the case a caller would "
            "otherwise have nothing to pace from."
        ),
    )


class ConfigInspectInput(StrictModel):
    repo_id: RepoId | None = None
    include_pending: bool = True


class ConfigGenerationSummary(StrictModel):
    generation: int = Field(ge=1)
    state: Literal["accepted", "active", "pending", "rejected"]
    digest: Sha256
    changed_sections: tuple[str, ...] = Field(default=(), max_length=100)


class RuntimeContractIdentityView(StrictModel):
    server_build_sha: Sha256
    server_version: str = Field(min_length=1, max_length=160)
    active_generation: int = Field(ge=1)
    tool_surface_hash: Sha256
    input_contract_digest: Sha256
    output_contract_digest: Sha256
    runtime_protocol_version: int = Field(ge=1)
    process_start_identity: Sha256


class RuntimeHealthCheckView(StrictModel):
    """One named runtime health component and why it holds or does not."""

    name: ShortText
    ok: bool
    detail: ShortText


class RuntimeHealthView(StrictModel):
    """What the supervisor observes about the runtime NOW, not at activation.

    `RuntimeActivationEvidenceView.runtime_phase` is a word captured in a receipt when a
    release was activated; it can be hours old and it said `healthy` throughout the
    2026-07-28 incident, during which the connector was torn down twice. A caller asking
    whether the runtime is well needs the record the watchdog maintains, and needs to know
    how old that record is: the watchdog writes only on change, so `healthy` with no
    timestamp cannot be told apart from a watchdog that stopped running. `observed_age_seconds`
    is that distinction, and it is the field to read first.

    `restarts_total` and `last_restart_at` are evidence rather than policy -- unlike
    `restart_count`, which the restart policy resets after a stable interval, so an outage
    disappears from it sixty seconds later.
    """

    phase: ShortText
    observed_at: str | None = Field(default=None, min_length=1, max_length=80)
    observed_age_seconds: float | None = Field(default=None, ge=0.0)
    checks: tuple[RuntimeHealthCheckView, ...] = Field(default=(), max_length=50)
    restarts_total: int = Field(default=0, ge=0)
    last_restart_at: str | None = Field(default=None, min_length=1, max_length=80)
    consecutive_health_failures: int = Field(default=0, ge=0)
    last_error_code: ShortText | None = None


class RuntimeActivationEvidenceView(StrictModel):
    """What the durable activation receipt says, read back independently.

    An activation command reports its own outcome. That report is the one thing
    that cannot corroborate itself: a release has already been observed claiming
    `converged: true` minutes before its runtime turned out to be unusable. These
    facts come from the persisted receipt instead, and `agreement` compares them
    against the identity the live process actually advertises -- so a caller can
    tell a genuinely converged activation from one that only said so.
    """

    receipt_id: Identifier
    classification: ShortText
    target_generation: int = Field(ge=1)
    accepted_at: str = Field(min_length=1, max_length=80)
    updated_at: str = Field(min_length=1, max_length=80)
    effect_boundary_crossed: bool
    activated_generation: int | None = Field(default=None, ge=1)
    activated_tool_surface_hash: Sha256 | None = None
    activated_process_identity: Sha256 | None = None
    runtime_phase: ShortText | None = None
    error_code: ShortText | None = None
    # Deliberately NOT a comparison of process identity or tool-surface hash. Both
    # describe one process instance and one release, so both change legitimately -- on a
    # watchdog restart, or after a later release activation -- and comparing them reported
    # a healthy converged installation as diverged, which is worse than reporting nothing
    # (#314). Those changes are facts, not verdicts, so they are reported separately, and
    # every field below says so in its own description: the reasoning has to travel with
    # the schema, because a caller reading two booleans named "changed since activation"
    # with no description will call a healthy installation diverged. That is exactly what
    # #314 was, and it happened a second time while this text lived only in a comment.
    agreement: Literal["matches", "diverged", "unverifiable"] = Field(
        description=(
            "THE verdict on this activation, and the only field that is one. `matches`: the "
            "live runtime serves the configuration generation this receipt activated. "
            "`diverged`: it serves a different one -- the activation did not end where it "
            "claimed, or something else took over. `unverifiable`: nothing was recorded or "
            "nothing is observable, reported rather than defaulted to success. Compares the "
            "configuration generation only, never process identity or tool-surface hash."
        )
    )
    process_restarted_since_activation: bool | None = Field(
        default=None,
        description=(
            "A fact, not a fault: true when the live process is not the instance this "
            "receipt recorded. Expected after a watchdog restart or any later activation. "
            "Null when either identity is unknown. Read `agreement` for the verdict."
        ),
    )
    tool_surface_changed_since_activation: bool | None = Field(
        default=None,
        description=(
            "A fact, not a fault: true when the live tool surface differs from the one this "
            "receipt recorded, which is what a release upgrade after this activation looks "
            "like. Comparing it against a hash from an earlier activation will therefore "
            "differ by design. Null when either is unknown. Read `agreement` for the verdict."
        ),
    )


class ConfigProjectionView(StrictModel):
    source_digest: Sha256
    accepted_source_digest: Sha256
    accepted_resolved_digest: Sha256
    active_resolved_digest: Sha256 | None = None
    runtime_generation: int | None = Field(default=None, ge=1)
    drift_state: Literal["none", "source_changed", "activation_required", "runtime_mismatch"]
    safe_reconciliation_action: str = Field(min_length=1, max_length=500)


class TicketGraphProjectionView(StrictModel):
    enabled: bool
    root_issue: int | None = Field(default=None, ge=1)
    repository: str | None = Field(default=None, max_length=300)


class RepositoryConfigProjectionView(StrictModel):
    repo_id: RepoId
    source_digest: Sha256
    accepted_resolved_digest: Sha256
    active_resolved_digest: Sha256 | None = None
    accepted_generation: int = Field(ge=1)
    active_generation: int | None = Field(default=None, ge=1)
    source_ticket_graph: TicketGraphProjectionView
    accepted_ticket_graph: TicketGraphProjectionView
    active_ticket_graph: TicketGraphProjectionView
    capability_projection_status: Literal["active", "pending", "unavailable", "disabled"]
    drift_reason: Literal[
        "none",
        "source_not_refreshed",
        "pending_approval",
        "accepted_not_active",
        "projection_loss",
        "provider_unavailable",
        "intentionally_disabled",
    ]
    safe_reconciliation_action: str = Field(min_length=1, max_length=500)


class ConfigInspectOutput(ToolResponse):
    accepted: ConfigGenerationSummary | None = None
    active: ConfigGenerationSummary | None = None
    pending: tuple[ConfigGenerationSummary, ...] = Field(default=(), max_length=100)
    capability_delta: (
        Literal["equivalent", "metadata_only", "expansion", "restriction", "incompatible"] | None
    ) = None
    restart_required: bool
    repo_facts: tuple[KeyValue, ...] = Field(default=(), max_length=500)
    repository_projections: tuple[RepositoryConfigProjectionView, ...] = Field(
        default=(), max_length=100
    )
    contract_identity: RuntimeContractIdentityView | None = None
    config_projection: ConfigProjectionView | None = None
    activation_evidence: RuntimeActivationEvidenceView | None = None
    runtime_health: RuntimeHealthView | None = None


class RuntimeTimestampState(str, Enum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class RuntimeLogParseState(str, Enum):
    STRUCTURED_V1 = "structured_v1"
    LEGACY_JSON = "legacy_json"
    LEGACY_PLAINTEXT = "legacy_plaintext"
    MALFORMED_JSON = "malformed_json"


class RuntimeLogEntry(StrictModel):
    # Nullable on purpose. A log line whose timestamp cannot be read has an UNKNOWN time,
    # and stamping it 1970-01-01 turned every managed-runtime record into a confident lie
    # (observed: 100 entries, all 1970, all empty). Readers must treat null as "unknown".
    timestamp: str | None = Field(default=None, max_length=80)
    timestamp_state: RuntimeTimestampState | None = None
    parse_state: RuntimeLogParseState | None = None
    source: RuntimeLogSource
    component: str | None = Field(default=None, max_length=160)
    stream: str | None = Field(default=None, max_length=80)
    event_kind: str | None = Field(default=None, max_length=160)
    action: str | None = Field(default=None, max_length=160)
    level: str = Field(min_length=1, max_length=30)
    message: str = Field(max_length=4000)
    duration_ms: float | None = Field(default=None, ge=0)
    correlation_id: str | None = Field(default=None, max_length=160)
    operation_id: str | None = Field(default=None, max_length=160)
    receipt_id: str | None = Field(default=None, max_length=160)
    trace_id: str | None = Field(default=None, max_length=160)
    workspace_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    repository_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class RuntimeLogsReadOutput(ToolResponse):
    source: RuntimeLogSource
    entries: tuple[RuntimeLogEntry, ...] = Field(default=(), max_length=200)
    malformed_count: int = Field(default=0, ge=0, le=1_000)
    legacy_count: int = Field(default=0, ge=0, le=1_000)
    structured_count: int = Field(default=0, ge=0, le=1_000)
    correlated_count: int = Field(default=0, ge=0, le=1_000)
    timestamp_unavailable_count: int = Field(default=0, ge=0, le=1_000)
    source_truncated: bool | None = None
    truncated: bool = False
    next_cursor: Cursor | None = None


MODEL_PAIRS: tuple[tuple[str, type[StrictModel], type[ToolResponse]], ...] = (
    ("repo_task_context", RepoTaskContextInput, RepoTaskContextOutput),
    ("repo_read", RepoReadInput, RepoReadOutput),
    ("repo_search", RepoSearchInput, RepoSearchOutput),
    ("repo_tree", RepoTreeInput, RepoTreeOutput),
    ("repo_history", RepoHistoryInput, RepoHistoryOutput),
    ("repo_issue", RepoIssueInput, RepoIssueOutput),
    ("repo_pr_read", RepoPrReadInput, RepoPrReadOutput),
    ("repo_list", RepoListInput, RepoListOutput),
    ("repo_policy", RepoPolicyInput, RepoPolicyOutput),
    ("workspace_create", WorkspaceCreateInput, WorkspaceCreateOutput),
    ("workspace_remove", WorkspaceRemoveInput, WorkspaceRemoveOutput),
    ("workspace_list", WorkspaceListInput, WorkspaceListOutput),
    ("workspace_refresh", WorkspaceRefreshInput, WorkspaceRefreshOutput),
    ("workspace_status", WorkspaceStatusInput, WorkspaceStatusOutput),
    ("workspace_format_changed", WorkspaceFormatChangedInput, WorkspaceFormatChangedOutput),
    ("workspace_read", WorkspaceReadInput, WorkspaceReadOutput),
    ("workspace_search", WorkspaceSearchInput, WorkspaceSearchOutput),
    ("workspace_tree", WorkspaceTreeInput, WorkspaceTreeOutput),
    ("workspace_diff", WorkspaceDiffInput, WorkspaceDiffOutput),
    ("workspace_mutate", WorkspaceMutateInput, WorkspaceMutateOutput),
    ("workspace_verify", WorkspaceVerifyInput, WorkspaceVerifyOutput),
    ("workspace_exec", WorkspaceExecInput, WorkspaceExecOutput),
    ("workspace_commit", WorkspaceCommitInput, WorkspaceCommitOutput),
    ("workspace_push", WorkspacePushInput, WorkspacePushOutput),
    ("workspace_pr", WorkspacePrInput, WorkspacePrOutput),
    ("workspace_pr_evidence", WorkspacePrEvidenceInput, WorkspacePrEvidenceOutput),
    ("operation", OperationInput, OperationOutput),
    ("config_inspect", ConfigInspectInput, ConfigInspectOutput),
    ("runtime_logs_read", RuntimeLogsReadInput, RuntimeLogsReadOutput),
)
