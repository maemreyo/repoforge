"""Strict request and response models for the static 28-tool Forge v2 surface."""

from __future__ import annotations

from enum import Enum
from string import Formatter
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import (
    ByteBudget,
    ChangeMetrics,
    CommandEvidence,
    CommitSummary,
    Cursor,
    DiffFile,
    Freshness,
    GitObjectId,
    GitRef,
    Identifier,
    KeyValue,
    LongText,
    OperationEvidence,
    OperationState,
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


class ContextSectionName(str, Enum):
    REPOSITORY = "repository"
    STATUS = "status"
    TICKET = "ticket"
    WORKSPACE = "workspace"
    RECENT_COMMITS = "recent_commits"


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
        default=(
            ContextSectionName.REPOSITORY,
            ContextSectionName.STATUS,
            ContextSectionName.TICKET,
            ContextSectionName.WORKSPACE,
            ContextSectionName.RECENT_COMMITS,
        ),
        min_length=1,
        max_length=5,
    )
    byte_budget: ByteBudget = 96_000


class RepoTaskContextOutput(ToolResponse):
    repo_id: RepoId
    sections: tuple[ContextSection, ...] = Field(default=(), max_length=5)
    truncated: bool = False
    next_cursor: Cursor | None = None


class RepoReadInput(StrictModel):
    repo_id: RepoId
    files: tuple[ReadFileRequest, ...] = Field(min_length=1, max_length=20)
    ref: GitRef | None = None
    byte_budget: ByteBudget = 60_000
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
    byte_budget: ByteBudget = 60_000
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


class RepoTreeInput(StrictModel):
    repo_id: RepoId
    ref: GitRef | None = None
    subtree: RelativePath | None = None
    max_entries: int = Field(default=500, ge=1, le=2000)
    byte_budget: ByteBudget = 60_000
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
    byte_budget: ByteBudget = 60_000
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


class RepoIssueInput(StrictModel):
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


class RepoIssueOutput(ToolResponse):
    repo_id: RepoId
    mode: IssueMode
    graph_status: Literal["available", "graph_unavailable", "not_requested"]
    issue: IssueEvidence | None = None
    nodes: tuple[IssueGraphNode, ...] = Field(default=(), max_length=500)
    selected: tuple[IssueGraphNode, ...] = Field(default=(), max_length=100)
    drift: tuple[IssueDrift, ...] = Field(default=(), max_length=100)
    mutation: IssueMutationEvidence | None = None
    next_action: ShortText | None = None
    truncated: bool = False
    next_cursor: Cursor | None = None


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


class RepoListInput(StrictModel):
    detail: bool = False
    cursor: Cursor | None = None
    limit: int = Field(default=50, ge=1, le=100)


class RepoListOutput(ToolResponse):
    repositories: tuple[RepositorySummary, ...] = Field(default=(), max_length=100)
    truncated: bool = False
    next_cursor: Cursor | None = None


class PolicyAction(str, Enum):
    PREVIEW = "preview"
    APPLY = "apply"


class PolicyMutation(StrictModel):
    section: Literal["profile", "diagnostic", "formatter", "override"]
    name: str = Field(min_length=1, max_length=160)
    operation: Literal["set", "remove"]
    value: str | None = Field(default=None, max_length=20_000)


class GeneratedPathDeclaration(StrictModel):
    glob: str = Field(min_length=1, max_length=512)
    regeneration_command: tuple[str, ...] = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)


class IssueWritePolicyDeclaration(StrictModel):
    enabled_ops: tuple[Literal["comment", "close", "reopen", "link", "create"], ...] = Field(
        default=("comment",), max_length=5
    )
    approval_required_ops: tuple[Literal["comment", "close", "reopen", "link", "create"], ...] = (
        Field(default=(), max_length=5)
    )
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
    operator_instruction: str | None = Field(default=None, max_length=1000)


