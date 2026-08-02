"""Observation and evidence contracts for the layered ticket-graph reader.

Core contracts that replace a single ``TicketGraphSnapshot`` with per-capability
observations, evidence requirements, and evaluator verdicts.  The types here
are the "Layered In-Process Observation Engine" (F-001 / F-003 / F-005) and
the GraphQLErrorClassifier taxonomy (F-002).

All types are immutable, slotted dataclasses or enums so they can be composed
freely without accidental sharing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from .tickets import GraphEvidenceCapability

# ---------------------------------------------------------------------------
# Global issue identity (F-005)
# ---------------------------------------------------------------------------

#: Default host for github.com issues.  Kept as a single constant so GHE
#: support, when added, touches one line instead of every query-site.
GITHUB_COM_HOST = "github.com"


@dataclass(frozen=True, slots=True, order=True)
class IssueRef:
    """Unambiguous global issue identity across hosts and repositories.

    ``number`` alone is not identity: two repositories on the same host can
    both have a #42.  This type ensures that external / dependency-context
    issues are never confused with local ones (the core F-005 invariant).
    """

    host: str = GITHUB_COM_HOST
    owner: str = ""
    repository: str = ""
    number: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("IssueRef.host must be a non-empty string")
        if not isinstance(self.owner, str) or not self.owner:
            raise ValueError("IssueRef.owner must be a non-empty string")
        if not isinstance(self.repository, str) or not self.repository:
            raise ValueError("IssueRef.repository must be a non-empty string")
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number <= 0:
            raise ValueError("IssueRef.number must be a positive integer")

    @property
    def slug(self) -> str:
        """Return the ``owner/repository`` slug."""
        return f"{self.owner}/{self.repository}"

    def __str__(self) -> str:
        return f"{self.slug}#{self.number}"


# ---------------------------------------------------------------------------
# Graph membership role  (F-005)
# ---------------------------------------------------------------------------


class GraphMembershipRole(str, Enum):
    """Why an issue appears in the graph.

    ``MEMBER`` issues are part of the selected root's subtree and are
    selectable by ``next``.  ``DEPENDENCY_CONTEXT`` issues are blockers,
    sub-issues, or references that live outside the selected subtree but whose
    state is needed to evaluate the selectable issues — they appear in the
    graph but are not themselves selectable.
    """

    MEMBER = "member"
    DEPENDENCY_CONTEXT = "dependency_context"


# ---------------------------------------------------------------------------
# Per-capability observation provenance  (F-003)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservationStamp:
    """Provenance and completeness of one capability observation.

    Every ``CapabilityObservation`` carries one of these so callers can reason
    about staleness, truncation, and authority independently per capability
    instead of relying on a single ``observed_at`` for the whole graph.
    """

    #: Machine-readable description of the source (e.g. ``"gh_api_graphql"``,
    #: ``"cache"``, ``"webhook_delta"``).
    source: str = "live"
    #: ISO-8601 timestamp of when the provider was queried.
    observed_at: str = ""
    #: Optional observed provider revision / ETag when available.
    revision: str | None = None
    #: Opaque authority fingerprint that issued this observation.
    authority_fingerprint: str | None = None
    #: Whether every requested item in the capability was read.
    complete: bool = False
    #: Whether the result was truncated by a budget.
    truncated: bool = False
    #: Stable error codes for failures that affected this capability.
    error_codes: tuple[str, ...] = ()
    #: Number of items successfully observed.
    item_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("ObservationStamp.source must be a non-empty string")
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("ObservationStamp.observed_at must be a non-empty string")
        if not isinstance(self.complete, bool):
            raise ValueError("ObservationStamp.complete must be a bool")
        if (
            not isinstance(self.item_count, int)
            or isinstance(self.item_count, bool)
            or self.item_count < 0
        ):
            raise ValueError("ObservationStamp.item_count must be a non-negative integer")


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CapabilityObservation(Generic[T]):
    """One capability's observation, typed by its data payload.

    ``scope`` lists the issue numbers this observation covers, so a caller
    can ask "did I observe issue 42 for comments?" without scanning every
    other capability.
    """

    capability: GraphEvidenceCapability
    scope: tuple[int, ...] = ()
    data: T | None = None
    stamp: ObservationStamp | None = None
    unavailable_refs: tuple[IssueRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.capability, GraphEvidenceCapability):
            raise ValueError("CapabilityObservation.capability must be a GraphEvidenceCapability")


# ---------------------------------------------------------------------------
# Evidence requirement and verdict  (F-001)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """What evidence a consumer (e.g. ``repo_issue next``) actually needs.

    Instead of gating on ``snapshot.evidence_complete`` (which requires every
    capability to be perfect), an operation declares which capabilities,
    freshness, and skew it can tolerate.
    """

    #: Capabilities the consumer requires to be present and complete.
    required_capabilities: tuple[GraphEvidenceCapability, ...] = ()
    #: Optional scope filter — only issues in ``target_scope`` need complete
    #: evidence.  ``None`` means every observed issue.
    target_scope: tuple[int, ...] | None = None
    #: Maximum acceptable age of any required capability in milliseconds.
    max_age_ms: float | None = None
    #: Maximum acceptable skew between the oldest and newest required
    #: capability in milliseconds.
    max_skew_ms: float | None = None
    #: Whether to revalidate a candidate's core + dependency evidence before
    #: returning it (narrow bounded refresh).
    revalidate_before_return: bool = False

    def __post_init__(self) -> None:
        if self.max_age_ms is not None and (
            not isinstance(self.max_age_ms, (int, float)) or self.max_age_ms < 0
        ):
            raise ValueError("EvidenceRequirement.max_age_ms must be None or a non-negative number")
        if self.max_skew_ms is not None and (
            not isinstance(self.max_skew_ms, (int, float)) or self.max_skew_ms < 0
        ):
            raise ValueError(
                "EvidenceRequirement.max_skew_ms must be None or a non-negative number"
            )

    @classmethod
    def for_next_default(cls) -> EvidenceRequirement:
        """Default requirement for ``repo_issue next``.

        ISSUE metadata, sub-issues (topology), and dependencies are always
        required.  Comments and project overlay are not — they are only needed
        when the readiness or evolution semantics require them (the core F-001
        fix).
        """
        return cls(
            required_capabilities=(
                GraphEvidenceCapability.ISSUE,
                GraphEvidenceCapability.SUB_ISSUES,
                GraphEvidenceCapability.DEPENDENCIES,
            ),
            max_age_ms=300_000,
            max_skew_ms=10_000,
            revalidate_before_return=False,
        )


@dataclass(frozen=True, slots=True)
class EvidenceVerdict:
    """Whether the observed evidence is sufficient for an operation.

    ``sufficient`` is the overall decision.  When ``False``, the detail fields
    explain which evidence paths are missing or unreliable.
    """

    sufficient: bool = False
    #: The issue the verdict was evaluated for (when candidate-specific).
    candidate_ref: IssueRef | None = None
    #: Capabilities that are missing entirely.
    missing: tuple[GraphEvidenceCapability, ...] = ()
    #: Capabilities whose age or skew exceeds the requirement.
    stale: tuple[GraphEvidenceCapability, ...] = ()
    #: Capabilities where the data is ambiguous or unreliable.
    ambiguous: tuple[GraphEvidenceCapability, ...] = ()
    #: Human-readable diagnostics for the verdict.
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.sufficient, bool):
            raise ValueError("EvidenceVerdict.sufficient must be a bool")


# ---------------------------------------------------------------------------
# GraphQLErrorClassifier taxonomy  (F-002)
# ---------------------------------------------------------------------------


class GraphQLErrorClassification(str, Enum):
    """Classification of a single GraphQL error from a batched ``gh api graphql`` response.

    The classifier (in ``graph_decode.py``) maps each error to exactly one of
    these.  ``GLOBAL_BATCH_FAILURE`` causes the whole batch to be rejected;
    alias-scoped failures degrade only the affected alias.
    """

    #: An error attributable to a known alias that maps to an optional
    #: capability (sub-issues, blocked-by, comments).
    ALIAS_CAPABILITY_FAILURE = "alias_capability_failure"
    #: An error attributable to a known alias that maps to the core issue
    #: object itself (title, body, state, labels).
    ALIAS_CORE_FAILURE = "alias_core_failure"
    #: An error that cannot be attributed to a single alias — rate limiting,
    #: schema violations, auth failures.  The entire batch is unreliable.
    GLOBAL_BATCH_FAILURE = "global_batch_failure"


class ObservationAuthorityOrigin(str, Enum):
    """Where an observation authority fingerprint came from.

    ``AUTH_ISSUED`` fingerprints are derived from the resolved auth profile
    identity (host, login/installation, profile) and rotate automatically when
    the credential generation changes.  ``MANUAL_LEGACY`` is the operator-pinned
    ``github_read_cache_authority_digest`` kept as an explicit compatibility
    fallback until the auth boundary issues fingerprints everywhere (F-004).
    """

    AUTH_ISSUED = "auth_issued"
    MANUAL_LEGACY = "manual_legacy"


@dataclass(frozen=True, slots=True)
class GitHubObservationAuthority:
    """Immutable authority identity for one ticket-graph observation.

    Holds only secret-free identity fields plus an opaque fingerprint; the
    observation code consumes the fingerprint and never touches credentials.
    """

    host: str
    #: Auth profile id when the fingerprint is auth-issued, else None.
    profile_id: str | None
    #: Principal or installation identity when known, else None.
    principal_identity: str | None
    #: Opaque authority fingerprint that rotates with the credential generation.
    fingerprint: str
    origin: ObservationAuthorityOrigin

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("GitHubObservationAuthority.host must be a non-empty string")
        if not isinstance(self.fingerprint, str) or len(self.fingerprint) != 64:
            raise ValueError("GitHubObservationAuthority.fingerprint must be a SHA-256 hex digest")
        if not isinstance(self.origin, ObservationAuthorityOrigin):
            raise ValueError(
                "GitHubObservationAuthority.origin must be an ObservationAuthorityOrigin"
            )


__all__ = [
    "GITHUB_COM_HOST",
    "CapabilityObservation",
    "EvidenceRequirement",
    "EvidenceVerdict",
    "GraphMembershipRole",
    "GraphQLErrorClassification",
    "IssueRef",
    "ObservationStamp",
]
