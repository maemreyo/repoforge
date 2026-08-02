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
                "source": stamp.source,
                "observed_at": stamp.observed_at,
                "complete": stamp.complete,
                "truncated": stamp.truncated,
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
        "cache_schema_version": 2,
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
    if bindings.get("cache_schema_version") != 2:
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


def snapshot_from_payload(payload: object) -> TicketGraphSnapshot | None:
    if not isinstance(payload, dict):
        return None
    try:
        raw_nodes = payload["nodes"]
        raw_live = payload["live_issues"]
        if not isinstance(raw_nodes, list) or not isinstance(raw_live, list):
            return None
        nodes = tuple(
            TicketNode(
                number=int(raw["number"]),
                title=str(raw["title"]),
                ticket_type=TicketType(str(raw["type"])),
                priority=TicketPriority(str(raw["priority"])),
                status=TicketStatus(str(raw["status"])),
                parent=int(raw["parent"]) if raw.get("parent") is not None else None,
                blockers=positive_integer_tuple(raw["blockers"]),
                blocks=positive_integer_tuple(raw["blocks"]),
                children=positive_integer_tuple(raw["children"]),
                roadmap=tuple(str(item) for item in raw["roadmap"]),
            )
            for raw in raw_nodes
            if isinstance(raw, dict)
        )
        live = tuple(
            TicketLiveMetadata(
                int(raw["number"]),
                str(raw["title"]),
                str(raw["state"]),
                str(raw["body"]),
                tuple(str(item) for item in raw.get("comments", [])),
            )
            for raw in raw_live
            if isinstance(raw, dict)
        )
        observed_at = payload["observed_at"]
        evidence_complete = payload["evidence_complete"]
        truncated = payload["truncated"]
        if (
            not isinstance(observed_at, str)
            or not isinstance(evidence_complete, bool)
            or not isinstance(truncated, bool)
        ):
            return None
        raw_coverage = payload.get("capability_coverage", [])
        if not isinstance(raw_coverage, list):
            return None
        capability_coverage = tuple(
            CapabilityCoverage(
                capability=GraphEvidenceCapability(str(item["capability"])),
                complete=bool(item["complete"]),
                unavailable=positive_integer_tuple(item["unavailable"]),
                truncated=bool(item["truncated"]),
            )
            for item in raw_coverage
            if isinstance(item, dict)
        )
        graph = TicketGraph(int(payload["schema_version"]), int(payload["program_issue"]), nodes)
        diagnostics: list[TicketDiagnostic] = []
        for item in payload.get("diagnostics", []):
            if not isinstance(item, dict) or not isinstance(item.get("issue_number"), int):
                continue
            diagnostics.append(
                TicketDiagnostic(
                    code=str(item.get("code", "")),
                    issue_number=int(item["issue_number"]),
                    message=str(item.get("message", "")),
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
        for item in raw_stamps:
            if not isinstance(item, dict):
                return None
            observation_stamps.append(
                ObservationStamp(
                    source=item["source"],
                    observed_at=item["observed_at"],
                    complete=item["complete"],
                    truncated=item["truncated"],
                    item_count=item["item_count"],
                )
            )
        raw_refs = payload.get("issue_refs", [])
        if not isinstance(raw_refs, list):
            return None
        issue_refs: list[IssueRef] = []
        for item in raw_refs:
            if not isinstance(item, dict):
                return None
            issue_refs.append(
                IssueRef(
                    host=item["host"],
                    owner=item["owner"],
                    repository=item["repository"],
                    number=item["number"],
                )
            )
        return TicketGraphSnapshot(
            graph=graph,
            observed_at=observed_at,
            evidence_complete=evidence_complete,
            unavailable=positive_integer_tuple(payload["unavailable"]),
            truncated=truncated,
            live_issues=live,
            capability_coverage=capability_coverage,
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
