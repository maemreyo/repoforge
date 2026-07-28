"""Persistence boundary for issue-graph publication plans and saga state."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.issue_graph_publication import IssueGraphPublication, IssueGraphPublicationPlan


class IssueGraphPublicationStore(Protocol):
    def create_plan(
        self, plan: IssueGraphPublicationPlan
    ) -> StateEnvelope[IssueGraphPublicationPlan]: ...

    def read_plan(self, plan_id: str) -> StateEnvelope[IssueGraphPublicationPlan] | None: ...

    def list_plans(self, *, max_records: int = 200) -> StatePage[IssueGraphPublicationPlan]: ...

    def create_publication(
        self, publication: IssueGraphPublication
    ) -> StateEnvelope[IssueGraphPublication]: ...

    def read_publication(
        self, publication_id: str
    ) -> StateEnvelope[IssueGraphPublication] | None: ...

    def list_publications(self, *, max_records: int = 200) -> StatePage[IssueGraphPublication]: ...

    def save_publication(
        self,
        publication: IssueGraphPublication,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[IssueGraphPublication]: ...
