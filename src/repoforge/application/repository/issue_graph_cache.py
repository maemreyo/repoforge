"""Cache envelope codec and binding validation for the ticket-graph snapshot.

The cache envelope is a versioned dict payload with bindings that pin the
repository slug, the reviewed source configuration digest, the reader/query
contract version, the API version, the authority digest, and a payload
checksum.  Strict codec rules: coercive parsing and silent skips are not
allowed (F-006) — a semantically malformed envelope is rejected as a whole.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

from ...config import GitHubTicketGraphConfig
from ...domain.observation import IssueRef, ObservationStamp
from ...domain.tickets import (
    GITHUB_API_VERSION,
    TICKET_GRAPH_READER_VERSION,
    CapabilityCoverage,
    GraphEvidenceCapability,
    TicketDiagnostic,
    TicketGraph,
    TicketGraphSnapshot,
    TicketLiveMetadata,
    TicketNode,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from .issue_graph_payloads import capability_coverage_payload, node_payload

#: Cache envelopes do not survive negative age beyond this skew; a cached
#: snapshot whose observed_at is far in the future relative to the local
#: clock must be discarded instead of served.
_ALLOWED_CACHE_SKEW_MS = 5_000


def source_digest(source: GitHubTicketGraphConfig) -> str:
    """Digest of the reviewed source configuration the snapshot was read under.

    Binds the cache entry to the repository/project fields and field names the
    reader used, so a configuration change is a cache miss instead of stale
    evidence served as current.
    """
    canonical = json.dumps(
        {
            "repository": source.repository,
            "root_issue": source.root_issue,
            "project_owner": source.project_owner,
            "project_number": source.project_number,
            "status_field": source.status_field,
            "priority_field": source.priority_field,
            "initiative_field": source.initiative_field,
            "type_field": source.type_field,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_payload(
    snapshot: TicketGraphSnapshot,
    source: GitHubTicketGraphConfig,
    authority_digest: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": snapshot.graph.schema_version,
        "program_issue": snapshot.graph.program_issue,
        "nodes": [node_payload(node) for node in snapshot.graph.nodes],
        "observed_at": snapshot.observed_at,
        "evidence_complete": snapshot.evidence_complete,
        "unavailable": list(snapshot.unavailable),
        "truncated": snapshot.truncated,
        "capability_coverage": capability_coverage_payload(snapshot),
        "live_issues": [
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "body": issue.body,
                "comments": list(issue.comments),
            }
            for issue in snapshot.live_issues
        ],
        "diagnostics": [
            {
                "code": diagnostic.code,
                "issue_number": diagnostic.issue_number,
                "message": diagnostic.message,
            }
            for diagnostic in snapshot.diagnostics
        ],
        "observation_stamps": [
            {
                "capability": stamp.capability.value if stamp.capability is not None else None,
                "source": stamp.source,
                "observed_at": stamp.observed_at,
                "revision": stamp.revision,
                "authority_fingerprint": stamp.authority_fingerprint,
                "complete": stamp.complete,
                "truncated": stamp.truncated,
                "error_codes": list(stamp.error_codes),
                "item_count": stamp.item_count,
            }
            for stamp in snapshot.observation_stamps
        ],
        "issue_refs": [
            {
                "host": ref.host,
                "owner": ref.owner,
                "repository": ref.repository,
                "number": ref.number,
            }
            for ref in snapshot.issue_refs
        ],
    }
    payload["bindings"] = {
        "cache_schema_version": 3,
        "reader_contract_version": TICKET_GRAPH_READER_VERSION,
        "api_version": GITHUB_API_VERSION,
        "repository_slug": snapshot.repository_slug,
        "source_digest": source_digest(source),
        "authority_digest": authority_digest,
        "payload_checksum": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }
    return payload


def payload_bindings_valid(
    payload: object,
    source: GitHubTicketGraphConfig,
    expected_slug: str | None,
    authority_digest: str | None,
) -> bool:
    """Whether a cached payload's bindings match the current reader and config.

    Bindings pin the resolved repository slug, the reviewed source
    configuration digest, the reader/query contract version, and the API
    version, and the checksum is recomputed over the payload body (excluding
    bindings) so a corrupted or hand-edited cache entry fails closed instead of
    being served. An unverifiable expected slug (no configured repository and
    no parseable github.com remote) fails closed to a miss. The authority
    digest must be present and match the operator-pinned credential generation;
    a ``None`` digest (no pinned authority) is never valid because RepoForge
    cannot prove the ambient GitHub authority is the same one that wrote the
    entry.
    """
    if not isinstance(payload, dict):
        return False
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        return False
    if bindings.get("cache_schema_version") != 3:
        return False
    if bindings.get("reader_contract_version") != TICKET_GRAPH_READER_VERSION:
        return False
    if bindings.get("api_version") != GITHUB_API_VERSION:
        return False
    if expected_slug is None or bindings.get("repository_slug") != expected_slug:
        return False
    if bindings.get("source_digest") != source_digest(source):
        return False
    if authority_digest is None or bindings.get("authority_digest") != authority_digest:
        return False
    stored = bindings.get("payload_checksum")
    if not isinstance(stored, str) or len(stored) != 64:
        return False
    body = {key: value for key, value in payload.items() if key != "bindings"}
    computed = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(computed, stored)


def positive_integer_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError
    result = tuple(value)
    if any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in result):
        raise ValueError
    return tuple(sorted(set(result)))


def _strict_int(value: object, field: str) -> int:
    """Return the value when it is an exact int (not a bool); else raise.

    The strict codec never coerces (``int("5")``, ``int(True)``) and never
    silently skips a malformed element: a semantically wrong envelope is
    rejected as a whole (F-006).
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _strict_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _strict_bool(value: object, field: str) -> bool:
    """Return the value when it is an exact bool; never coerce truthy strings/ints.

    JSON ``"false"``, ``0``, and ``1`` are rejected.  A truthy non-bool would
    otherwise flip fail-closed coverage into complete evidence (F-006).
    """
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a bool")
    return value