class WorkspaceCreateInput(StrictModel):
    repo_id: RepoId
    task_slug: str = Field(min_length=1, max_length=160)
    base: GitRef | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=256)
    issue_ids: tuple[str, ...] = Field(default=(), max_length=100)


class WorkspaceCreateOutput(ToolResponse):
    workspace_id: Identifier
    repo_id: RepoId
    branch: str = Field(min_length=1, max_length=512)
    base: GitRef
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    issue_ids: tuple[str, ...] = Field(default=(), max_length=100)


class WorkspaceRemoveInput(StrictModel):
    workspace_id: Identifier
    delete_local_branch: bool = False


class WorkspaceRemoveOutput(ToolResponse):
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


class RefreshResolution(StrictModel):
    path: RelativePath
    content: str = Field(max_length=2_000_000)


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


class WorkspaceRefreshInput(StrictModel):
    workspace_id: Identifier
    action: RefreshAction
    expected_head_sha: GitObjectId
    expected_fingerprint: Sha256
    plan_token: str | None = Field(default=None, max_length=2048)
    resolutions: tuple[RefreshResolution, ...] = Field(default=(), max_length=100)


class WorkspaceRefreshOutput(ToolResponse):
    workspace_id: Identifier
    action: RefreshAction
    result: Literal["current", "preview", "applied", "conflict"]
    plan_hash: Sha256
    plan_token: str | None = Field(default=None, max_length=2048)
    target_base_sha: GitObjectId
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    prediction_scope: Literal["committed_head"] = "committed_head"
    apply_blockers: tuple[str, ...] = Field(default=(), max_length=20)
    conflicts: tuple[RefreshConflictEvidence, ...] = Field(default=(), max_length=100)
    warnings: tuple[str, ...] = Field(default=(), max_length=100)
    changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    verify_selector: tuple[RelativePath, ...] = Field(default=(), max_length=1000)
    invalidated_receipts: tuple[str, ...] = Field(default=(), max_length=100)
    transaction_id: Identifier | None = None


class WorkspaceStatusSection(str, Enum):
    LOCAL = "local"
    BASE = "base"
    HYGIENE = "hygiene"


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
    byte_budget: ByteBudget = 60_000


class WorkspaceStatusOutput(ToolResponse):
    workspace_id: Identifier
    repo_id: RepoId
    head_sha: GitObjectId
    workspace_fingerprint: Sha256
    clean: bool
    sections: tuple[StatusSectionEvidence, ...] = Field(default=(), max_length=3)
    fingerprint_source: Literal["cache", "scan"]
    truncated: bool = False


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
    workspace_id: Identifier
    formatters: tuple[FormatterEvidence, ...] = Field(default=(), max_length=100)
    changed: bool
    head_sha: GitObjectId
    workspace_fingerprint: Sha256


class WorkspaceReadInput(StrictModel):
    workspace_id: Identifier
    files: tuple[ReadFileRequest, ...] = Field(min_length=1, max_length=20)
    byte_budget: ByteBudget = 60_000
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
    byte_budget: ByteBudget = 60_000
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


class WorkspaceTreeInput(StrictModel):
    workspace_id: Identifier
    subtree: RelativePath | None = None
    max_entries: int = Field(default=500, ge=1, le=2000)
    byte_budget: ByteBudget = 60_000
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
    max_files: int = Field(default=100, ge=1, le=1000)
    byte_budget: ByteBudget = 120_000
    cursor: Cursor | None = None


class WorkspaceDiffOutput(ToolResponse):
    workspace_id: Identifier
    staged: bool
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


class RestoreOperation(StrictModel):
    op: Literal["restore"]
    paths: tuple[RelativePath, ...] = Field(min_length=1, max_length=100)


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


class WorkspaceMutateOutput(ToolResponse):
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
    transaction_id: Identifier | None = None


class VerifyMode(str, Enum):
    PLAN = "plan"
    AUTO = "auto"
    DIAGNOSTIC = "diagnostic"
    PROFILE = "profile"
    ADHOC = "adhoc"


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


