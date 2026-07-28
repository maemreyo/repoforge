"""Immutable desired GitHub issue-graph proposals and deterministic planning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum

from .errors import ErrorCode, RepoForgeError

ISSUE_GRAPH_PROPOSAL_SCHEMA_VERSION = 1
_MAX_NODES = 100
_MAX_EDGES = 500
_MAX_HIERARCHY_DEPTH = 2
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_GIT_SHA = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_MANAGED_MARKER = re.compile(r"^<!-- repoforge-issue:([A-Za-z0-9][A-Za-z0-9._-]{0,79}) -->$")
_IDENTITY_FIELDS = (
    "repo_id",
    "repository_fingerprint",
    "base_commit_sha",
    "live_snapshot_sha256",
    "active_generation",
    "tool_surface_hash",
    "input_contract_digest",
    "output_contract_digest",
    "template_version",
    "schema_version",
)


def _proposal_error(findings: tuple[IssueGraphFinding, ...]) -> RepoForgeError:
    return RepoForgeError(
        "Desired issue graph is invalid and no proposal was created",
        code=ErrorCode.PROPOSAL_BLOCKED,
        details={"findings": [asdict(item) for item in findings]},
        safe_next_action="Correct the reported graph findings and create a new read-only proposal.",
    )


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def managed_marker(client_ref: str) -> str:
    if _SAFE_REF.fullmatch(client_ref) is None:
        raise ValueError("client_ref is invalid")
    return f"<!-- repoforge-issue:{client_ref} -->"


class IssueEdgeKind(str, Enum):
    BLOCKED_BY = "blocked_by"
    RELATES = "relates"
    SUPERSEDES = "supersedes"


@dataclass(frozen=True, slots=True)
class IssueNodeDraft:
    client_ref: str
    title: str
    ticket_type: str
    priority: str
    status: str
    parent_ref: str | None
    body: str

    def __post_init__(self) -> None:
        if _SAFE_REF.fullmatch(self.client_ref) is None:
            raise ValueError("client_ref is invalid")
        if not self.title.strip() or len(self.title) > 1_000:
            raise ValueError("issue title is invalid")
        if self.ticket_type not in {"program", "epic", "task"}:
            raise ValueError("ticket_type is invalid")
        if self.priority not in {"p0", "p1", "p2", "p3"}:
            raise ValueError("priority is invalid")
        if self.status not in {"planned", "ready", "in_progress", "blocked", "done"}:
            raise ValueError("status is invalid")
        if self.parent_ref is not None and _SAFE_REF.fullmatch(self.parent_ref) is None:
            raise ValueError("parent_ref is invalid")
        if not self.body or len(self.body.encode("utf-8")) > 20_000:
            raise ValueError("issue body is invalid")


@dataclass(frozen=True, slots=True)
class IssueEdgeDraft:
    source_ref: str
    target_ref: str
    kind: IssueEdgeKind

    def __post_init__(self) -> None:
        if (
            _SAFE_REF.fullmatch(self.source_ref) is None
            or _SAFE_REF.fullmatch(self.target_ref) is None
        ):
            raise ValueError("issue edge reference is invalid")
        if not isinstance(self.kind, IssueEdgeKind):
            raise ValueError("issue edge kind is invalid")


@dataclass(frozen=True, slots=True)
class IssueGraphDraft:
    repo_id: str
    root_ref: str
    nodes: tuple[IssueNodeDraft, ...]
    edges: tuple[IssueEdgeDraft, ...]

    def __post_init__(self) -> None:
        if _SAFE_REF.fullmatch(self.repo_id) is None:
            raise ValueError("repo_id is invalid")
        if _SAFE_REF.fullmatch(self.root_ref) is None:
            raise ValueError("root_ref is invalid")
        if not isinstance(self.nodes, tuple) or not isinstance(self.edges, tuple):
            raise ValueError("issue graph collections must be immutable tuples")
        if not self.nodes or len(self.nodes) > _MAX_NODES:
            raise ValueError("issue graph node count is invalid")
        if len(self.edges) > _MAX_EDGES:
            raise ValueError("issue graph edge count is invalid")


@dataclass(frozen=True, slots=True)
class IssueGraphIdentity:
    repo_id: str
    repository_fingerprint: str
    base_commit_sha: str
    live_snapshot_sha256: str
    active_generation: int
    tool_surface_hash: str
    input_contract_digest: str
    output_contract_digest: str
    template_version: int
    schema_version: int

    def __post_init__(self) -> None:
        if _SAFE_REF.fullmatch(self.repo_id) is None:
            raise ValueError("identity repo_id is invalid")
        for field in (
            "repository_fingerprint",
            "live_snapshot_sha256",
            "tool_surface_hash",
            "input_contract_digest",
            "output_contract_digest",
        ):
            if _SHA256.fullmatch(str(getattr(self, field))) is None:
                raise ValueError(f"identity {field} must be a lowercase SHA-256")
        if _GIT_SHA.fullmatch(self.base_commit_sha) is None:
            raise ValueError("identity base_commit_sha is invalid")
        if self.active_generation <= 0 or self.template_version <= 0 or self.schema_version <= 0:
            raise ValueError("identity versions must be positive")

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LiveIssueCandidate:
    issue_number: int
    title: str
    managed_marker: str | None

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("live issue number must be positive")
        if not self.title or len(self.title) > 1_000:
            raise ValueError("live issue title is invalid")
        if self.managed_marker is not None and len(self.managed_marker) > 160:
            raise ValueError("managed marker is invalid")


@dataclass(frozen=True, slots=True)
class IssueGraphFinding:
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class IssueGraphDelta:
    exact_matches: tuple[tuple[str, int], ...]
    create_refs: tuple[str, ...]
    duplicate_candidates: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class IssueGraphProposal:
    proposal_id: str
    proposal_hash: str
    draft: IssueGraphDraft
    identity: IssueGraphIdentity
    canonical_json: str
    publication_order: tuple[str, ...]
    rendered_nodes: tuple[tuple[str, str], ...]
    revision_comments: tuple[tuple[str, str], ...]
    delta: IssueGraphDelta
    created_at: str
    expires_at: str
    external_writes: int = 0
    schema_version: int = ISSUE_GRAPH_PROPOSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not re.fullmatch(r"igp-[a-f0-9]{24}", self.proposal_id):
            raise ValueError("proposal_id is invalid")
        if _SHA256.fullmatch(self.proposal_hash) is None:
            raise ValueError("proposal_hash is invalid")
        if _hash_json(json.loads(self.canonical_json)) != self.proposal_hash:
            raise ValueError("proposal hash does not match canonical content")
        if _timestamp(self.expires_at, "expires_at") <= _timestamp(self.created_at, "created_at"):
            raise ValueError("proposal expiry must be later than creation")
        if self.external_writes != 0:
            raise ValueError("planning proposals cannot contain external writes")
        if self.schema_version != ISSUE_GRAPH_PROPOSAL_SCHEMA_VERSION:
            raise ValueError("unsupported issue graph proposal schema")


def _finding(code: str, path: str, message: str) -> IssueGraphFinding:
    return IssueGraphFinding(code, path, message)


def _node_payload(node: IssueNodeDraft) -> dict[str, object]:
    return {
        "client_ref": node.client_ref,
        "title": node.title,
        "ticket_type": node.ticket_type,
        "priority": node.priority,
        "status": node.status,
        "parent_ref": node.parent_ref,
        "body": node.body,
    }


def _edge_payload(edge: IssueEdgeDraft) -> dict[str, object]:
    return {
        "source_ref": edge.source_ref,
        "target_ref": edge.target_ref,
        "kind": edge.kind.value,
    }


def _validate_graph(draft: IssueGraphDraft) -> tuple[IssueGraphFinding, ...]:
    findings: list[IssueGraphFinding] = []
    by_ref: dict[str, IssueNodeDraft] = {}
    for index, node in enumerate(draft.nodes):
        if node.client_ref in by_ref:
            findings.append(
                _finding(
                    "DUPLICATE_CLIENT_REF",
                    f"nodes[{index}].client_ref",
                    f"client_ref {node.client_ref!r} is repeated",
                )
            )
        by_ref[node.client_ref] = node
        missing_sections = tuple(
            section
            for section in ("## Objective", "## Acceptance criteria")
            if section not in node.body
        )
        if missing_sections:
            findings.append(
                _finding(
                    "REQUIRED_SECTION_MISSING",
                    f"nodes[{index}].body",
                    "missing required sections: " + ", ".join(missing_sections),
                )
            )
    if draft.root_ref not in by_ref:
        findings.append(
            _finding("ROOT_NOT_FOUND", "root_ref", "root_ref does not resolve to a node")
        )

    for index, node in enumerate(draft.nodes):
        if node.parent_ref is not None and node.parent_ref not in by_ref:
            findings.append(
                _finding(
                    "UNKNOWN_PARENT",
                    f"nodes[{index}].parent_ref",
                    f"parent {node.parent_ref!r} does not resolve",
                )
            )
        if node.client_ref == draft.root_ref and node.parent_ref is not None:
            findings.append(
                _finding(
                    "ROOT_HAS_PARENT", f"nodes[{index}].parent_ref", "root cannot have a parent"
                )
            )

    seen_edges: set[tuple[str, str, IssueEdgeKind]] = set()
    for index, edge in enumerate(draft.edges):
        key = (edge.source_ref, edge.target_ref, edge.kind)
        if key in seen_edges:
            findings.append(_finding("DUPLICATE_EDGE", f"edges[{index}]", "edge is duplicated"))
        seen_edges.add(key)
        for field, ref in (("source_ref", edge.source_ref), ("target_ref", edge.target_ref)):
            if ref not in by_ref:
                findings.append(
                    _finding(
                        "UNRESOLVED_REFERENCE",
                        f"edges[{index}].{field}",
                        f"reference {ref!r} does not resolve",
                    )
                )
        if edge.source_ref == edge.target_ref:
            findings.append(
                _finding("SELF_EDGE", f"edges[{index}]", "self-referential edges are invalid")
            )
        reciprocal = (edge.target_ref, edge.source_ref, edge.kind)
        if (
            edge.kind in {IssueEdgeKind.BLOCKED_BY, IssueEdgeKind.SUPERSEDES}
            and reciprocal in seen_edges
        ):
            findings.append(
                _finding(
                    "CONFLICTING_RECIPROCAL_INTENT",
                    f"edges[{index}]",
                    "directed edge intent conflicts with its reciprocal",
                )
            )

    # GitHub-native sub-issues are bounded to root -> child -> grandchild here.
    for node in draft.nodes:
        depth = 0
        current = node
        parent_visited: set[str] = set()
        while current.parent_ref is not None and current.parent_ref in by_ref:
            if current.client_ref in parent_visited:
                findings.append(
                    _finding(
                        "GRAPH_CYCLE",
                        f"node:{node.client_ref}",
                        "parent membership contains a cycle",
                    )
                )
                break
            parent_visited.add(current.client_ref)
            depth += 1
            current = by_ref[current.parent_ref]
        if depth > _MAX_HIERARCHY_DEPTH:
            findings.append(
                _finding(
                    "HIERARCHY_DEPTH_UNSUPPORTED",
                    f"node:{node.client_ref}",
                    f"hierarchy depth {depth} exceeds {_MAX_HIERARCHY_DEPTH}",
                )
            )

    # Parent constraints and directed dependency constraints share one cycle detector.
    adjacency: dict[str, set[str]] = {ref: set() for ref in by_ref}
    for node in draft.nodes:
        if node.parent_ref in by_ref:
            adjacency[node.parent_ref].add(node.client_ref)
    for edge in draft.edges:
        if edge.source_ref not in by_ref or edge.target_ref not in by_ref:
            continue
        if edge.kind in {IssueEdgeKind.BLOCKED_BY, IssueEdgeKind.SUPERSEDES}:
            adjacency[edge.target_ref].add(edge.source_ref)

    visiting: set[str] = set()
    graph_visited: set[str] = set()

    def visit(ref: str) -> bool:
        if ref in visiting:
            return True
        if ref in graph_visited:
            return False
        visiting.add(ref)
        cycle = any(visit(child) for child in sorted(adjacency[ref]))
        visiting.remove(ref)
        graph_visited.add(ref)
        return cycle

    if any(visit(ref) for ref in sorted(adjacency)):
        findings.append(_finding("GRAPH_CYCLE", "graph", "issue graph contains a cycle"))

    return tuple(sorted(set(findings), key=lambda item: (item.code, item.path, item.message)))


def _live_delta(
    nodes: tuple[IssueNodeDraft, ...],
    live_issues: tuple[LiveIssueCandidate, ...],
) -> tuple[IssueGraphDelta, tuple[IssueGraphFinding, ...]]:
    marker_owners: dict[str, list[int]] = {}
    for issue in live_issues:
        if issue.managed_marker is None:
            continue
        match = _MANAGED_MARKER.fullmatch(issue.managed_marker.strip())
        if match is not None:
            marker_owners.setdefault(match.group(1), []).append(issue.issue_number)

    findings: list[IssueGraphFinding] = []
    exact: list[tuple[str, int]] = []
    candidates: list[tuple[str, int]] = []
    creates: list[str] = []
    for node in sorted(nodes, key=lambda item: item.client_ref):
        owners = sorted(set(marker_owners.get(node.client_ref, ())))
        if len(owners) > 1:
            findings.append(
                _finding(
                    "CONFLICTING_MANAGED_MARKER",
                    f"marker:{node.client_ref}",
                    "managed marker resolves to multiple live issues: "
                    + ", ".join(str(item) for item in owners),
                )
            )
            continue
        if owners:
            exact.append((node.client_ref, owners[0]))
        else:
            creates.append(node.client_ref)
        for issue in live_issues:
            if issue.managed_marker is None and issue.title.casefold() == node.title.casefold():
                candidates.append((node.client_ref, issue.issue_number))

    return (
        IssueGraphDelta(
            tuple(exact),
            tuple(creates),
            tuple(sorted(set(candidates))),
        ),
        tuple(sorted(findings, key=lambda item: (item.code, item.path, item.message))),
    )


def _topological_nodes(draft: IssueGraphDraft) -> tuple[str, ...]:
    refs = {node.client_ref for node in draft.nodes}
    dependencies: dict[str, set[str]] = {ref: set() for ref in refs}
    for node in draft.nodes:
        if node.parent_ref in refs:
            dependencies[node.client_ref].add(node.parent_ref)
    for edge in draft.edges:
        if edge.source_ref not in refs or edge.target_ref not in refs:
            continue
        if edge.kind in {IssueEdgeKind.BLOCKED_BY, IssueEdgeKind.SUPERSEDES}:
            dependencies[edge.source_ref].add(edge.target_ref)

    remaining = {ref: set(values) for ref, values in dependencies.items()}
    ordered: list[str] = []
    while remaining:
        ready = sorted(ref for ref, deps in remaining.items() if not deps)
        if not ready:
            raise _proposal_error(
                (_finding("GRAPH_CYCLE", "graph", "issue graph contains a cycle"),)
            )
        for ref in ready:
            ordered.append(ref)
            remaining.pop(ref)
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(ordered)


def _render_node(node: IssueNodeDraft, draft: IssueGraphDraft) -> str:
    graph_lines = ["## RepoForge graph intent"]
    if node.parent_ref is not None:
        graph_lines.append(f"Parent: `{node.parent_ref}`")
    for kind, label in (
        (IssueEdgeKind.BLOCKED_BY, "Blocked by"),
        (IssueEdgeKind.RELATES, "Relates"),
        (IssueEdgeKind.SUPERSEDES, "Supersedes"),
    ):
        targets = sorted(
            edge.target_ref
            for edge in draft.edges
            if edge.source_ref == node.client_ref and edge.kind is kind
        )
        if targets:
            graph_lines.append(label + ": " + ", ".join(f"`{ref}`" for ref in targets))
    if node.client_ref == draft.root_ref:
        graph_lines.extend(("", "## Delivery checklist"))
        children = sorted(
            (item for item in draft.nodes if item.parent_ref == draft.root_ref),
            key=lambda item: item.client_ref,
        )
        graph_lines.extend(f"- [ ] {item.client_ref} — {item.title}" for item in children)
    return (
        managed_marker(node.client_ref)
        + "\n\n"
        + node.body.rstrip()
        + "\n\n"
        + "\n".join(graph_lines)
        + "\n"
    )


def _revision_comment(node: IssueNodeDraft, identity: IssueGraphIdentity) -> str:
    return (
        f"<!-- repoforge-revision:{node.client_ref}:v{identity.template_version} -->\n"
        f"Desired-state revision for `{node.client_ref}` bound to live snapshot "
        f"`{identity.live_snapshot_sha256}` and generation `{identity.active_generation}`."
    )


def _proposal_payload(
    draft: IssueGraphDraft,
    identity: IssueGraphIdentity,
    publication_order: tuple[str, ...],
    rendered_nodes: tuple[tuple[str, str], ...],
    revision_comments: tuple[tuple[str, str], ...],
    delta: IssueGraphDelta,
    created_at: str,
    expires_at: str,
) -> dict[str, object]:
    return {
        "schema_version": ISSUE_GRAPH_PROPOSAL_SCHEMA_VERSION,
        "identity": identity.payload(),
        "draft": {
            "repo_id": draft.repo_id,
            "root_ref": draft.root_ref,
            "nodes": [
                _node_payload(node)
                for node in sorted(draft.nodes, key=lambda item: item.client_ref)
            ],
            "edges": [
                _edge_payload(edge)
                for edge in sorted(
                    draft.edges,
                    key=lambda item: (item.kind.value, item.source_ref, item.target_ref),
                )
            ],
        },
        "publication_order": list(publication_order),
        "rendered_nodes": [[ref, body] for ref, body in rendered_nodes],
        "revision_comments": [[ref, body] for ref, body in revision_comments],
        "delta": {
            "exact_matches": [[ref, number] for ref, number in delta.exact_matches],
            "create_refs": list(delta.create_refs),
            "duplicate_candidates": [[ref, number] for ref, number in delta.duplicate_candidates],
        },
        "created_at": created_at,
        "expires_at": expires_at,
        "external_writes": 0,
    }


def plan_issue_graph(
    draft: IssueGraphDraft,
    identity: IssueGraphIdentity,
    *,
    live_issues: tuple[LiveIssueCandidate, ...],
    created_at: str,
    expires_at: str,
) -> IssueGraphProposal:
    """Validate and plan one desired graph without performing external effects."""
    if draft.repo_id != identity.repo_id:
        raise _proposal_error(
            (_finding("REPOSITORY_IDENTITY_MISMATCH", "repo_id", "draft and identity differ"),)
        )
    if _timestamp(expires_at, "expires_at") <= _timestamp(created_at, "created_at"):
        raise ValueError("proposal expiry must be later than creation")

    graph_findings = _validate_graph(draft)
    delta, marker_findings = _live_delta(draft.nodes, live_issues)
    findings = tuple(
        sorted(
            graph_findings + marker_findings, key=lambda item: (item.code, item.path, item.message)
        )
    )
    if findings:
        raise _proposal_error(findings)

    node_order = _topological_nodes(draft)
    node_rank = {ref: index for index, ref in enumerate(node_order)}
    parent_steps = tuple(
        f"parent:{node.client_ref}->{node.parent_ref}"
        for node in sorted(
            (item for item in draft.nodes if item.parent_ref is not None),
            key=lambda item: (node_rank[item.client_ref], item.client_ref),
        )
    )
    edge_steps = tuple(
        f"{edge.kind.value}:{edge.source_ref}->{edge.target_ref}"
        for edge in sorted(
            draft.edges,
            key=lambda item: (
                node_rank[item.source_ref],
                item.kind.value,
                item.source_ref,
                item.target_ref,
            ),
        )
    )
    publication_order = (
        tuple(f"node:{ref}" for ref in node_order)
        + parent_steps
        + edge_steps
        + (f"checklist:{draft.root_ref}",)
    )
    by_ref = {node.client_ref: node for node in draft.nodes}
    rendered_nodes = tuple((ref, _render_node(by_ref[ref], draft)) for ref in node_order)
    revision_comments = tuple((ref, _revision_comment(by_ref[ref], identity)) for ref in node_order)
    payload = _proposal_payload(
        draft,
        identity,
        publication_order,
        rendered_nodes,
        revision_comments,
        delta,
        created_at,
        expires_at,
    )
    canonical_json = _canonical_json(payload)
    proposal_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return IssueGraphProposal(
        proposal_id=f"igp-{proposal_hash[:24]}",
        proposal_hash=proposal_hash,
        draft=draft,
        identity=identity,
        canonical_json=canonical_json,
        publication_order=publication_order,
        rendered_nodes=rendered_nodes,
        revision_comments=revision_comments,
        delta=delta,
        created_at=created_at,
        expires_at=expires_at,
    )


def proposal_stale_fields(
    proposal: IssueGraphProposal,
    actual: IssueGraphIdentity,
) -> tuple[str, ...]:
    """Return exact proposal identity dimensions that changed."""
    return tuple(
        field
        for field in _IDENTITY_FIELDS
        if getattr(proposal.identity, field) != getattr(actual, field)
    )


def _object_map(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field} keys must be strings")
        result[key] = item
    return result


def _object_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array")
    return [item for item in value]


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _pairs(value: object, field: str) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for index, item in enumerate(_object_list(value, field)):
        values = _object_list(item, f"{field}[{index}]")
        if len(values) != 2:
            raise ValueError(f"{field}[{index}] must contain two values")
        result.append((str(values[0]), _integer(values[1], f"{field}[{index}][1]")))
    return tuple(result)


def proposal_payload(proposal: IssueGraphProposal) -> dict[str, object]:
    decoded: object = json.loads(proposal.canonical_json)
    return _object_map(decoded, "proposal")


def proposal_from_payload(payload: dict[str, object]) -> IssueGraphProposal:
    identity_raw = _object_map(payload["identity"], "identity")
    draft_raw = _object_map(payload["draft"], "draft")
    delta_raw = _object_map(payload["delta"], "delta")
    nodes_raw = _object_list(draft_raw["nodes"], "draft.nodes")
    edges_raw = _object_list(draft_raw["edges"], "draft.edges")
    rendered_raw = _object_list(payload["rendered_nodes"], "rendered_nodes")
    revision_raw = _object_list(payload["revision_comments"], "revision_comments")
    publication_raw = _object_list(payload["publication_order"], "publication_order")
    draft = IssueGraphDraft(
        repo_id=str(draft_raw["repo_id"]),
        root_ref=str(draft_raw["root_ref"]),
        nodes=tuple(
            IssueNodeDraft(
                client_ref=str(item["client_ref"]),
                title=str(item["title"]),
                ticket_type=str(item["ticket_type"]),
                priority=str(item["priority"]),
                status=str(item["status"]),
                parent_ref=(str(item["parent_ref"]) if item["parent_ref"] is not None else None),
                body=str(item["body"]),
            )
            for item in (_object_map(value, "draft.nodes[]") for value in nodes_raw)
        ),
        edges=tuple(
            IssueEdgeDraft(
                source_ref=str(item["source_ref"]),
                target_ref=str(item["target_ref"]),
                kind=IssueEdgeKind(str(item["kind"])),
            )
            for item in (_object_map(value, "draft.edges[]") for value in edges_raw)
        ),
    )
    identity = IssueGraphIdentity(
        repo_id=str(identity_raw["repo_id"]),
        repository_fingerprint=str(identity_raw["repository_fingerprint"]),
        base_commit_sha=str(identity_raw["base_commit_sha"]),
        live_snapshot_sha256=str(identity_raw["live_snapshot_sha256"]),
        active_generation=_integer(identity_raw["active_generation"], "active_generation"),
        tool_surface_hash=str(identity_raw["tool_surface_hash"]),
        input_contract_digest=str(identity_raw["input_contract_digest"]),
        output_contract_digest=str(identity_raw["output_contract_digest"]),
        template_version=_integer(identity_raw["template_version"], "template_version"),
        schema_version=_integer(identity_raw["schema_version"], "schema_version"),
    )
    delta = IssueGraphDelta(
        exact_matches=_pairs(delta_raw["exact_matches"], "delta.exact_matches"),
        create_refs=tuple(
            str(item) for item in _object_list(delta_raw["create_refs"], "delta.create_refs")
        ),
        duplicate_candidates=_pairs(
            delta_raw["duplicate_candidates"], "delta.duplicate_candidates"
        ),
    )
    canonical_json = _canonical_json(payload)
    proposal_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return IssueGraphProposal(
        proposal_id=f"igp-{proposal_hash[:24]}",
        proposal_hash=proposal_hash,
        draft=draft,
        identity=identity,
        canonical_json=canonical_json,
        publication_order=tuple(str(item) for item in publication_raw),
        rendered_nodes=tuple(
            (str(values[0]), str(values[1]))
            for values in (_object_list(item, "rendered_nodes[]") for item in rendered_raw)
            if len(values) == 2
        ),
        revision_comments=tuple(
            (str(values[0]), str(values[1]))
            for values in (_object_list(item, "revision_comments[]") for item in revision_raw)
            if len(values) == 2
        ),
        delta=delta,
        created_at=str(payload["created_at"]),
        expires_at=str(payload["expires_at"]),
        external_writes=_integer(payload["external_writes"], "external_writes"),
        schema_version=_integer(payload["schema_version"], "schema_version"),
    )


__all__ = [
    "ISSUE_GRAPH_PROPOSAL_SCHEMA_VERSION",
    "IssueEdgeDraft",
    "IssueEdgeKind",
    "IssueGraphDelta",
    "IssueGraphDraft",
    "IssueGraphFinding",
    "IssueGraphIdentity",
    "IssueGraphProposal",
    "IssueNodeDraft",
    "LiveIssueCandidate",
    "managed_marker",
    "plan_issue_graph",
    "proposal_from_payload",
    "proposal_payload",
    "proposal_stale_fields",
]
