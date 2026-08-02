"""Strict codec for the ticket-graph cache envelope (F-006).

Encodes a :class:`TicketGraphSnapshot` to a versioned dict payload and
decodes it back, with exact-type validation, no silent coercion, and
provenance consistency between coverage and observation stamps.

Bindings and the payload checksum live in
:mod:`issue_graph_cache_bindings`; this module owns the snapshot body.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...domain.observation import IssueRef, ObservationStamp
from ...domain.tickets import (
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
from .issue_graph_cache_bindings import build_bindings
from .issue_graph_payloads import capability_coverage_payload, node_payload

#: Cache envelopes do not survive negative age beyond this skew; a cached
#: snapshot whose observed_at is far in the future relative to the local
#: clock must be discarded instead of served.
_ALLOWED_CACHE_SKEW_MS = 5_000


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

    JSON ``"false"``, ``0``, and ``1`` are rejected. A truthy non-bool would
    otherwise flip fail-closed coverage into complete evidence (F-006).
    """
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a bool")
    return value


def snapshot_payload(
    snapshot: TicketGraphSnapshot,
    source: Any,
    authority_digest: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
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
    body["bindings"] = build_bindings(
        source=source,
        authority_digest=authority_digest,
        repository_slug=snapshot.repository_slug,
        payload_body=body,
    )
    return body


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
        return None
    return round(max(0.0, age_ms), 3)


__all__ = [
    "_ALLOWED_CACHE_SKEW_MS",
    "_strict_bool",
    "_strict_int",
    "_strict_str",
    "observed_age_ms",
    "positive_integer_tuple",
    "snapshot_from_payload",
    "snapshot_payload",
]
