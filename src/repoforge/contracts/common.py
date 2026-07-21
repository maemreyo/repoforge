"""Closed, reusable Pydantic primitives shared by every Forge v2 tool."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain.errors import ErrorCode

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
RepoId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
RelativePath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=4096,
        pattern=r"^[A-Za-z0-9._ -][A-Za-z0-9._/ -]*$",
    ),
]
GitRef = Annotated[str, Field(min_length=1, max_length=512)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
GitObjectId = Annotated[str, Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")]
Cursor = Annotated[str, Field(min_length=1, max_length=2048)]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]
LongText = Annotated[str, Field(min_length=1, max_length=120_000)]
ByteBudget = Annotated[int, Field(ge=1, le=120_000)]


class StrictModel(BaseModel):
    """Base model that fails closed on undeclared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class ToolErrorDetails(StrictModel):
    """Bounded optional details for the unified public error union."""

    field: str | None = Field(default=None, max_length=160)
    path: RelativePath | None = None
    operation_index: int | None = Field(default=None, ge=0, le=99)
    expected: str | None = Field(default=None, max_length=1000)
    actual: str | None = Field(default=None, max_length=1000)
    correlation_id: str | None = Field(default=None, max_length=128)
    operation_id: str | None = Field(default=None, max_length=160)
    receipt_id: str | None = Field(default=None, max_length=160)
    result_reference: str | None = Field(default=None, max_length=256)
    effect_boundary_crossed: bool | None = None
    original_error_type: str | None = Field(default=None, max_length=160)


class ToolError(StrictModel):
    """One typed error shape shared by all 28 public tools."""

    code: ErrorCode
    message: ShortText
    why: ShortText
    retryable: bool = False
    safe_next_action: ShortText
    details: ToolErrorDetails | None = None
    unchanged_state: tuple[ShortText, ...] = Field(default=(), max_length=20)
    automatic_retry_allowed: bool = False


class ToolResponse(StrictModel):
    """Stable success metadata inherited by every tool-specific output."""

    status: Literal["ok"] = "ok"
    summary: str = Field(min_length=1, max_length=500)
    error: None = None


class OutcomeReceiptEvidence(StrictModel):
    """Authoritative durable outcome identity for one mutating call."""

    operation_id: str = Field(pattern=r"^op-[0-9a-f]{24}$")
    receipt_id: str = Field(pattern=r"^receipt-[0-9a-f]{24}$")
    state: Literal[
        "accepted",
        "applying",
        "applied_unvalidated",
        "applied_validated",
        "rolled_back",
        "failed_before_effect",
        "failed_after_effect",
        "unknown",
    ]
    result_reference: str | None = Field(default=None, max_length=256)
    effect_boundary_crossed: bool
    pre_identity: dict[str, str] = Field(default_factory=dict, max_length=20)
    post_identity: dict[str, str] = Field(default_factory=dict, max_length=20)


class ToolFailure(StrictModel):
    """One failure branch shared by every advertised tool output union."""

    status: Literal["failed"]
    summary: str = Field(min_length=1, max_length=500)
    error: ToolError


class Freshness(str, Enum):
    LIVE = "live"
    CACHE = "cache"
    LOCAL = "local"
    UNAVAILABLE = "unavailable"


class ProviderEvidence(StrictModel):
    provider: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0.0, le=1.0)
    coverage: tuple[str, ...] = Field(default=(), max_length=100)
    limitations: tuple[str, ...] = Field(default=(), max_length=100)


class ReadFileRequest(StrictModel):
    path: RelativePath
    start_line: int = Field(default=1, ge=1, le=10_000_000)
    end_line: int = Field(default=500, ge=1, le=10_000_000)


class ReadFileResult(StrictModel):
    path: RelativePath
    content: str = Field(max_length=120_000)
    sha256: Sha256
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    truncated: bool = False
    omitted_line_range: tuple[int, int] | None = None
    next_cursor: Cursor | None = None


class SearchMode(str, Enum):
    LITERAL = "literal"
    REGEX = "regex"
    FILE_NAME = "file_name"


class SearchMatch(StrictModel):
    path: RelativePath
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    match: str = Field(min_length=1, max_length=4000)
    context_before: tuple[str, ...] = Field(default=(), max_length=5)
    context_after: tuple[str, ...] = Field(default=(), max_length=5)
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    provider: str = Field(default="literal", min_length=1, max_length=80)


class TreeEntryKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class TreeEntry(StrictModel):
    path: RelativePath
    kind: TreeEntryKind
    size_bytes: int | None = Field(default=None, ge=0)


