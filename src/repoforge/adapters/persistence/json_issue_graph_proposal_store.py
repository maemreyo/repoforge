"""Private atomic JSON store for immutable desired issue-graph proposals."""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.issue_graph_proposal import (
    ISSUE_GRAPH_PROPOSAL_SCHEMA_VERSION,
    IssueGraphProposal,
    proposal_from_payload,
    proposal_payload,
)
from ...ports.issue_graph_proposal_store import IssueGraphProposalStore
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_PROPOSAL_ID = re.compile(r"^igp-[a-f0-9]{24}$")


def _proposal_id(value: str) -> str:
    if _PROPOSAL_ID.fullmatch(value) is None:
        raise ValueError("invalid issue graph proposal id")
    return value


class _ProposalCodec(StateCodec[IssueGraphProposal]):
    schema_version = SchemaVersion(ISSUE_GRAPH_PROPOSAL_SCHEMA_VERSION)

    def encode(self, value: IssueGraphProposal) -> dict[str, object]:
        return proposal_payload(value)

    def decode(self, payload: dict[str, object]) -> IssueGraphProposal:
        return proposal_from_payload(dict(payload))


class JsonIssueGraphProposalStore(IssueGraphProposalStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records = JsonStateRepository(
            state_root,
            collection="issue-graph-proposals",
            locks=locks,
            codec=_ProposalCodec(),
            id_validator=_proposal_id,
            max_record_bytes=2_000_000,
        )
        self.root = self._records.root

    def create(self, proposal: IssueGraphProposal) -> StateEnvelope[IssueGraphProposal]:
        existing = self._records.read(proposal.proposal_id)
        if existing is not None:
            if existing.value == proposal:
                return existing
            raise RepoForgeError(
                "Issue graph proposal id is already bound to different content",
                code=ErrorCode.ALREADY_EXISTS,
            )
        return self._records.create(proposal.proposal_id, proposal)

    def read(self, proposal_id: str) -> StateEnvelope[IssueGraphProposal] | None:
        return self._records.read(proposal_id)

    def list_records(self, *, max_records: int = 200) -> StatePage[IssueGraphProposal]:
        return self._records.list_records(max_records=max_records)


__all__ = ["JsonIssueGraphProposalStore"]
