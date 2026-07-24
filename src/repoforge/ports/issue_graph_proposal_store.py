"""Persistence boundary for immutable desired issue-graph proposals."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import StateEnvelope, StatePage
from ..domain.issue_graph_proposal import IssueGraphProposal


class IssueGraphProposalStore(Protocol):
    def create(self, proposal: IssueGraphProposal) -> StateEnvelope[IssueGraphProposal]: ...

    def read(self, proposal_id: str) -> StateEnvelope[IssueGraphProposal] | None: ...

    def list_records(self, *, max_records: int = 200) -> StatePage[IssueGraphProposal]: ...
