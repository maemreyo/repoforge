"""Typed, exact-state-bound human approval request contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

APPROVAL_SCHEMA_VERSION = 1
_APPROVAL_ID = re.compile(r"^(?:apr-[a-f0-9]{24}|chg-[a-f0-9]{20})$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _text(name: str, value: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise ValueError(f"{name} must contain between 1 and {limit} characters")
    if any(ord(character) < 32 and character not in "\t\n" for character in normalized):
        raise ValueError(f"{name} contains control characters")
    return normalized


def _identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


_TERMINAL = frozenset(status for status in ApprovalStatus if status is not ApprovalStatus.PENDING)


@dataclass(frozen=True, slots=True)
class ApprovalSubject:
    kind: str
    repo_id: str | None
    summary: str
    capability_delta: str | None = None

    def __post_init__(self) -> None:
        _identifier("approval subject kind", self.kind)
        if self.repo_id is not None:
            _identifier("approval subject repo_id", self.repo_id)
        _text("approval subject summary", self.summary, limit=1_000)
        if self.capability_delta is not None:
            _identifier("approval subject capability_delta", self.capability_delta)


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    proposal_id: str
    payload_digest: str
    expected_generation: int | None = None
    expected_source_sha256: str | None = None

    def __post_init__(self) -> None:
        _identifier("approval proposal_id", self.proposal_id)
        if _SHA256.fullmatch(self.payload_digest) is None:
            raise ValueError("approval payload_digest must be a SHA-256 identity")
        if self.expected_generation is not None and (
            not isinstance(self.expected_generation, int)
            or isinstance(self.expected_generation, bool)
            or self.expected_generation <= 0
        ):
            raise ValueError("approval expected_generation must be a positive integer")
        if (
            self.expected_source_sha256 is not None
            and _SHA256.fullmatch(self.expected_source_sha256) is None
        ):
            raise ValueError("approval expected_source_sha256 must be a SHA-256 identity")


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    status: ApprovalStatus
    actor: str
    decided_at: str
    reason: str

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL:
            raise ValueError("approval decisions must use a terminal status")
        _text("approval actor", self.actor, limit=256)
        _text("approval decided_at", self.decided_at, limit=64)
        _text("approval decision reason", self.reason, limit=1_000)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    action: str
    subject: ApprovalSubject
    binding: ApprovalBinding
    reason: str
    created_at: str
    expires_at: str | None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision | None = None

    def __post_init__(self) -> None:
        validate_approval_id(self.request_id)
        _identifier("approval action", self.action)
        _text("approval reason", self.reason, limit=2_000)
        _text("approval created_at", self.created_at, limit=64)
        if self.expires_at is not None:
            _text("approval expires_at", self.expires_at, limit=64)
        if self.status is ApprovalStatus.PENDING and self.decision is not None:
            raise ValueError("pending approval cannot contain a decision")
        if self.status is not ApprovalStatus.PENDING and (
            self.decision is None or self.decision.status is not self.status
        ):
            raise ValueError("terminal approval status requires a matching decision")

    def summary(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "action": self.action,
            "repo_id": self.subject.repo_id,
            "summary": self.subject.summary,
            "capability_delta": self.subject.capability_delta,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "expected_generation": self.binding.expected_generation,
            "proposal_id": self.binding.proposal_id,
            "decision": (
                {
                    "status": self.decision.status.value,
                    "actor": self.decision.actor,
                    "decided_at": self.decision.decided_at,
                    "reason": self.decision.reason,
                }
                if self.decision is not None
                else None
            ),
        }


def validate_approval_id(value: str) -> str:
    if not isinstance(value, str) or _APPROVAL_ID.fullmatch(value) is None:
        raise ValueError("approval id must use apr-<24 hex> or chg-<20 hex>")
    return value


def decide_approval(
    request: ApprovalRequest,
    status: ApprovalStatus,
    *,
    actor: str,
    decided_at: str,
    reason: str,
) -> ApprovalRequest:
    if status not in _TERMINAL:
        raise ValueError("approval decision must be terminal")
    decision = ApprovalDecision(status, actor, decided_at, reason)
    if request.status is ApprovalStatus.PENDING:
        return replace(request, status=status, decision=decision)
    if request.status is status and request.decision == decision:
        return request
    raise ValueError(f"approval request is already terminal: {request.status.value}")