def snapshot_from_payload(payload: object) -> TicketGraphSnapshot | None:
    if not isinstance(payload, dict):
        return None
    try:
        raw_nodes = payload["nodes"]
        raw_live = payload["live_issues"]
        if not isinstance(raw_nodes, list) or not isinstance(raw_live, list):
            return None
        nodes: list[TicketNode] = []
        for index, raw in enumerate(raw_nodes):
            if not isinstance(raw, dict):
                raise ValueError(f"nodes[{index}] must be an object")
            nodes.append(
                TicketNode(
                    number=_strict_int(raw["number"], f"nodes[{index}].number"),
                    title=_strict_str(raw["title"], f"nodes[{index}].title"),
                    ticket_type=TicketType(_strict_str(raw["type"], f"nodes[{index}].type")),
                    priority=TicketPriority(
                        _strict_str(raw["priority"], f"nodes[{index}].priority")
                    ),
                    status=TicketStatus(_strict_str(raw["status"], f"nodes[{index}].status")),
                    parent=(
                        _strict_int(raw["parent"], f"nodes[{index}].parent")
                        if raw.get("parent") is not None
                        else None
                    ),
                    blockers=positive_integer_tuple(raw["blockers"]),
                    blocks=positive_integer_tuple(raw["blocks"]),
                    children=positive_integer_tuple(raw["children"]),
                    roadmap=tuple(
                        _strict_str(item, f"nodes[{index}].roadmap") for item in raw["roadmap"]
                    ),
                )
            )
        live: list[TicketLiveMetadata] = []
        for index, raw in enumerate(raw_live):
            if not isinstance(raw, dict):
                raise ValueError(f"live_issues[{index}] must be an object")
            live.append(
                TicketLiveMetadata(
                    _strict_int(raw["number"], f"live_issues[{index}].number"),
                    _strict_str(raw["title"], f"live_issues[{index}].title"),
                    _strict_str(raw["state"], f"live_issues[{index}].state"),
                    _strict_str(raw["body"], f"live_issues[{index}].body"),
                    tuple(
                        _strict_str(item, f"live_issues[{index}].comments")
                        for item in raw.get("comments", [])
                    ),
                )
            )
        observed_at = _strict_str(payload["observed_at"], "observed_at")
        evidence_complete = _strict_bool(payload["evidence_complete"], "evidence_complete")
        truncated = _strict_bool(payload["truncated"], "truncated")
        raw_coverage = payload.get("capability_coverage", [])
        if not isinstance(raw_coverage, list):
            return None
        capability_coverage: list[CapabilityCoverage] = []
        for index, item in enumerate(raw_coverage):
            if not isinstance(item, dict):
                raise ValueError(f"capability_coverage[{index}] must be an object")
            capability_coverage.append(
                CapabilityCoverage(
                    capability=GraphEvidenceCapability(
                        _strict_str(item["capability"], f"capability_coverage[{index}].capability")
                    ),
                    complete=_strict_bool(
                        item["complete"], f"capability_coverage[{index}].complete"
                    ),
                    unavailable=positive_integer_tuple(item["unavailable"]),
                    truncated=_strict_bool(
                        item["truncated"], f"capability_coverage[{index}].truncated"
                    ),
                )
            )
        graph = TicketGraph(
            _strict_int(payload["schema_version"], "schema_version"),
            _strict_int(payload["program_issue"], "program_issue"),
            tuple(nodes),
        )
        diagnostics: list[TicketDiagnostic] = []
        raw_diagnostics = payload.get("diagnostics", [])
        if not isinstance(raw_diagnostics, list):
            return None
        for index, item in enumerate(raw_diagnostics):
            if not isinstance(item, dict):
                raise ValueError(f"diagnostics[{index}] must be an object")
            diagnostics.append(
                TicketDiagnostic(
                    code=_strict_str(item.get("code", ""), f"diagnostics[{index}].code"),
                    issue_number=_strict_int(
                        item["issue_number"], f"diagnostics[{index}].issue_number"
                    ),
                    message=_strict_str(item.get("message", ""), f"diagnostics[{index}].message"),
                )
            )
        bindings = payload.get("bindings")
        repository_slug = (
            bindings.get("repository_slug")
            if isinstance(bindings, dict) and isinstance(bindings.get("repository_slug"), str)
            else None
        )
        raw_stamps = payload.get("observation_stamps", [])
        if not isinstance(raw_stamps, list):
            return None
        observation_stamps: list[ObservationStamp] = []
        seen_stamp_capabilities: set[GraphEvidenceCapability] = set()
        for index, item in enumerate(raw_stamps):
            if not isinstance(item, dict):
                raise ValueError(f"observation_stamps[{index}] must be an object")
            raw_capability = item.get("capability")
            if raw_capability is None:
                raise ValueError(f"observation_stamps[{index}].capability is required")
            capability = GraphEvidenceCapability(
                _strict_str(raw_capability, f"observation_stamps[{index}].capability")
            )
            if capability in seen_stamp_capabilities:
                raise ValueError(
                    f"observation_stamps[{index}].capability {capability.value} is duplicated"
                )
            seen_stamp_capabilities.add(capability)
            raw_error_codes = item.get("error_codes", [])
            if not isinstance(raw_error_codes, list):
                raise ValueError(f"observation_stamps[{index}].error_codes must be a list")
            error_codes = tuple(
                _strict_str(code, f"observation_stamps[{index}].error_codes[{code_index}]")
                for code_index, code in enumerate(raw_error_codes)
            )
            raw_revision = item.get("revision")
            revision = (
                _strict_str(raw_revision, f"observation_stamps[{index}].revision")
                if raw_revision is not None
                else None
            )
            raw_authority = item.get("authority_fingerprint")
            authority_fingerprint = (
                _strict_str(raw_authority, f"observation_stamps[{index}].authority_fingerprint")
                if raw_authority is not None
                else None
            )
            observation_stamps.append(
                ObservationStamp(
                    capability=capability,
                    source=_strict_str(item["source"], f"observation_stamps[{index}].source"),
                    observed_at=_strict_str(
                        item["observed_at"], f"observation_stamps[{index}].observed_at"
                    ),
                    revision=revision,
                    authority_fingerprint=authority_fingerprint,
                    complete=_strict_bool(
                        item["complete"], f"observation_stamps[{index}].complete"
                    ),
                    truncated=_strict_bool(
                        item["truncated"], f"observation_stamps[{index}].truncated"
                    ),
                    error_codes=error_codes,
                    item_count=_strict_int(
                        item["item_count"], f"observation_stamps[{index}].item_count"
                    ),
                )
            )
        coverage_by_capability = {item.capability: item for item in capability_coverage}
        if len(coverage_by_capability) != len(capability_coverage):
            raise ValueError("capability_coverage must not contain duplicate capabilities")
        for stamp in observation_stamps:
            stamp_capability = stamp.capability
            if stamp_capability is None:
                raise ValueError("observation stamp capability is required")
            coverage = coverage_by_capability.get(stamp_capability)
            if coverage is None:
                raise ValueError(
                    f"observation stamp for {stamp_capability.value} has no matching coverage entry"
                )
            if stamp.complete != coverage.complete or stamp.truncated != coverage.truncated:
                raise ValueError(
                    f"observation stamp for {stamp_capability.value} disagrees with coverage "
                    f"(complete={stamp.complete!r}/{coverage.complete!r}, "
                    f"truncated={stamp.truncated!r}/{coverage.truncated!r})"
                )
        raw_refs = payload.get("issue_refs", [])
        if not isinstance(raw_refs, list):
            return None
        issue_refs: list[IssueRef] = []
        for index, item in enumerate(raw_refs):
            if not isinstance(item, dict):
                raise ValueError(f"issue_refs[{index}] must be an object")
            issue_refs.append(
                IssueRef(
                    host=_strict_str(item["host"], f"issue_refs[{index}].host"),
                    owner=_strict_str(item["owner"], f"issue_refs[{index}].owner"),
                    repository=_strict_str(item["repository"], f"issue_refs[{index}].repository"),
                    number=_strict_int(item["number"], f"issue_refs[{index}].number"),
                )
            )
        return TicketGraphSnapshot(
            graph=graph,
            observed_at=observed_at,
            evidence_complete=evidence_complete,
            unavailable=positive_integer_tuple(payload["unavailable"]),
            truncated=truncated,
            live_issues=tuple(live),
            capability_coverage=tuple(capability_coverage),
            diagnostics=tuple(diagnostics),
            repository_slug=repository_slug,
            observation_stamps=tuple(observation_stamps),
            issue_refs=tuple(issue_refs),
        )
    except (KeyError, TypeError, ValueError):
        return None


def observed_age_ms(now_epoch: float, observed_at: str) -> float | None:
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    if observed.tzinfo is None:
        return None
    age_ms = (now_epoch - observed.timestamp()) * 1000
    if age_ms < -_ALLOWED_CACHE_SKEW_MS:
        # The envelope predates the local clock by more than the allowed
        # skew; serving it as current evidence would crash output validation
        # (`cache_age_ms` is bounded by `ge=0`). The caller treats `None` as
        # a miss and forces a fresh read.
        return None
    return round(max(0.0, age_ms), 3)


__all__ = [
    "_ALLOWED_CACHE_SKEW_MS",
    "observed_age_ms",
    "payload_bindings_valid",
    "positive_integer_tuple",
    "snapshot_from_payload",
    "snapshot_payload",
    "source_digest",
]
