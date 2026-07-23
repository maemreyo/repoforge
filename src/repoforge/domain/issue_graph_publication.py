"""Durable desired issue-graph publication plans and saga state."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .errors import ErrorCode, RepoForgeError
from .issue_graph_proposal import (
    IssueEdgeKind,
    IssueGraphIdentity,
    IssueGraphProposal,
    proposal_stale_fields,
)

ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION = 1
_SAFE_HASH = re.compile(r"^[a-f0-9]{64}$")
_PLAN_ID = re.compile(r"^igplan-[a-f0-9]{24}$")
_PUBLICATION_ID = re.compile(r"^igpub-[a-f0-9]{24}$")
_STEP_ID = re.compile(r"^igstep-[a-f0-9]{24}$")
_OPERATION_ID = re.compile(r"^op-[a-f0-9]{24}$")
_RECEIPT_ID = re.compile(r"^receipt-[a-f0-9]{24}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def next_publication_timestamp(previous: str, candidate: str) -> str:
    previous_dt = _timestamp(previous, "previous")
    candidate_dt = _timestamp(candidate, "candidate")
    if candidate_dt <= previous_dt:
        return (previous_dt + timedelta(microseconds=1)).isoformat()
    return candidate_dt.isoformat()


class PublicationStepKind(str, Enum):
    UPDATE_NODE = "update_node"
    CREATE_NODE = "create_node"
    ADOPT_NODE = "adopt_node"
    ADD_SUB_ISSUE = "add_sub_issue"
    REMOVE_SUB_ISSUE = "remove_sub_issue"
    ADD_DEPENDENCY = "add_dependency"
    REMOVE_DEPENDENCY = "remove_dependency"
    UPDATE_EPIC = "update_epic"


class PublicationStepState(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    RECONCILED_EXISTING = "reconciled_existing"
    PAUSED_RATE_LIMIT = "paused_rate_limit"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    FAILED_AFTER_EFFECT = "failed_after_effect"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


class PublicationState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


@dataclass(frozen=True, slots=True)
class PublicationProviderIdentity:
    provider: str
    api_version: str
    media_type: str
    adapter: str
    capability_hash: str

    def __post_init__(self) -> None:
        if self.provider != "github":
            raise ValueError("publication provider must be github")
        if not self.api_version or len(self.api_version) > 80:
            raise ValueError("publication API version is invalid")
        if not self.media_type or len(self.media_type) > 160:
            raise ValueError("publication media type is invalid")
        if not self.adapter or len(self.adapter) > 160:
            raise ValueError("publication adapter identity is invalid")
        if _SAFE_HASH.fullmatch(self.capability_hash) is None:
            raise ValueError("publication capability hash is invalid")


@dataclass(frozen=True, slots=True)
class PublicationLiveNode:
    client_ref: str
    issue_number: int
    database_id: int
    managed: bool
    title: str
    body: str

    def __post_init__(self) -> None:
        if _SAFE_REF.fullmatch(self.client_ref) is None:
            raise ValueError("publication live client_ref is invalid")
        if self.issue_number <= 0 or self.database_id <= 0:
            raise ValueError("publication live issue identity is invalid")
        if not self.title or not self.body:
            raise ValueError("publication live issue content is invalid")


RelationshipPairs = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PublicationLiveGraph:
    nodes: tuple[PublicationLiveNode, ...]
    parent_by_ref: RelationshipPairs = ()
    blocked_by_refs: RelationshipPairs = ()
    snapshot_sha256: str = ""

    def __post_init__(self) -> None:
        if _SAFE_HASH.fullmatch(self.snapshot_sha256) is None:
            raise ValueError("publication live snapshot hash is invalid")
        if len({node.client_ref for node in self.nodes}) != len(self.nodes):
            raise ValueError("publication live graph contains duplicate client refs")

    @staticmethod
    def _pairs(value: RelationshipPairs) -> tuple[tuple[str, str], ...]:
        for source, target in value:
            if _SAFE_REF.fullmatch(source) is None or _SAFE_REF.fullmatch(target) is None:
                raise ValueError("publication relationship evidence is invalid")
        return value

    def parent_map(self) -> dict[str, str]:
        return dict(self._pairs(self.parent_by_ref))

    def blocked_by_sets(self) -> dict[str, frozenset[str]]:
        grouped: dict[str, set[str]] = {}
        for source, target in self._pairs(self.blocked_by_refs):
            grouped.setdefault(source, set()).add(target)
        return {source: frozenset(targets) for source, targets in grouped.items()}


@dataclass(frozen=True, slots=True)
class IssueGraphPublicationStep:
    step_id: str
    ordinal: int
    kind: PublicationStepKind
    source_ref: str
    target_ref: str | None
    title: str | None
    body: str | None
    expected_issue_number: int | None
    state: PublicationStepState = PublicationStepState.PENDING
    issue_number: int | None = None
    operation_id: str | None = None
    receipt_id: str | None = None
    result_reference: str | None = None
    external_writes: int = 0
    provider_identity: PublicationProviderIdentity | None = None

    def __post_init__(self) -> None:
        if _STEP_ID.fullmatch(self.step_id) is None or self.ordinal < 0:
            raise ValueError("publication step identity is invalid")
        if _SAFE_REF.fullmatch(self.source_ref) is None:
            raise ValueError("publication step source_ref is invalid")
        if self.target_ref is not None and _SAFE_REF.fullmatch(self.target_ref) is None:
            raise ValueError("publication step target_ref is invalid")
        if self.expected_issue_number is not None and self.expected_issue_number <= 0:
            raise ValueError("publication step expected issue number is invalid")
        if self.issue_number is not None and self.issue_number <= 0:
            raise ValueError("publication step issue number is invalid")
        if self.operation_id is not None and _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("publication step operation id is invalid")
        if self.receipt_id is not None and _RECEIPT_ID.fullmatch(self.receipt_id) is None:
            raise ValueError("publication step receipt id is invalid")
        if self.external_writes < 0 or self.external_writes > 20:
            raise ValueError("publication step external writes are invalid")


@dataclass(frozen=True, slots=True)
class IssueGraphPublicationPlan:
    plan_id: str
    proposal_id: str
    proposal_hash: str
    identity: IssueGraphIdentity
    effect_plan_hash: str
    provider_identity: PublicationProviderIdentity
    steps: tuple[IssueGraphPublicationStep, ...]
    initial_mapping: tuple[tuple[str, int], ...]
    adopt_refs: tuple[str, ...]
    created_at: str
    expires_at: str
    schema_version: int = ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if _PLAN_ID.fullmatch(self.plan_id) is None:
            raise ValueError("publication plan id is invalid")
        if _SAFE_HASH.fullmatch(self.proposal_hash) is None:
            raise ValueError("publication proposal hash is invalid")
        if _SAFE_HASH.fullmatch(self.effect_plan_hash) is None:
            raise ValueError("publication effect plan hash is invalid")
        if tuple(step.ordinal for step in self.steps) != tuple(range(len(self.steps))):
            raise ValueError("publication step ordinals are not contiguous")
        if _timestamp(self.expires_at, "expires_at") <= _timestamp(self.created_at, "created_at"):
            raise ValueError("publication plan expiry must follow creation")
        if self.schema_version != ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION:
            raise ValueError("publication plan schema is unsupported")


@dataclass(frozen=True, slots=True)
class IssueGraphPublication:
    publication_id: str
    plan_id: str
    proposal_id: str
    proposal_hash: str
    effect_plan_hash: str
    identity: IssueGraphIdentity
    provider_identity: PublicationProviderIdentity
    state: PublicationState
    steps: tuple[IssueGraphPublicationStep, ...]
    node_mapping: tuple[tuple[str, int], ...]
    operation_id: str
    receipt_id: str
    result_reference: str | None
    retry_at: str | None
    external_writes: int
    created_at: str
    updated_at: str
    expires_at: str
    schema_version: int = ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if _PUBLICATION_ID.fullmatch(self.publication_id) is None:
            raise ValueError("publication id is invalid")
        if _PLAN_ID.fullmatch(self.plan_id) is None:
            raise ValueError("publication plan id is invalid")
        if (
            _SAFE_HASH.fullmatch(self.proposal_hash) is None
            or _SAFE_HASH.fullmatch(self.effect_plan_hash) is None
        ):
            raise ValueError("publication hash identity is invalid")
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("publication operation id is invalid")
        if _RECEIPT_ID.fullmatch(self.receipt_id) is None:
            raise ValueError("publication receipt id is invalid")
        if self.external_writes < 0:
            raise ValueError("publication external writes are invalid")
        if self.retry_at is not None:
            _timestamp(self.retry_at, "retry_at")
        if _timestamp(self.updated_at, "updated_at") < _timestamp(self.created_at, "created_at"):
            raise ValueError("publication updated_at precedes creation")
        if self.schema_version != ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION:
            raise ValueError("publication schema is unsupported")


def _step_payload(step: IssueGraphPublicationStep, *, include_result: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "step_id": step.step_id,
        "ordinal": step.ordinal,
        "kind": step.kind.value,
        "source_ref": step.source_ref,
        "target_ref": step.target_ref,
        "title": step.title,
        "body": step.body,
        "expected_issue_number": step.expected_issue_number,
        "state": step.state.value,
    }
    if include_result:
        payload.update(
            {
                "issue_number": step.issue_number,
                "operation_id": step.operation_id,
                "receipt_id": step.receipt_id,
                "result_reference": step.result_reference,
                "external_writes": step.external_writes,
                "provider_identity": (
                    asdict(step.provider_identity) if step.provider_identity is not None else None
                ),
            }
        )
    return payload


def _new_step(
    proposal_hash: str,
    ordinal: int,
    kind: PublicationStepKind,
    source_ref: str,
    *,
    target_ref: str | None = None,
    title: str | None = None,
    body: str | None = None,
    expected_issue_number: int | None = None,
) -> IssueGraphPublicationStep:
    semantic = {
        "proposal_hash": proposal_hash,
        "ordinal": ordinal,
        "kind": kind.value,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "title": title,
        "body": body,
        "expected_issue_number": expected_issue_number,
    }
    return IssueGraphPublicationStep(
        step_id=f"igstep-{_digest(semantic)[:24]}",
        ordinal=ordinal,
        kind=kind,
        source_ref=source_ref,
        target_ref=target_ref,
        title=title,
        body=body,
        expected_issue_number=expected_issue_number,
    )


def _stale_error(fields: tuple[str, ...]) -> RepoForgeError:
    return RepoForgeError(
        "Issue graph proposal identity is stale",
        code=ErrorCode.CONFIG_STALE,
        retryable=True,
        details={"stale_fields": list(fields)},
        safe_next_action="Create a new proposal and publication plan from current repository evidence.",
    )


def require_current_publication_identity(
    expected: IssueGraphIdentity,
    actual: IssueGraphIdentity,
) -> None:
    fields = tuple(
        field
        for field in (
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
        if getattr(expected, field) != getattr(actual, field)
    )
    if fields:
        raise _stale_error(fields)


def build_issue_graph_publication_plan(
    proposal: IssueGraphProposal,
    actual_identity: IssueGraphIdentity,
    *,
    live_graph: PublicationLiveGraph,
    adopt_refs: tuple[str, ...],
    provider_identity: PublicationProviderIdentity,
    created_at: str,
    expires_at: str,
) -> IssueGraphPublicationPlan:
    stale = proposal_stale_fields(proposal, actual_identity)
    if stale:
        raise _stale_error(stale)
    if live_graph.snapshot_sha256 != actual_identity.live_snapshot_sha256:
        raise _stale_error(("live_snapshot_sha256",))
    safe_adopt = tuple(sorted(set(adopt_refs)))
    by_ref = {node.client_ref: node for node in proposal.draft.nodes}
    live_by_ref = {node.client_ref: node for node in live_graph.nodes}
    desired_refs = frozenset(by_ref)
    findings: list[dict[str, str]] = []
    for ref, candidate in sorted(live_by_ref.items()):
        if ref in desired_refs and not candidate.managed and ref not in safe_adopt:
            findings.append(
                {
                    "code": "ADOPTION_REQUIRED",
                    "path": f"node:{ref}",
                    "message": "unmanaged existing issue requires explicit adoption",
                }
            )
    if findings:
        raise RepoForgeError(
            "Issue graph publication requires explicit operator decisions",
            code=ErrorCode.PROPOSAL_BLOCKED,
            details={"findings": findings},
            safe_next_action="Review the unmanaged issue and include its client_ref in adopt_refs.",
        )

    steps: list[IssueGraphPublicationStep] = []

    def add(
        kind: PublicationStepKind,
        source_ref: str,
        *,
        target_ref: str | None = None,
        title: str | None = None,
        body: str | None = None,
        expected_issue_number: int | None = None,
    ) -> None:
        steps.append(
            _new_step(
                proposal.proposal_hash,
                len(steps),
                kind,
                source_ref,
                target_ref=target_ref,
                title=title,
                body=body,
                expected_issue_number=expected_issue_number,
            )
        )

    rendered = dict(proposal.rendered_nodes)
    node_order = tuple(
        item.split(":", 1)[1] for item in proposal.publication_order if item.startswith("node:")
    )
    initial_mapping: list[tuple[str, int]] = []
    for ref in node_order:
        node = by_ref[ref]
        mapped_live = live_by_ref.get(ref)
        if mapped_live is None:
            add(
                PublicationStepKind.CREATE_NODE,
                ref,
                title=node.title,
                body=rendered[ref],
            )
            continue
        initial_mapping.append((ref, mapped_live.issue_number))
        add(
            PublicationStepKind.UPDATE_NODE
            if mapped_live.managed
            else PublicationStepKind.ADOPT_NODE,
            ref,
            title=node.title,
            body=rendered[ref],
            expected_issue_number=mapped_live.issue_number,
        )

    current_parents = live_graph.parent_map()
    for node in sorted(proposal.draft.nodes, key=lambda item: item.client_ref):
        if node.parent_ref is None:
            continue
        current_parent = current_parents.get(node.client_ref)
        if current_parent is not None and current_parent != node.parent_ref:
            add(
                PublicationStepKind.REMOVE_SUB_ISSUE,
                current_parent,
                target_ref=node.client_ref,
            )
        if current_parent != node.parent_ref:
            add(
                PublicationStepKind.ADD_SUB_ISSUE,
                node.parent_ref,
                target_ref=node.client_ref,
            )

    current_blockers = live_graph.blocked_by_sets()
    desired_blockers: dict[str, set[str]] = {}
    for edge in proposal.draft.edges:
        if edge.kind is IssueEdgeKind.BLOCKED_BY:
            desired_blockers.setdefault(edge.source_ref, set()).add(edge.target_ref)
    for source_ref in sorted(set(current_blockers) | set(desired_blockers)):
        current = current_blockers.get(source_ref, frozenset())
        desired = frozenset(desired_blockers.get(source_ref, set()))
        for target_ref in sorted(current - desired):
            add(
                PublicationStepKind.REMOVE_DEPENDENCY,
                source_ref,
                target_ref=target_ref,
            )
        for target_ref in sorted(desired - current):
            add(
                PublicationStepKind.ADD_DEPENDENCY,
                source_ref,
                target_ref=target_ref,
            )

    root_live = live_by_ref.get(proposal.draft.root_ref)
    add(
        PublicationStepKind.UPDATE_EPIC,
        proposal.draft.root_ref,
        title=by_ref[proposal.draft.root_ref].title,
        body=rendered[proposal.draft.root_ref],
        expected_issue_number=(root_live.issue_number if root_live is not None else None),
    )
    effect_payload = {
        "proposal_id": proposal.proposal_id,
        "proposal_hash": proposal.proposal_hash,
        "identity": actual_identity.payload(),
        "provider_identity": asdict(provider_identity),
        "steps": [_step_payload(step, include_result=False) for step in steps],
        "initial_mapping": sorted(initial_mapping),
        "adopt_refs": list(safe_adopt),
        "created_at": created_at,
        "expires_at": expires_at,
    }
    effect_plan_hash = _digest(effect_payload)
    plan = IssueGraphPublicationPlan(
        plan_id=f"igplan-{effect_plan_hash[:24]}",
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.proposal_hash,
        identity=actual_identity,
        effect_plan_hash=effect_plan_hash,
        provider_identity=provider_identity,
        steps=tuple(steps),
        initial_mapping=tuple(sorted(initial_mapping)),
        adopt_refs=safe_adopt,
        created_at=created_at,
        expires_at=expires_at,
    )
    return plan


def publication_plan_payload(plan: IssueGraphPublicationPlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "proposal_id": plan.proposal_id,
        "proposal_hash": plan.proposal_hash,
        "identity": plan.identity.payload(),
        "effect_plan_hash": plan.effect_plan_hash,
        "provider_identity": asdict(plan.provider_identity),
        "steps": [_step_payload(step, include_result=True) for step in plan.steps],
        "initial_mapping": [[ref, number] for ref, number in plan.initial_mapping],
        "adopt_refs": list(plan.adopt_refs),
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
        "schema_version": plan.schema_version,
    }


def publication_payload(publication: IssueGraphPublication) -> dict[str, object]:
    return {
        "publication_id": publication.publication_id,
        "plan_id": publication.plan_id,
        "proposal_id": publication.proposal_id,
        "proposal_hash": publication.proposal_hash,
        "effect_plan_hash": publication.effect_plan_hash,
        "identity": publication.identity.payload(),
        "provider_identity": asdict(publication.provider_identity),
        "state": publication.state.value,
        "steps": [_step_payload(step, include_result=True) for step in publication.steps],
        "node_mapping": [[ref, number] for ref, number in publication.node_mapping],
        "operation_id": publication.operation_id,
        "receipt_id": publication.receipt_id,
        "result_reference": publication.result_reference,
        "retry_at": publication.retry_at,
        "external_writes": publication.external_writes,
        "created_at": publication.created_at,
        "updated_at": publication.updated_at,
        "expires_at": publication.expires_at,
        "schema_version": publication.schema_version,
    }


def _identity_from_payload(raw: dict[str, Any]) -> IssueGraphIdentity:
    return IssueGraphIdentity(
        repo_id=str(raw["repo_id"]),
        repository_fingerprint=str(raw["repository_fingerprint"]),
        base_commit_sha=str(raw["base_commit_sha"]),
        live_snapshot_sha256=str(raw["live_snapshot_sha256"]),
        active_generation=int(raw["active_generation"]),
        tool_surface_hash=str(raw["tool_surface_hash"]),
        input_contract_digest=str(raw["input_contract_digest"]),
        output_contract_digest=str(raw["output_contract_digest"]),
        template_version=int(raw["template_version"]),
        schema_version=int(raw["schema_version"]),
    )


def _provider_from_payload(raw: dict[str, Any]) -> PublicationProviderIdentity:
    return PublicationProviderIdentity(
        provider=str(raw["provider"]),
        api_version=str(raw["api_version"]),
        media_type=str(raw["media_type"]),
        adapter=str(raw["adapter"]),
        capability_hash=str(raw["capability_hash"]),
    )


def _step_from_payload(raw: dict[str, Any]) -> IssueGraphPublicationStep:
    provider_raw = raw.get("provider_identity")
    return IssueGraphPublicationStep(
        step_id=str(raw["step_id"]),
        ordinal=int(raw["ordinal"]),
        kind=PublicationStepKind(str(raw["kind"])),
        source_ref=str(raw["source_ref"]),
        target_ref=str(raw["target_ref"]) if raw.get("target_ref") is not None else None,
        title=str(raw["title"]) if raw.get("title") is not None else None,
        body=str(raw["body"]) if raw.get("body") is not None else None,
        expected_issue_number=(
            int(raw["expected_issue_number"])
            if raw.get("expected_issue_number") is not None
            else None
        ),
        state=PublicationStepState(str(raw.get("state", "pending"))),
        issue_number=int(raw["issue_number"]) if raw.get("issue_number") is not None else None,
        operation_id=str(raw["operation_id"]) if raw.get("operation_id") is not None else None,
        receipt_id=str(raw["receipt_id"]) if raw.get("receipt_id") is not None else None,
        result_reference=(
            str(raw["result_reference"]) if raw.get("result_reference") is not None else None
        ),
        external_writes=int(raw.get("external_writes", 0)),
        provider_identity=(
            _provider_from_payload(provider_raw) if isinstance(provider_raw, dict) else None
        ),
    )


def publication_plan_from_payload(payload: dict[str, Any]) -> IssueGraphPublicationPlan:
    return IssueGraphPublicationPlan(
        plan_id=str(payload["plan_id"]),
        proposal_id=str(payload["proposal_id"]),
        proposal_hash=str(payload["proposal_hash"]),
        identity=_identity_from_payload(dict(payload["identity"])),
        effect_plan_hash=str(payload["effect_plan_hash"]),
        provider_identity=_provider_from_payload(dict(payload["provider_identity"])),
        steps=tuple(_step_from_payload(dict(item)) for item in payload["steps"]),
        initial_mapping=tuple(
            (str(ref), int(number)) for ref, number in payload["initial_mapping"]
        ),
        adopt_refs=tuple(str(item) for item in payload["adopt_refs"]),
        created_at=str(payload["created_at"]),
        expires_at=str(payload["expires_at"]),
        schema_version=int(payload["schema_version"]),
    )


def publication_from_payload(payload: dict[str, Any]) -> IssueGraphPublication:
    return IssueGraphPublication(
        publication_id=str(payload["publication_id"]),
        plan_id=str(payload["plan_id"]),
        proposal_id=str(payload["proposal_id"]),
        proposal_hash=str(payload["proposal_hash"]),
        effect_plan_hash=str(payload["effect_plan_hash"]),
        identity=_identity_from_payload(dict(payload["identity"])),
        provider_identity=_provider_from_payload(dict(payload["provider_identity"])),
        state=PublicationState(str(payload["state"])),
        steps=tuple(_step_from_payload(dict(item)) for item in payload["steps"]),
        node_mapping=tuple((str(ref), int(number)) for ref, number in payload["node_mapping"]),
        operation_id=str(payload["operation_id"]),
        receipt_id=str(payload["receipt_id"]),
        result_reference=(
            str(payload["result_reference"])
            if payload.get("result_reference") is not None
            else None
        ),
        retry_at=str(payload["retry_at"]) if payload.get("retry_at") is not None else None,
        external_writes=int(payload["external_writes"]),
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
        expires_at=str(payload["expires_at"]),
        schema_version=int(payload["schema_version"]),
    )


def update_publication_step(
    publication: IssueGraphPublication,
    index: int,
    step: IssueGraphPublicationStep,
    *,
    state: PublicationState | None = None,
    mapping: dict[str, int] | None = None,
    retry_at: str | None = None,
    updated_at: str,
) -> IssueGraphPublication:
    steps = list(publication.steps)
    steps[index] = step
    resolved_mapping = dict(publication.node_mapping) if mapping is None else mapping
    return replace(
        publication,
        steps=tuple(steps),
        node_mapping=tuple(sorted(resolved_mapping.items())),
        state=publication.state if state is None else state,
        retry_at=retry_at,
        external_writes=sum(item.external_writes for item in steps),
        updated_at=next_publication_timestamp(publication.updated_at, updated_at),
    )


__all__ = [
    "ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION",
    "IssueGraphPublication",
    "IssueGraphPublicationPlan",
    "IssueGraphPublicationStep",
    "PublicationLiveGraph",
    "PublicationLiveNode",
    "PublicationProviderIdentity",
    "PublicationState",
    "PublicationStepKind",
    "PublicationStepState",
    "build_issue_graph_publication_plan",
    "publication_from_payload",
    "publication_payload",
    "publication_plan_from_payload",
    "publication_plan_payload",
    "require_current_publication_identity",
    "update_publication_step",
]
