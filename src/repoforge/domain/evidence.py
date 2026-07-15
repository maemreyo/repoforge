"""Provider-neutral normalized evidence, snapshot, conflict, and query contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .errors import ErrorCode, RepoForgeError
from .redaction import redact_text

EVIDENCE_SCHEMA_VERSION = 1
MAX_EVIDENCE_SUMMARY_CHARS = 2_000
MAX_EVIDENCE_REASON_CHARS = 512
MAX_EVIDENCE_SCOPE_ITEMS = 256
MAX_EVIDENCE_QUERY_LIMIT = 100
MAX_EVIDENCE_ARTIFACT_BYTES = 10 * 1024 * 1024

_EVIDENCE_ID = re.compile(r"^ev-[a-f0-9]{24}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA40 = re.compile(r"^[a-f0-9]{40}$")
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,127}$")


class EvidenceSourceKind(str, Enum):
    GIT = "git"
    CODE_INTELLIGENCE = "code_intelligence"
    ARCHITECTURE = "architecture"
    ANALYZER = "analyzer"
    VERIFICATION = "verification"
    CI = "ci"
    KNOWLEDGE = "knowledge"


class EvidenceStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"
    UNAVAILABLE = "unavailable"


def _invalid(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.EVIDENCE_INVALID,
        safe_next_action="Rebuild normalized evidence from bounded provider output for the exact snapshot.",
    )


def validate_evidence_id(value: str) -> str:
    if not isinstance(value, str) or _EVIDENCE_ID.fullmatch(value) is None:
        raise _invalid("evidence_id has an invalid format")
    return value


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise _invalid(f"{field} has an invalid format")
    return value


def _safe_text(value: str, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise _invalid(f"{field} must contain between 1 and {limit} characters")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in normalized):
        raise _invalid(f"{field} contains unsupported control characters")
    return normalized


def _timestamp(value: str, field: str) -> str:
    normalized = _safe_text(value, field, limit=64)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _invalid(f"{field} must include a timezone offset")
    return parsed.isoformat()


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalized_strings(
    values: tuple[str, ...],
    field: str,
    *,
    item_limit: int = 512,
    path_like: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise _invalid(f"{field} must be an immutable tuple")
    if len(values) > MAX_EVIDENCE_SCOPE_ITEMS:
        raise _invalid(f"{field} exceeds {MAX_EVIDENCE_SCOPE_ITEMS} items")
    normalized: set[str] = set()
    for value in values:
        item = _safe_text(value, field, limit=item_limit)
        if path_like:
            candidate = item.replace("\\", "/")
            parts = candidate.split("/")
            if candidate.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise _invalid(f"{field} contains an unsafe repository-relative path")
            item = candidate
        normalized.add(item)
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class EvidenceMeasure:
    """Bounded percentage plus an explicit provider-neutral reason."""

    value: int
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, int)
            or isinstance(self.value, bool)
            or not 0 <= self.value <= 100
        ):
            raise _invalid("evidence measure must be an integer between 0 and 100")
        object.__setattr__(
            self,
            "reason",
            redact_text(
                _safe_text(self.reason, "evidence measure reason", limit=MAX_EVIDENCE_REASON_CHARS),
                limit=MAX_EVIDENCE_REASON_CHARS,
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceArtifactRef:
    """Content-addressed provider artifact reference; never an embedded body."""

    digest: str
    media_type: str
    size_bytes: int
    required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or _SHA64.fullmatch(self.digest) is None:
            raise _invalid("artifact digest must be a lowercase SHA-256")
        if not isinstance(self.media_type, str) or _MEDIA_TYPE.fullmatch(self.media_type) is None:
            raise _invalid("artifact media_type has an invalid format")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or not 0 <= self.size_bytes <= MAX_EVIDENCE_ARTIFACT_BYTES
        ):
            raise _invalid("artifact size exceeds the reviewed bound")
        if not isinstance(self.required, bool):
            raise _invalid("artifact required must be a boolean")


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    source_reference: str
    provider_run_id: str | None = None
    artifact: EvidenceArtifactRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_reference",
            _safe_text(self.source_reference, "source_reference", limit=512),
        )
        if self.provider_run_id is not None:
            object.__setattr__(
                self,
                "provider_run_id",
                _safe_id(self.provider_run_id, "provider_run_id"),
            )


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    flows: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "paths",
            _normalized_strings(self.paths, "paths", path_like=True),
        )
        object.__setattr__(self, "symbols", _normalized_strings(self.symbols, "symbols"))
        object.__setattr__(self, "flows", _normalized_strings(self.flows, "flows"))
        object.__setattr__(self, "tests", _normalized_strings(self.tests, "tests"))
        if not any((self.paths, self.symbols, self.flows, self.tests)):
            raise _invalid("evidence scope must identify at least one path, symbol, flow, or test")


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    snapshot_id: str
    repo_id: str
    head_sha: str
    workspace_id: str | None = None
    workspace_fingerprint: str | None = None
    config_generation: int | None = None
    policy_hash: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.snapshot_id, "snapshot_id")
        _safe_id(self.repo_id, "repo_id")
        if not isinstance(self.head_sha, str) or _SHA40.fullmatch(self.head_sha) is None:
            raise _invalid("head_sha must be a lowercase 40-character Git SHA")
        if self.workspace_id is not None:
            _safe_id(self.workspace_id, "workspace_id")
        if self.workspace_fingerprint is not None and (
            not isinstance(self.workspace_fingerprint, str)
            or _SHA64.fullmatch(self.workspace_fingerprint) is None
        ):
            raise _invalid("workspace_fingerprint must be a lowercase SHA-256")
        if (self.workspace_id is None) != (self.workspace_fingerprint is None):
            raise _invalid("workspace_id and workspace_fingerprint must be present together")
        if self.config_generation is not None and (
            not isinstance(self.config_generation, int)
            or isinstance(self.config_generation, bool)
            or self.config_generation < 0
        ):
            raise _invalid("config_generation must be a non-negative integer")
        if self.policy_hash is not None and (
            not isinstance(self.policy_hash, str) or _SHA64.fullmatch(self.policy_hash) is None
        ):
            raise _invalid("policy_hash must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    source_kind: EvidenceSourceKind
    provider_id: str
    provider_version: str
    provenance: EvidenceProvenance
    scope: EvidenceScope
    snapshot: EvidenceSnapshot
    summary: str
    coverage: EvidenceMeasure
    confidence: EvidenceMeasure
    status: EvidenceStatus
    conflict_group: str | None
    content_digest: str
    created_at: str
    expires_at: str | None = None
    schema_version: int = EVIDENCE_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    snapshot_id: str | None = None
    source_kinds: tuple[EvidenceSourceKind, ...] = ()
    path: str | None = None
    symbol: str | None = None
    test: str | None = None
    statuses: tuple[EvidenceStatus, ...] = ()
    include_stale: bool = False
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.snapshot_id is not None:
            _safe_id(self.snapshot_id, "query snapshot_id")
        if not isinstance(self.source_kinds, tuple) or not all(
            isinstance(item, EvidenceSourceKind) for item in self.source_kinds
        ):
            raise _invalid("query source_kinds must be an EvidenceSourceKind tuple")
        object.__setattr__(
            self,
            "source_kinds",
            tuple(sorted(set(self.source_kinds), key=lambda item: item.value)),
        )
        for field in ("path", "symbol", "test"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _safe_text(value, f"query {field}", limit=512))
        if not isinstance(self.statuses, tuple) or not all(
            isinstance(item, EvidenceStatus) for item in self.statuses
        ):
            raise _invalid("query statuses must be an EvidenceStatus tuple")
        object.__setattr__(
            self,
            "statuses",
            tuple(sorted(set(self.statuses), key=lambda item: item.value)),
        )
        if not isinstance(self.include_stale, bool):
            raise _invalid("query include_stale must be a boolean")
        if (
            not isinstance(self.limit, int)
            or isinstance(self.limit, bool)
            or not 1 <= self.limit <= MAX_EVIDENCE_QUERY_LIMIT
        ):
            raise _invalid(f"query limit must be between 1 and {MAX_EVIDENCE_QUERY_LIMIT}")
        if self.cursor is not None:
            validate_evidence_id(self.cursor)


def _measure_payload(measure: EvidenceMeasure) -> dict[str, object]:
    return {"value": measure.value, "reason": measure.reason}


def _artifact_payload(reference: EvidenceArtifactRef | None) -> dict[str, object] | None:
    if reference is None:
        return None
    return {
        "digest": reference.digest,
        "media_type": reference.media_type,
        "size_bytes": reference.size_bytes,
        "required": reference.required,
    }


def _provenance_payload(provenance: EvidenceProvenance) -> dict[str, object]:
    return {
        "source_reference": provenance.source_reference,
        "provider_run_id": provenance.provider_run_id,
        "artifact": _artifact_payload(provenance.artifact),
    }


def _scope_payload(scope: EvidenceScope) -> dict[str, object]:
    return {
        "paths": list(scope.paths),
        "symbols": list(scope.symbols),
        "flows": list(scope.flows),
        "tests": list(scope.tests),
    }


def _snapshot_payload(snapshot: EvidenceSnapshot) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "repo_id": snapshot.repo_id,
        "workspace_id": snapshot.workspace_id,
        "head_sha": snapshot.head_sha,
        "workspace_fingerprint": snapshot.workspace_fingerprint,
        "config_generation": snapshot.config_generation,
        "policy_hash": snapshot.policy_hash,
    }


def evidence_identity_payload(item: EvidenceItem) -> dict[str, object]:
    """Return normalized identity fields; derived status is deliberately excluded."""

    return {
        "source_kind": item.source_kind.value,
        "provider_id": item.provider_id,
        "provider_version": item.provider_version,
        "provenance": _provenance_payload(item.provenance),
        "scope": _scope_payload(item.scope),
        "snapshot": _snapshot_payload(item.snapshot),
        "summary": item.summary,
        "coverage": _measure_payload(item.coverage),
        "confidence": _measure_payload(item.confidence),
        "conflict_group": item.conflict_group,
        "created_at": item.created_at,
        "expires_at": item.expires_at,
        "schema_version": item.schema_version,
    }


def canonical_evidence_identity_bytes(item: EvidenceItem) -> bytes:
    return json.dumps(
        evidence_identity_payload(item),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def evidence_payload(item: EvidenceItem) -> dict[str, object]:
    payload = evidence_identity_payload(item)
    return {
        "evidence_id": item.evidence_id,
        **payload,
        "status": item.status.value,
        "content_digest": item.content_digest,
    }


def canonical_evidence_bytes(item: EvidenceItem) -> bytes:
    return json.dumps(
        evidence_payload(item),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def validate_evidence_item(item: EvidenceItem) -> EvidenceItem:
    validate_evidence_id(item.evidence_id)
    if item.schema_version != EVIDENCE_SCHEMA_VERSION or isinstance(item.schema_version, bool):
        raise RepoForgeError(
            f"Unsupported evidence schema version: {item.schema_version!r}",
            code=ErrorCode.EVIDENCE_SCHEMA_UNSUPPORTED,
            safe_next_action="Upgrade RepoForge before reading or writing this evidence schema.",
        )
    _safe_id(item.provider_id, "provider_id")
    _safe_text(item.provider_version, "provider_version", limit=64)
    if item.conflict_group is not None:
        _safe_id(item.conflict_group, "conflict_group")
    normalized_created = _timestamp(item.created_at, "created_at")
    normalized_expires = (
        _timestamp(item.expires_at, "expires_at") if item.expires_at is not None else None
    )
    if normalized_expires is not None and _timestamp_value(normalized_expires) <= _timestamp_value(
        normalized_created
    ):
        raise _invalid("expires_at must be later than created_at")
    if item.created_at != normalized_created or item.expires_at != normalized_expires:
        raise _invalid("evidence timestamps must use canonical ISO-8601 formatting")
    if not isinstance(item.content_digest, str) or _SHA64.fullmatch(item.content_digest) is None:
        raise _invalid("content_digest must be a lowercase SHA-256")
    expected_digest = hashlib.sha256(canonical_evidence_identity_bytes(item)).hexdigest()
    if item.content_digest != expected_digest:
        raise _invalid("content_digest does not match normalized evidence identity")
    if item.evidence_id != f"ev-{expected_digest[:24]}":
        raise _invalid("evidence_id does not match normalized evidence identity")
    return item


def new_evidence_item(
    *,
    source_kind: EvidenceSourceKind,
    provider_id: str,
    provider_version: str,
    provenance: EvidenceProvenance,
    scope: EvidenceScope,
    snapshot: EvidenceSnapshot,
    summary: str,
    coverage: EvidenceMeasure,
    confidence: EvidenceMeasure,
    status: EvidenceStatus,
    conflict_group: str | None,
    created_at: str,
    expires_at: str | None = None,
) -> EvidenceItem:
    if not isinstance(source_kind, EvidenceSourceKind):
        raise _invalid("source_kind must be an EvidenceSourceKind")
    if not isinstance(status, EvidenceStatus):
        raise _invalid("status must be an EvidenceStatus")
    normalized_provider_id = _safe_id(provider_id, "provider_id")
    normalized_provider_version = _safe_text(provider_version, "provider_version", limit=64)
    normalized_summary = redact_text(
        _safe_text(summary, "summary", limit=MAX_EVIDENCE_SUMMARY_CHARS),
        limit=MAX_EVIDENCE_SUMMARY_CHARS,
    )
    normalized_conflict_group = (
        _safe_id(conflict_group, "conflict_group") if conflict_group is not None else None
    )
    normalized_created = _timestamp(created_at, "created_at")
    normalized_expires = _timestamp(expires_at, "expires_at") if expires_at is not None else None
    provisional = EvidenceItem(
        evidence_id="ev-" + "0" * 24,
        source_kind=source_kind,
        provider_id=normalized_provider_id,
        provider_version=normalized_provider_version,
        provenance=provenance,
        scope=scope,
        snapshot=snapshot,
        summary=normalized_summary,
        coverage=coverage,
        confidence=confidence,
        status=status,
        conflict_group=normalized_conflict_group,
        content_digest="0" * 64,
        created_at=normalized_created,
        expires_at=normalized_expires,
    )
    digest = hashlib.sha256(canonical_evidence_identity_bytes(provisional)).hexdigest()
    return validate_evidence_item(
        replace(
            provisional,
            evidence_id=f"ev-{digest[:24]}",
            content_digest=digest,
        )
    )


def evidence_status_for(
    item: EvidenceItem,
    *,
    current_snapshot: EvidenceSnapshot,
    now: str,
) -> EvidenceStatus:
    """Derive snapshot/expiry staleness without mutating persisted provider evidence."""

    validate_evidence_item(item)
    normalized_now = _timestamp(now, "now")
    if item.snapshot != current_snapshot:
        return EvidenceStatus.STALE
    if item.expires_at is not None and _timestamp_value(normalized_now) >= _timestamp_value(
        item.expires_at
    ):
        return EvidenceStatus.STALE
    return item.status


def mark_evidence_conflicts(items: tuple[EvidenceItem, ...]) -> tuple[EvidenceItem, ...]:
    """Mark divergent evidence in the same snapshot-bound conflict group."""

    groups: dict[tuple[str, str], list[EvidenceItem]] = {}
    for item in items:
        validate_evidence_item(item)
        if item.conflict_group is not None and item.status is not EvidenceStatus.STALE:
            groups.setdefault((item.snapshot.snapshot_id, item.conflict_group), []).append(item)
    conflicting_ids: set[str] = set()
    for group in groups.values():
        if len({item.content_digest for item in group}) > 1:
            conflicting_ids.update(item.evidence_id for item in group)
    marked = tuple(
        replace(item, status=EvidenceStatus.CONFLICTING)
        if item.evidence_id in conflicting_ids
        else item
        for item in items
    )
    return tuple(sorted(marked, key=lambda item: item.evidence_id))


def evidence_matches_query(item: EvidenceItem, query: EvidenceQuery) -> bool:
    if query.snapshot_id is not None and item.snapshot.snapshot_id != query.snapshot_id:
        return False
    if query.source_kinds and item.source_kind not in query.source_kinds:
        return False
    if query.path is not None and query.path not in item.scope.paths:
        return False
    if query.symbol is not None and query.symbol not in item.scope.symbols:
        return False
    if query.test is not None and query.test not in item.scope.tests:
        return False
    if query.statuses and item.status not in query.statuses:
        return False
    return query.include_stale or item.status is not EvidenceStatus.STALE