class DiffLine(StrictModel):
    kind: Literal["context", "add", "delete"]
    old_line: int | None = Field(default=None, ge=1)
    new_line: int | None = Field(default=None, ge=1)
    text: str = Field(max_length=10_000)


class DiffHunk(StrictModel):
    header: str = Field(min_length=1, max_length=500)
    lines: tuple[DiffLine, ...] = Field(default=(), max_length=5000)


class DiffFile(StrictModel):
    path: RelativePath
    status: Literal["added", "modified", "deleted", "renamed"]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    hunks: tuple[DiffHunk, ...] = Field(default=(), max_length=500)


class CommitSummary(StrictModel):
    sha: GitObjectId
    subject: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=300)
    committed_at: str = Field(min_length=1, max_length=80)


class ChangeBudgetLimits(StrictModel):
    max_changed_files: int = Field(ge=0)
    max_diff_lines: int = Field(ge=0)
    max_total_changed_bytes: int = Field(ge=0)


class ChangeMetrics(StrictModel):
    changed_files: int = Field(ge=0)
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    diff_lines: int = Field(ge=0)
    binary_files: int = Field(default=0, ge=0)
    total_current_bytes: int = Field(ge=0)
    limits: ChangeBudgetLimits | None = None
    within_limits: bool


class CommandEvidence(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1, max_length=100)
    returncode: int
    duration_ms: float = Field(ge=0)
    output_excerpt: str = Field(default="", max_length=12_000)


class EnforcementEvidenceModel(StrictModel):
    network: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    filesystem: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    timeout: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    output: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    process_cleanup: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    cpu: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    memory: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    disk: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    subprocess_count: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]
    network_bytes: Literal["enforced", "advisory", "observed", "unsupported", "not_applicable"]


class ExecutionEvidenceModel(StrictModel):
    adapter_kind: str = Field(min_length=1, max_length=80)
    identity_schema_version: int = Field(ge=1, le=100)
    environment_identity_hash: Sha256
    requested_policy_hash: Sha256
    effective_policy_hash: Sha256
    requested_network: str = Field(min_length=1, max_length=80)
    effective_network: str = Field(min_length=1, max_length=80)
    requested_filesystem: str = Field(min_length=1, max_length=80)
    effective_filesystem: str = Field(min_length=1, max_length=80)
    degraded: bool
    enforcement: EnforcementEvidenceModel
    warnings: tuple[str, ...] = Field(default=(), max_length=20)


class RepositorySummary(StrictModel):
    repo_id: RepoId
    capabilities: tuple[str, ...] = Field(default=(), max_length=100)
    default_ref: GitRef


class WorkspaceSummary(StrictModel):
    workspace_id: Identifier
    repo_id: RepoId
    branch: str = Field(min_length=1, max_length=512)
    base: GitRef
    exists: bool
    dirty: bool | None = None
    lifecycle: str = Field(min_length=1, max_length=80)
    issue_ids: tuple[str, ...] = Field(default=(), max_length=100)


class OperationState(str, Enum):
    QUEUED = "queued"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ORPHANED = "orphaned"


class OperationEvidence(StrictModel):
    operation_id: Identifier
    kind: str = Field(min_length=1, max_length=120)
    state: OperationState
    phase: str = Field(min_length=1, max_length=120)
    progress_current: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    progress_unit: str | None = Field(default=None, max_length=64)
    progress_message: str | None = Field(default=None, max_length=2_000)
    workspace_id: Identifier | None = None
    result_reference: str | None = Field(default=None, max_length=256)
    result_reference_status: Literal["not_applicable", "not_checked", "available", "missing"] = (
        "not_applicable"
    )
    receipt_id: str | None = Field(default=None, pattern=r"^receipt-[a-f0-9]{24}$")
    receipt_status: Literal["not_applicable", "not_checked", "available", "missing"] = (
        "not_applicable"
    )
    error_code: str | None = Field(default=None, max_length=128)
    retryability: Literal["none", "manual", "automatic"] = "none"
    terminal: bool = False
    cancellation_reason: str | None = Field(default=None, max_length=500)
    poll_after_seconds: float | None = Field(default=1.0, ge=0.1, le=60.0)
    suggested_poll_after_s: float | None = Field(default=1.0, ge=0.1, le=60.0)
    eta_seconds: float | None = Field(default=None, ge=0.0, le=31_536_000.0)
    updated_at: str | None = Field(default=None, max_length=80)
    schema_version: int = Field(default=2, ge=1)
    record_provenance: Literal["current", "legacy_migrated", "recovered_inconsistent"] = "current"
    record_consistency: Literal["consistent", "record_inconsistent"] = "consistent"
    record_diagnostics: tuple[str, ...] = Field(default=(), max_length=20)


class KeyValue(StrictModel):
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(max_length=10_000)