class WorkspaceVerifyAssessment(StrictModel):
    snapshot_id: Sha256
    current: bool
    changed_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    risk_score: int = Field(ge=0, le=100)
    risk_level: Literal["low", "medium", "high", "critical"]
    uncertainties: tuple[str, ...] = Field(default=(), max_length=64)
    refresh_required: bool
    behind_base: int = Field(ge=0)
    provider: ProviderEvidence | None = None
    final_profile: Identifier
    manual_review_required: bool
    evidence_coverage: tuple[KeyValue, ...] = Field(default=(), max_length=32)


class WorkspaceVerifyInput(StrictModel):
    workspace_id: Identifier
    mode: VerifyMode = VerifyMode.AUTO
    diagnostic_id: Identifier | None = None
    selector: str | tuple[str, ...] | None = None
    selector2: str | tuple[str, ...] | None = None
    profile_name: Identifier | None = None
    argv: tuple[str, ...] | None = Field(default=None, max_length=100)
    working_directory: RelativePath | None = None
    expected_fingerprint: Sha256 | None = None
    background: bool = False
    intent: VerifyIntent = VerifyIntent.FINAL
    expectation: VerifyExpectation = VerifyExpectation.NONE
    expected_failure_class: Identifier | None = None
    force_rerun: bool = False
    impact_paths: tuple[RelativePath, ...] = Field(default=(), max_length=2000)
    artifact_output_path: RelativePath | None = None

    @model_validator(mode="after")
    def validate_mode_fields(self) -> WorkspaceVerifyInput:
        if self.mode is VerifyMode.DIAGNOSTIC and self.diagnostic_id is None:
            raise ValueError("diagnostic mode requires diagnostic_id")
        if self.mode is VerifyMode.ADHOC and not self.argv:
            raise ValueError("adhoc mode requires argv")
        if self.mode is VerifyMode.PLAN and (self.background or self.artifact_output_path):
            raise ValueError("plan mode is read-only")
        if self.background and self.artifact_output_path is not None:
            raise ValueError("background verification cannot write a synchronous artifact")
        if (
            self.expected_failure_class is not None
            and self.expectation is not VerifyExpectation.FAIL
        ):
            raise ValueError("expected_failure_class requires expectation=fail")
        return self


class WorkspaceVerifyOutput(ToolResponse):
    workspace_id: Identifier
    requested_mode: VerifyMode
    selected_mode: VerifyMode
    routing_reason: str = Field(min_length=1, max_length=1000)
    impact_evidence: ProviderEvidence | None = None
    assessment: WorkspaceVerifyAssessment | None = None
    recommendations: tuple[VerifyRecommendationEvidence, ...] = Field(default=(), max_length=32)
    staleness_warning: str | None = Field(default=None, max_length=1000)
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


class WorkspaceCommitInput(StrictModel):
    workspace_id: Identifier
    message: str = Field(min_length=1, max_length=1000)


class WorkspaceCommitOutput(ToolResponse):
    workspace_id: Identifier
    previous_head_sha: GitObjectId
    head_sha: GitObjectId
    committed: bool
    verification_fingerprint: Sha256


class WorkspacePushInput(StrictModel):
    workspace_id: Identifier
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=256)


class WorkspacePushOutput(ToolResponse):
    workspace_id: Identifier
    head_sha: GitObjectId
    remote: str = Field(min_length=1, max_length=160)
    remote_head_before: GitObjectId | None = None
    remote_head_after: GitObjectId
    pushed: bool
    retryable_rejection: bool = False


class WorkspacePrAction(str, Enum):
    CREATE_DRAFT = "create_draft"
    UPDATE = "update"
    WATCH = "watch"


class WorkspacePrInput(StrictModel):
    workspace_id: Identifier
    action: WorkspacePrAction
    title: str | None = Field(default=None, max_length=1000)
    body: str | None = Field(default=None, max_length=60_000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=256)
    until: Literal["all_completed", "required_completed", "first_failure"] = "all_completed"
    timeout_seconds: int = Field(default=900, ge=1, le=3600)
    event_cursor: Cursor | None = None


