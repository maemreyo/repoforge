"""Read-only planning and private persistence for desired issue graphs."""

from __future__ import annotations

from ...domain.durable_state import StateEnvelope
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.issue_graph_proposal import (
    IssueGraphDraft,
    IssueGraphIdentity,
    IssueGraphProposal,
    LiveIssueCandidate,
    plan_issue_graph,
    proposal_stale_fields,
)
from ...ports.issue_graph_proposal_store import IssueGraphProposalStore


class IssueGraphProposalService:
    """Application boundary with no GitHub, workspace, config, or command mutation ports."""

    def __init__(self, store: IssueGraphProposalStore) -> None:
        self.store = store

    def preview(
        self,
        draft: IssueGraphDraft,
        identity: IssueGraphIdentity,
        *,
        live_issues: tuple[LiveIssueCandidate, ...],
        created_at: str,
        expires_at: str,
    ) -> IssueGraphProposal:
        return plan_issue_graph(
            draft,
            identity,
            live_issues=live_issues,
            created_at=created_at,
            expires_at=expires_at,
        )

    def create(
        self,
        proposal: IssueGraphProposal,
    ) -> StateEnvelope[IssueGraphProposal]:
        return self.store.create(proposal)

    def read(self, proposal_id: str) -> IssueGraphProposal:
        record = self.store.read(proposal_id)
        if record is None:
            raise RepoForgeError(
                "Issue graph proposal was not found",
                code=ErrorCode.NOT_FOUND,
                safe_next_action="Create a new proposal from current graph and identity evidence.",
            )
        return record.value

    def inspect(
        self,
        proposal_id: str,
        actual_identity: IssueGraphIdentity,
    ) -> tuple[str, ...]:
        return proposal_stale_fields(self.read(proposal_id), actual_identity)


__all__ = ["IssueGraphProposalService"]
