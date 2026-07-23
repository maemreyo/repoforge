"""Governed public workflow over immutable issue-graph proposals and publication sagas."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from ...domain.approval import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalSubject,
    decide_approval,
)
from ...domain.errors import ConfigError, ErrorCode, RepoForgeError
from ...domain.execution_receipt import EffectReceiptState
from ...domain.issue_graph_proposal import (
    IssueEdgeDraft,
    IssueEdgeKind,
    IssueGraphDraft,
    IssueGraphIdentity,
    IssueNodeDraft,
    LiveIssueCandidate,
)
from ...domain.issue_graph_publication import (
    IssueGraphPublication,
    IssueGraphPublicationPlan,
    PublicationLiveGraph,
    PublicationLiveNode,
    PublicationState,
)
from ...domain.operation_task import OperationState
from ...domain.repository_selection import repository_capability_digest
from ...domain.runtime_contract import RuntimeContractIdentity
from ...ports.background_tasks import BackgroundTaskRunner
from ...ports.issue_graph_proposal_store import IssueGraphProposalStore
from ...ports.issue_graph_publication_store import IssueGraphPublicationStore
from ...ports.issue_mutation import RemoteIssue
from ..context import ApplicationContext
from ..extended_context import approval_stores, issue_mutation_gateway
from ..operations import OperationManager
from .issue_graph_proposal import IssueGraphProposalService
from .issue_graph_publication import (
    IssueGraphPublicationCoordinator,
    RepositoryIssueGraphStepExecutor,
)

_MARKER = re.compile(r"^<!-- repoforge-issue:([A-Za-z0-9][A-Za-z0-9._-]{0,79}) -->$", re.M)


@dataclass(frozen=True, slots=True)
class IssueGraphWorkflowResult:
    action: str
    state: str
    proposal_id: str | None = None
    proposal_hash: str | None = None
    plan_id: str | None = None
    effect_plan_hash: str | None = None
    approval_request_id: str | None = None
    approval_status: str | None = None
    publication_id: str | None = None
    publication_state: str | None = None
    operation_id: str | None = None
    receipt_id: str | None = None
    result_reference: str | None = None
    retry_at: str | None = None
    complete: bool = False
    external_writes: int = 0
    recovery_action: str | None = None


class IssueGraphWorkflowService:
    def __init__(
        self,
        ctx: ApplicationContext,
        operations: OperationManager,
        proposals: IssueGraphProposalStore,
        publications: IssueGraphPublicationStore,
        background_tasks: BackgroundTaskRunner,
    ) -> None:
        self.ctx = ctx
        self.operations = operations
        self.proposals = proposals
        self.publications = publications
        self.background_tasks = background_tasks
        self.proposal_service = IssueGraphProposalService(proposals)

    @staticmethod
    def _int_value(value: object, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{field} must be an integer")
        return value

    @staticmethod
    def _runtime_identity(raw: dict[str, object] | None) -> RuntimeContractIdentity:
        if raw is None:
            raise ConfigError(
                "Current MCP runtime contract identity is required for issue graph management"
            )
        try:
            return RuntimeContractIdentity(
                server_build_sha=str(raw["server_build_sha"]),
                server_version=str(raw["server_version"]),
                active_generation=IssueGraphWorkflowService._int_value(
                    raw["active_generation"], "active_generation"
                ),
                tool_surface_hash=str(raw["tool_surface_hash"]),
                input_contract_digest=str(raw["input_contract_digest"]),
                output_contract_digest=str(raw["output_contract_digest"]),
                runtime_protocol_version=IssueGraphWorkflowService._int_value(
                    raw["runtime_protocol_version"], "runtime_protocol_version"
                ),
                process_start_identity=str(raw["process_start_identity"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("Current MCP runtime contract identity is invalid") from exc

    @staticmethod
    def _draft(repo_id: str, manage: dict[str, object]) -> IssueGraphDraft:
        nodes_raw = manage.get("nodes")
        edges_raw = manage.get("edges", ())
        if not isinstance(nodes_raw, (list, tuple)) or not isinstance(edges_raw, (list, tuple)):
            raise ValueError("Issue graph manage plan nodes and edges are invalid")
        nodes = tuple(
            IssueNodeDraft(
                client_ref=str(item["client_ref"]),
                title=str(item["title"]),
                ticket_type=str(item["ticket_type"]),
                priority=str(item["priority"]),
                status=str(item["status"]),
                parent_ref=(
                    str(item["parent_ref"]) if item.get("parent_ref") is not None else None
                ),
                body=str(item["body"]),
            )
            for item in nodes_raw
            if isinstance(item, dict)
        )
        edges = tuple(
            IssueEdgeDraft(
                source_ref=str(item["source_ref"]),
                target_ref=str(item["target_ref"]),
                kind=IssueEdgeKind(str(item["kind"])),
            )
            for item in edges_raw
            if isinstance(item, dict)
        )
        return IssueGraphDraft(repo_id, str(manage["root_ref"]), nodes, edges)

    @staticmethod
    def _marker_ref(issue: RemoteIssue) -> str | None:
        match = _MARKER.search(issue.body)
        return match.group(1) if match is not None else None

    @staticmethod
    def _snapshot_hash(
        issues: tuple[RemoteIssue, ...],
        nodes: tuple[PublicationLiveNode, ...],
        parents: tuple[tuple[str, str], ...],
        blockers: tuple[tuple[str, str], ...],
    ) -> str:
        payload = {
            "issues": [
                {
                    "number": issue.issue_number,
                    "database_id": issue.database_id,
                    "title": issue.title,
                    "state": issue.state,
                    "body_sha256": hashlib.sha256(issue.body.encode("utf-8")).hexdigest(),
                }
                for issue in sorted(issues, key=lambda item: item.issue_number)
            ],
            "nodes": [asdict(node) for node in sorted(nodes, key=lambda item: item.client_ref)],
            "parents": [list(item) for item in sorted(parents)],
            "blockers": [list(item) for item in sorted(blockers)],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _live_truth(
        self,
        repo_id: str,
        draft: IssueGraphDraft,
    ) -> tuple[tuple[LiveIssueCandidate, ...], PublicationLiveGraph, str]:
        repo = self.ctx.repo(repo_id)
        gateway = issue_mutation_gateway(self.ctx)
        recent, truncated = gateway.recent_issues(repo.path, max_issues=100)
        if truncated:
            raise RepoForgeError(
                "Issue graph planning evidence is truncated",
                code=ErrorCode.PROPOSAL_BLOCKED,
                details={"missing_coverage": ["recent_issues:truncated"]},
                safe_next_action="Reduce the desired graph scope or reconcile duplicate candidates manually.",
            )
        candidates = tuple(
            LiveIssueCandidate(issue.issue_number, issue.title, self._marker_ref(issue))
            for issue in recent
        )
        desired = {node.client_ref: node for node in draft.nodes}
        mapped: dict[str, RemoteIssue] = {}
        for issue in recent:
            marker = self._marker_ref(issue)
            if marker in desired:
                mapped[str(marker)] = issue
        for ref, node in desired.items():
            if ref in mapped:
                continue
            matches = tuple(issue for issue in recent if issue.title == node.title)
            if len(matches) > 1:
                raise RepoForgeError(
                    "Issue graph title adoption is ambiguous",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                    details={
                        "client_ref": ref,
                        "candidate_numbers": [item.issue_number for item in matches],
                    },
                    safe_next_action="Add exact managed markers or resolve duplicate live issues before planning.",
                )
            if len(matches) == 1:
                mapped[ref] = matches[0]
        number_to_ref = {issue.issue_number: ref for ref, issue in mapped.items()}
        live_nodes = tuple(
            PublicationLiveNode(
                ref,
                issue.issue_number,
                issue.database_id,
                self._marker_ref(issue) == ref,
                issue.title,
                issue.body,
            )
            for ref, issue in sorted(mapped.items())
        )
        parent_pairs: set[tuple[str, str]] = set()
        blocker_pairs: set[tuple[str, str]] = set()
        for source_ref, issue in sorted(mapped.items()):
            children, children_truncated = gateway.sub_issues(
                repo.path, issue.issue_number, max_issues=100
            )
            blockers, blockers_truncated = gateway.blocked_by(
                repo.path, issue.issue_number, max_issues=100
            )
            if children_truncated or blockers_truncated:
                raise RepoForgeError(
                    "Issue graph relationship evidence is truncated",
                    code=ErrorCode.PROPOSAL_BLOCKED,
                    details={"missing_coverage": ["relationships:truncated"]},
                    safe_next_action="Inspect the bounded live relationships before retrying publication planning.",
                )
            for child in children:
                child_ref = number_to_ref.get(child.issue_number) or self._marker_ref(child)
                if child_ref in desired:
                    parent_pairs.add((str(child_ref), source_ref))
            for blocker in blockers:
                blocker_ref = number_to_ref.get(blocker.issue_number) or self._marker_ref(blocker)
                if blocker_ref in desired:
                    blocker_pairs.add((source_ref, str(blocker_ref)))
        parents = tuple(sorted(parent_pairs))
        blocked_by = tuple(sorted(blocker_pairs))
        snapshot = self._snapshot_hash(recent, live_nodes, parents, blocked_by)
        return (
            candidates,
            PublicationLiveGraph(live_nodes, parents, blocked_by, snapshot),
            snapshot,
        )

    def _identity(
        self,
        repo_id: str,
        runtime: RuntimeContractIdentity,
        live_snapshot_sha256: str,
    ) -> IssueGraphIdentity:
        repo = self.ctx.repo(repo_id)
        base = self.ctx.git.resolve_snapshot_ref(repo.path, repo, repo.default_base)
        return IssueGraphIdentity(
            repo_id=repo_id,
            repository_fingerprint=repository_capability_digest(repo),
            base_commit_sha=base.commit_sha,
            live_snapshot_sha256=live_snapshot_sha256,
            active_generation=runtime.active_generation,
            tool_surface_hash=runtime.tool_surface_hash,
            input_contract_digest=runtime.input_contract_digest,
            output_contract_digest=runtime.output_contract_digest,
            template_version=2,
            schema_version=1,
        )

    def _coordinator(self, repo_id: str) -> IssueGraphPublicationCoordinator:
        return IssueGraphPublicationCoordinator(
            self.ctx,
            self.operations,
            self.proposals,
            self.publications,
            RepositoryIssueGraphStepExecutor(self.ctx, repo_id),
        )

    @staticmethod
    def _approval_payload(plan: IssueGraphPublicationPlan) -> dict[str, object]:
        return {
            "kind": "issue_graph_publication_v1",
            "repo_id": plan.identity.repo_id,
            "proposal_id": plan.proposal_id,
            "proposal_hash": plan.proposal_hash,
            "plan_id": plan.plan_id,
            "effect_plan_hash": plan.effect_plan_hash,
            "identity": plan.identity.payload(),
            "provider_identity": asdict(plan.provider_identity),
            "adopt_refs": list(plan.adopt_refs),
        }

    def _approval_for_plan(self, plan: IssueGraphPublicationPlan) -> ApprovalRequest:
        approvals, payloads = approval_stores(self.ctx)
        payload = self._approval_payload(plan)
        digest = payloads.digest(payload)
        page = approvals.list_records(max_records=200)
        existing = next(
            (
                record.value
                for record in page.records
                if record.value.action == "issue_graph_publication"
                and record.value.subject.repo_id == plan.identity.repo_id
                and record.value.binding.proposal_id == plan.plan_id
                and record.value.binding.payload_digest == digest
                and record.value.status in {ApprovalStatus.PENDING, ApprovalStatus.ACCEPTED}
            ),
            None,
        )
        if existing is not None:
            return existing
        if page.scan_truncated:
            raise ConfigError("Issue graph approval reconciliation is incomplete")
        request_id = f"apr-{self.ctx.ids.new_hex(24)}"
        request = ApprovalRequest(
            request_id=request_id,
            action="issue_graph_publication",
            subject=ApprovalSubject(
                "issue_graph_publication",
                plan.identity.repo_id,
                "Approve exact issue graph publication plan",
                "external_write",
            ),
            binding=ApprovalBinding(
                plan.plan_id,
                digest,
                plan.identity.active_generation,
                plan.identity.repository_fingerprint,
            ),
            reason="Issue graph publication may create, update, adopt, and link GitHub issues.",
            created_at=self.ctx.clock.now_iso(),
            expires_at=plan.expires_at,
        )
        payloads.save(request_id, payload)
        try:
            approvals.create(request)
        except Exception:
            payloads.delete(request_id)
            raise
        return request

    def _require_approval(
        self,
        plan: IssueGraphPublicationPlan,
        approval_request_id: str,
    ) -> ApprovalRequest:
        approvals, payloads = approval_stores(self.ctx)
        envelope = approvals.read(approval_request_id)
        if envelope is None:
            raise ConfigError("Unknown issue graph approval_request_id")
        request = envelope.value
        payload = self._approval_payload(plan)
        digest = payloads.digest(payload)
        if (
            request.action != "issue_graph_publication"
            or request.subject.kind != "issue_graph_publication"
            or request.subject.repo_id != plan.identity.repo_id
            or request.binding.proposal_id != plan.plan_id
            or request.binding.payload_digest != digest
            or request.binding.expected_generation != plan.identity.active_generation
            or request.binding.expected_source_sha256 != plan.identity.repository_fingerprint
            or payloads.read(request.request_id) != payload
        ):
            raise RepoForgeError(
                "Approval request does not match the exact issue graph publication plan",
                code=ErrorCode.APPROVAL_MISMATCH,
            )
        return request

    def accept_exact_approval(
        self,
        approval_request_id: str,
        *,
        actor: str,
        reason: str,
    ) -> ApprovalRequest:
        approvals, payloads = approval_stores(self.ctx)
        envelope = approvals.read(approval_request_id)
        if envelope is None:
            raise ConfigError("Unknown issue graph approval_request_id")
        request = envelope.value
        payload = payloads.read(approval_request_id)
        if (
            request.action != "issue_graph_publication"
            or request.subject.kind != "issue_graph_publication"
            or payload is None
            or payloads.digest(payload) != request.binding.payload_digest
        ):
            raise RepoForgeError(
                "Approval request does not match an exact issue graph publication payload",
                code=ErrorCode.APPROVAL_MISMATCH,
            )
        if request.status is ApprovalStatus.ACCEPTED:
            return request
        if request.status is not ApprovalStatus.PENDING:
            raise ConfigError(f"Issue graph publication approval is {request.status.value}")
        accepted = decide_approval(
            request,
            ApprovalStatus.ACCEPTED,
            actor=actor,
            decided_at=self.ctx.clock.now_iso(),
            reason=reason,
        )
        return approvals.save(accepted, expected_revision=envelope.revision).value

    def _publication_result(
        self,
        action: str,
        publication: IssueGraphPublication,
        *,
        approval: ApprovalRequest | None = None,
    ) -> IssueGraphWorkflowResult:
        operation = self.operations.status(publication.operation_id)
        receipt_store = self.ctx.effect_receipts
        receipt = receipt_store.read(publication.receipt_id) if receipt_store is not None else None
        complete = bool(
            publication.state is PublicationState.SUCCEEDED
            and operation.state is OperationState.SUCCEEDED
            and receipt is not None
            and receipt.value.state is EffectReceiptState.APPLIED_VALIDATED
            and publication.result_reference
        )
        if complete:
            state = "succeeded"
            recovery = None
        elif publication.state is PublicationState.PAUSED:
            state = "paused"
            recovery = "Wait until retry_at, then call repo_issue manage reconcile."
        elif publication.state is PublicationState.MANUAL_RECOVERY_REQUIRED:
            state = "manual_recovery_required"
            recovery = (
                "Inspect step receipts and authoritative GitHub relationships before retrying."
            )
        elif operation.state in {
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.EXPIRED,
            OperationState.ORPHANED,
        }:
            state = "partial_failed"
            recovery = (
                "Inspect the durable operation and receipt, then call reconcile only when safe."
            )
        else:
            state = "publishing"
            recovery = (
                "Use operation get/wait or repo_issue manage status; do not blind-retry writes."
            )
        return IssueGraphWorkflowResult(
            action=action,
            state=state,
            proposal_id=publication.proposal_id,
            proposal_hash=publication.proposal_hash,
            plan_id=publication.plan_id,
            effect_plan_hash=publication.effect_plan_hash,
            approval_request_id=approval.request_id if approval is not None else None,
            approval_status=approval.status.value if approval is not None else None,
            publication_id=publication.publication_id,
            publication_state=publication.state.value,
            operation_id=publication.operation_id,
            receipt_id=publication.receipt_id,
            result_reference=publication.result_reference,
            retry_at=publication.retry_at,
            complete=complete,
            external_writes=publication.external_writes,
            recovery_action=recovery,
        )

    def _plan(
        self,
        repo_id: str,
        manage: dict[str, object],
        runtime: RuntimeContractIdentity,
    ) -> IssueGraphWorkflowResult:
        draft = self._draft(repo_id, manage)
        adopt_refs_raw = manage.get("adopt_refs", ())
        if not isinstance(adopt_refs_raw, (list, tuple)):
            raise ValueError("Issue graph manage plan adopt_refs is invalid")
        adopt_refs = tuple(sorted({str(item) for item in adopt_refs_raw}))
        candidates, live_graph, snapshot = self._live_truth(repo_id, draft)
        identity = self._identity(repo_id, runtime, snapshot)
        created_at = self.ctx.clock.now_iso()
        expires_at = (
            datetime.fromisoformat(created_at)
            + timedelta(
                seconds=self._int_value(
                    manage.get("expires_in_seconds", 3_600), "expires_in_seconds"
                )
            )
        ).isoformat()
        proposal = self.proposal_service.preview(
            draft,
            identity,
            live_issues=candidates,
            created_at=created_at,
            expires_at=expires_at,
        )
        self.proposal_service.create(proposal)
        plan = self._coordinator(repo_id).prepare(
            proposal.proposal_id,
            identity,
            live_graph=live_graph,
            adopt_refs=adopt_refs,
            created_at=created_at,
            expires_at=expires_at,
        )
        approval = self._approval_for_plan(plan)
        return IssueGraphWorkflowResult(
            action="plan",
            state=("planned" if approval.status is ApprovalStatus.ACCEPTED else "pending_approval"),
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            plan_id=plan.plan_id,
            effect_plan_hash=plan.effect_plan_hash,
            approval_request_id=approval.request_id,
            approval_status=approval.status.value,
            complete=False,
            external_writes=0,
            recovery_action=(
                "Apply the exact accepted plan."
                if approval.status is ApprovalStatus.ACCEPTED
                else (
                    f"Run `rf approval approve {approval.request_id}` after review, then call "
                    "repo_issue manage apply with the exact returned hashes."
                )
            ),
        )

    def _apply(
        self,
        repo_id: str,
        manage: dict[str, object],
        runtime: RuntimeContractIdentity,
    ) -> IssueGraphWorkflowResult:
        proposal_id = str(manage["proposal_id"])
        plan_id = str(manage["plan_id"])
        proposal = self.proposal_service.read(proposal_id)
        plan_record = self.publications.read_plan(plan_id)
        if plan_record is None:
            raise RepoForgeError(
                "Issue graph publication plan was not found", code=ErrorCode.NOT_FOUND
            )
        plan = plan_record.value
        if (
            proposal.proposal_hash != str(manage["proposal_hash"])
            or plan.proposal_id != proposal_id
            or plan.proposal_hash != proposal.proposal_hash
            or plan.effect_plan_hash != str(manage["effect_plan_hash"])
        ):
            raise RepoForgeError(
                "Issue graph apply hashes do not match immutable planning records",
                code=ErrorCode.APPROVAL_MISMATCH,
            )
        approval = self._require_approval(plan, str(manage["approval_request_id"]))
        if approval.status is ApprovalStatus.PENDING:
            return IssueGraphWorkflowResult(
                action="apply",
                state="pending_approval",
                proposal_id=proposal.proposal_id,
                proposal_hash=proposal.proposal_hash,
                plan_id=plan.plan_id,
                effect_plan_hash=plan.effect_plan_hash,
                approval_request_id=approval.request_id,
                approval_status=approval.status.value,
                complete=False,
                external_writes=0,
                recovery_action=(
                    f"Unsupported Elicitation is not approval. Run `rf approval approve "
                    f"{approval.request_id}` after review, then retry the exact apply request."
                ),
            )
        if approval.status is not ApprovalStatus.ACCEPTED:
            raise ConfigError(f"Issue graph publication approval is {approval.status.value}")
        _, _, snapshot = self._live_truth(repo_id, proposal.draft)
        identity = self._identity(repo_id, runtime, snapshot)
        coordinator = self._coordinator(repo_id)
        publication = coordinator.accept(
            plan.plan_id,
            approved_proposal_hash=proposal.proposal_hash,
            approved_effect_plan_hash=plan.effect_plan_hash,
            actual_identity=identity,
        )
        resume_identity = publication.identity

        def resume_in_background() -> None:
            coordinator.resume(publication.publication_id, resume_identity)

        self.background_tasks.submit(
            f"issue-graph-{publication.publication_id}",
            resume_in_background,
        )
        return self._publication_result("apply", publication, approval=approval)

    def _status(self, repo_id: str, publication_id: str) -> IssueGraphWorkflowResult:
        record = self.publications.read_publication(publication_id)
        if record is None:
            raise RepoForgeError("Issue graph publication was not found", code=ErrorCode.NOT_FOUND)
        publication = record.value
        if publication.identity.repo_id != repo_id:
            raise ConfigError(
                f"Issue graph publication {publication_id} belongs to repository "
                f"{publication.identity.repo_id!r}, not {repo_id!r}"
            )
        return self._publication_result("status", publication)

    def _reconcile(
        self,
        repo_id: str,
        publication_id: str,
        runtime: RuntimeContractIdentity,
    ) -> IssueGraphWorkflowResult:
        record = self.publications.read_publication(publication_id)
        if record is None:
            raise RepoForgeError("Issue graph publication was not found", code=ErrorCode.NOT_FOUND)
        publication = record.value
        current = self._identity(
            repo_id,
            runtime,
            publication.identity.live_snapshot_sha256,
        )
        resumed = self._coordinator(repo_id).resume(publication_id, current)
        return self._publication_result("reconcile", resumed)

    def _find_approval(
        self, plan: IssueGraphPublicationPlan
    ) -> tuple[ApprovalRequest | None, bool]:
        approvals, _ = approval_stores(self.ctx)
        records = approvals.list_records(max_records=200)
        approval = next(
            (
                record.value
                for record in records.records
                if record.value.action == "issue_graph_publication"
                and record.value.subject.repo_id == plan.identity.repo_id
                and record.value.binding.proposal_id == plan.plan_id
            ),
            None,
        )
        return approval, not records.scan_truncated

    @staticmethod
    def _bounded_scan_incomplete(kind: str) -> dict[str, object]:
        return {
            "action": "status",
            "state": "evidence_incomplete",
            "complete": False,
            "external_writes": 0,
            "recovery_action": (
                f"The bounded {kind} scan was truncated. Resume with exact proposal, plan, "
                "approval, publication, or operation IDs instead of guessing the latest record."
            ),
            "_evidence_complete": False,
            "_evidence_truncated": True,
        }

    def latest_facts(self, repo_id: str) -> dict[str, object]:
        publication_page = self.publications.list_publications(max_records=200)
        if publication_page.scan_truncated:
            return self._bounded_scan_incomplete("issue graph publication")
        publications = tuple(
            record.value
            for record in publication_page.records
            if record.value.identity.repo_id == repo_id
        )
        if publications:
            publication = max(publications, key=lambda item: (item.updated_at, item.publication_id))
            plan_record = self.publications.read_plan(publication.plan_id)
            approval, approval_complete = (
                self._find_approval(plan_record.value) if plan_record is not None else (None, True)
            )
            facts = asdict(self._publication_result("status", publication, approval=approval))
            facts["_evidence_complete"] = approval_complete
            facts["_evidence_truncated"] = not approval_complete
            return facts

        plan_page = self.publications.list_plans(max_records=200)
        if plan_page.scan_truncated:
            return self._bounded_scan_incomplete("issue graph publication plan")
        plans = tuple(
            record.value for record in plan_page.records if record.value.identity.repo_id == repo_id
        )
        if plans:
            plan = max(plans, key=lambda item: (item.created_at, item.plan_id))
            approval, approval_complete = self._find_approval(plan)
            facts = asdict(
                IssueGraphWorkflowResult(
                    action="plan",
                    state=(
                        "planned"
                        if approval is not None and approval.status is ApprovalStatus.ACCEPTED
                        else "pending_approval"
                    ),
                    proposal_id=plan.proposal_id,
                    proposal_hash=plan.proposal_hash,
                    plan_id=plan.plan_id,
                    effect_plan_hash=plan.effect_plan_hash,
                    approval_request_id=(approval.request_id if approval is not None else None),
                    approval_status=(approval.status.value if approval is not None else None),
                    complete=False,
                    external_writes=0,
                    recovery_action=(
                        "Apply the exact accepted plan."
                        if approval is not None and approval.status is ApprovalStatus.ACCEPTED
                        else (
                            f"Run `rf approval approve {approval.request_id}` after review, then "
                            "retry the exact apply request."
                            if approval is not None
                            else "Recreate the exact approval request from this immutable plan."
                        )
                    ),
                )
            )
            facts["_evidence_complete"] = approval_complete
            facts["_evidence_truncated"] = not approval_complete
            return facts

        proposal_page = self.proposals.list_records(max_records=200)
        if proposal_page.scan_truncated:
            return self._bounded_scan_incomplete("issue graph proposal")
        proposals = tuple(
            record.value
            for record in proposal_page.records
            if record.value.identity.repo_id == repo_id
        )
        if proposals:
            proposal = max(proposals, key=lambda item: (item.created_at, item.proposal_id))
            facts = asdict(
                IssueGraphWorkflowResult(
                    action="plan",
                    state="planned",
                    proposal_id=proposal.proposal_id,
                    proposal_hash=proposal.proposal_hash,
                    complete=False,
                    external_writes=0,
                    recovery_action="Create an immutable publication plan from the proposal.",
                )
            )
            facts["_evidence_complete"] = True
            facts["_evidence_truncated"] = False
            return facts
        return {
            "action": "plan",
            "state": "not_started",
            "complete": False,
            "external_writes": 0,
            "recovery_action": "Call repo_issue mode=manage action=plan with a typed desired graph.",
            "_evidence_complete": True,
            "_evidence_truncated": False,
        }

    def execute(
        self,
        repo_id: str,
        manage: dict[str, object],
        runtime_identity: dict[str, object] | None,
    ) -> IssueGraphWorkflowResult:
        runtime = self._runtime_identity(runtime_identity)
        action = str(manage.get("action", ""))
        if action == "plan":
            return self._plan(repo_id, manage, runtime)
        if action == "apply":
            return self._apply(repo_id, manage, runtime)
        if action == "status":
            return self._status(repo_id, str(manage["publication_id"]))
        if action == "reconcile":
            return self._reconcile(repo_id, str(manage["publication_id"]), runtime)
        raise ValueError("Unsupported issue graph manage action")


__all__ = ["IssueGraphWorkflowResult", "IssueGraphWorkflowService"]
