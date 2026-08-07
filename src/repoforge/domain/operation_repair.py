"""Exact-state operation repair proposals for durable execution recovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum

from .operation_task import validate_operation_id

_MAX_BLOCKER_CODE = 96
_MAX_BLOCKER_DETAIL = 512


class OperationRepairDisposition(str, Enum):
    ALREADY_TERMINAL = "already_terminal"
    CANCEL_QUEUED = "cancel_queued"
    REQUEUE_UNSTARTED = "requeue_unstarted"
    CANCEL_REAPED = "cancel_reaped"
    ORPHAN_REAPED = "orphan_reaped"
    BLOCKED_MISSING_BINDING = "blocked_missing_binding"
    BLOCKED_OWNER_MISMATCH = "blocked_owner_mismatch"
    BLOCKED_ATTEMPT_MISMATCH = "blocked_attempt_mismatch"
    BLOCKED_IDENTITY_UNPROVEN = "blocked_identity_unproven"
    BLOCKED_CHILD_SURVIVED = "blocked_child_survived"
    BLOCKED_STATE_CHANGED = "blocked_state_changed"
    NOT_REPAIRABLE = "not_repairable"


_REPAIRABLE_DISPOSITIONS = frozenset(
    {
        OperationRepairDisposition.ALREADY_TERMINAL,
        OperationRepairDisposition.CANCEL_QUEUED,
        OperationRepairDisposition.REQUEUE_UNSTARTED,
        OperationRepairDisposition.CANCEL_REAPED,
        OperationRepairDisposition.ORPHAN_REAPED,
    }
)


def _bounded_text(value: str, field: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} is invalid or exceeds {limit} characters")
    return value


@dataclass(frozen=True, slots=True, order=True)
class OperationRepairBlocker:
    code: str
    detail: str

    def __post_init__(self) -> None:
        _bounded_text(self.code, "repair blocker code", _MAX_BLOCKER_CODE)
        _bounded_text(self.detail, "repair blocker detail", _MAX_BLOCKER_DETAIL)


@dataclass(frozen=True, slots=True)
class OperationRepairSnapshot:
    operation_id: str
    operation_updated_at: str
    operation_state: str
    operation_owner_id: str | None
    operation_attempt: int
    operation_lease_expires_at: str | None
    cancellation_requested_at: str | None
    work_updated_at: str | None
    work_state: str | None
    work_owner_id: str | None
    work_attempt: int | None
    work_lease_expires_at: str | None
    child_started: bool | None
    binding_digest: str | None

    def __post_init__(self) -> None:
        validate_operation_id(self.operation_id)
        _bounded_text(self.operation_updated_at, "operation updated_at", 80)
        _bounded_text(self.operation_state, "operation state", 32)
        if self.operation_attempt < 0:
            raise ValueError("operation attempt must be non-negative")
        if self.work_attempt is not None and self.work_attempt < 0:
            raise ValueError("work attempt must be non-negative")
        if self.binding_digest is not None and (
            len(self.binding_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.binding_digest)
        ):
            raise ValueError("binding digest must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class OperationRepairProposal:
    snapshot: OperationRepairSnapshot
    disposition: OperationRepairDisposition
    blockers: tuple[OperationRepairBlocker, ...]
    repairable: bool
    proposal_token: str


def _proposal_token(
    snapshot: OperationRepairSnapshot,
    disposition: OperationRepairDisposition,
    blockers: tuple[OperationRepairBlocker, ...],
) -> str:
    payload = {
        "schema_version": 1,
        "snapshot": asdict(snapshot),
        "disposition": disposition.value,
        "blockers": [asdict(blocker) for blocker in blockers],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def operation_repair_proposal(
    snapshot: OperationRepairSnapshot,
    *,
    disposition: OperationRepairDisposition,
    blockers: tuple[OperationRepairBlocker, ...] = (),
) -> OperationRepairProposal:
    ordered = tuple(sorted(set(blockers)))
    repairable = disposition in _REPAIRABLE_DISPOSITIONS
    return OperationRepairProposal(
        snapshot=snapshot,
        disposition=disposition,
        blockers=ordered,
        repairable=repairable,
        proposal_token=_proposal_token(snapshot, disposition, ordered),
    )