class WorkspacePrOutput(ToolResponse):
    workspace_id: Identifier
    action: WorkspacePrAction
    pull_request: PullRequestEvidence | None = None
    operation: OperationEvidence | None = None
    remote_version: str | None = Field(default=None, max_length=256)
    event_cursor: Cursor | None = None
    terminal_reason: str | None = Field(default=None, max_length=500)


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


class WorkspacePrEvidenceOutput(ToolResponse):
    workspace_id: Identifier
    pull_request: PullRequestEvidence
    checks: tuple[CheckEvidence, ...] = Field(default=(), max_length=500)
    failure_excerpt: tuple[str, ...] = Field(default=(), max_length=200)
    delta_token: Cursor
    changed_since: bool
    truncated: bool = False


class OperationAction(str, Enum):
    GET = "get"
    LIST = "list"
    CANCEL = "cancel"


class OperationInput(StrictModel):
    action: OperationAction
    operation_id: Identifier | None = None
    scope: str | None = Field(default=None, max_length=300)
    state: OperationState | None = None
    expected_updated_at: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: Cursor | None = None


class OperationOutput(ToolResponse):
    action: OperationAction
    operation: OperationEvidence | None = None
    operations: tuple[OperationEvidence, ...] = Field(default=(), max_length=200)
    cancellation_requested: bool = False
    truncated: bool = False
    next_cursor: Cursor | None = None


class ConfigInspectInput(StrictModel):
    repo_id: RepoId | None = None
    include_pending: bool = True


class ConfigGenerationSummary(StrictModel):
    generation: int = Field(ge=1)
    state: Literal["accepted", "active", "pending", "rejected"]
    digest: Sha256
    changed_sections: tuple[str, ...] = Field(default=(), max_length=100)


class ConfigInspectOutput(ToolResponse):
    accepted: ConfigGenerationSummary | None = None
    active: ConfigGenerationSummary | None = None
    pending: tuple[ConfigGenerationSummary, ...] = Field(default=(), max_length=100)
    repo_facts: tuple[KeyValue, ...] = Field(default=(), max_length=500)


class RuntimeLogSource(str, Enum):
    AUDIT = "audit"
    RUNTIME = "runtime"


class RuntimeLogEntry(StrictModel):
    timestamp: str = Field(min_length=1, max_length=80)
    source: RuntimeLogSource
    action: str | None = Field(default=None, max_length=160)
    level: str = Field(min_length=1, max_length=30)
    message: str = Field(max_length=4000)
    duration_ms: float | None = Field(default=None, ge=0)


class RuntimeLogsReadInput(StrictModel):
    source: RuntimeLogSource = RuntimeLogSource.AUDIT
    limit: int = Field(default=50, ge=1, le=200)
    action: str | None = Field(default=None, max_length=160)
    only_failed: bool = False
    min_duration_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    start_time: str | None = Field(default=None, max_length=80)
    end_time: str | None = Field(default=None, max_length=80)
    cursor: Cursor | None = None


class RuntimeLogsReadOutput(ToolResponse):
    source: RuntimeLogSource
    entries: tuple[RuntimeLogEntry, ...] = Field(default=(), max_length=200)
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
    ("workspace_commit", WorkspaceCommitInput, WorkspaceCommitOutput),
    ("workspace_push", WorkspacePushInput, WorkspacePushOutput),
    ("workspace_pr", WorkspacePrInput, WorkspacePrOutput),
    ("workspace_pr_evidence", WorkspacePrEvidenceInput, WorkspacePrEvidenceOutput),
    ("operation", OperationInput, OperationOutput),
    ("config_inspect", ConfigInspectInput, ConfigInspectOutput),
    ("runtime_logs_read", RuntimeLogsReadInput, RuntimeLogsReadOutput),
)
