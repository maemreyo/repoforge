"""Private CAS JSON persistence for issue-graph publication sagas."""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateCodec, StateEnvelope
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.issue_graph_publication import (
    ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION,
    IssueGraphPublication,
    IssueGraphPublicationPlan,
    PublicationState,
    publication_from_payload,
    publication_payload,
    publication_plan_from_payload,
    publication_plan_payload,
)
from ...ports.issue_graph_publication_store import IssueGraphPublicationStore
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_PLAN_ID = re.compile(r"^igplan-[a-f0-9]{24}$")
_PUBLICATION_ID = re.compile(r"^igpub-[a-f0-9]{24}$")


def _plan_id(value: str) -> str:
    if _PLAN_ID.fullmatch(value) is None:
        raise ValueError("invalid issue graph publication plan id")
    return value


def _publication_id(value: str) -> str:
    if _PUBLICATION_ID.fullmatch(value) is None:
        raise ValueError("invalid issue graph publication id")
    return value


class _PlanCodec(StateCodec[IssueGraphPublicationPlan]):
    schema_version = SchemaVersion(ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION)

    def encode(self, value: IssueGraphPublicationPlan) -> dict[str, object]:
        return publication_plan_payload(value)

    def decode(self, payload: dict[str, object]) -> IssueGraphPublicationPlan:
        return publication_plan_from_payload(dict(payload))


class _PublicationCodec(StateCodec[IssueGraphPublication]):
    schema_version = SchemaVersion(ISSUE_GRAPH_PUBLICATION_SCHEMA_VERSION)

    def encode(self, value: IssueGraphPublication) -> dict[str, object]:
        return publication_payload(value)

    def decode(self, payload: dict[str, object]) -> IssueGraphPublication:
        return publication_from_payload(dict(payload))


class JsonIssueGraphPublicationStore(IssueGraphPublicationStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._plans = JsonStateRepository(
            state_root,
            collection="issue-graph-publication-plans",
            locks=locks,
            codec=_PlanCodec(),
            id_validator=_plan_id,
            max_record_bytes=4_000_000,
        )
        self._publications = JsonStateRepository(
            state_root,
            collection="issue-graph-publications",
            locks=locks,
            codec=_PublicationCodec(),
            id_validator=_publication_id,
            max_record_bytes=4_000_000,
        )
        self.root = self._publications.root

    @staticmethod
    def _immutable_collision(kind: str) -> RepoForgeError:
        return RepoForgeError(
            f"Issue graph {kind} identity is already bound to different content",
            code=ErrorCode.ALREADY_EXISTS,
        )

    def create_plan(
        self, plan: IssueGraphPublicationPlan
    ) -> StateEnvelope[IssueGraphPublicationPlan]:
        existing = self._plans.read(plan.plan_id)
        if existing is not None:
            if existing.value == plan:
                return existing
            raise self._immutable_collision("publication plan")
        return self._plans.create(plan.plan_id, plan)

    def read_plan(self, plan_id: str) -> StateEnvelope[IssueGraphPublicationPlan] | None:
        return self._plans.read(plan_id)

    def create_publication(
        self, publication: IssueGraphPublication
    ) -> StateEnvelope[IssueGraphPublication]:
        existing = self._publications.read(publication.publication_id)
        if existing is not None:
            if existing.value == publication:
                return existing
            raise self._immutable_collision("publication")
        return self._publications.create(publication.publication_id, publication)

    def read_publication(self, publication_id: str) -> StateEnvelope[IssueGraphPublication] | None:
        return self._publications.read(publication_id)

    def save_publication(
        self,
        publication: IssueGraphPublication,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[IssueGraphPublication]:
        current = self._publications.read(publication.publication_id)
        if current is not None and current.value.state in {
            PublicationState.SUCCEEDED,
            PublicationState.MANUAL_RECOVERY_REQUIRED,
        }:
            if current.value == publication:
                return current
            raise RepoForgeError(
                "Terminal issue graph publication records are immutable",
                code=ErrorCode.STATE_INVALID,
            )
        return self._publications.save(
            publication.publication_id,
            publication,
            expected_revision=expected_revision,
        )


__all__ = ["JsonIssueGraphPublicationStore"]
