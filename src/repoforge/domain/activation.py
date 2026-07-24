"""Pure release identity and activation receipt contracts for versioned runtime activation.

These are the immutable facts that make it unambiguous which commit the live runtime
serves, and the receipts that make every activation reversible. Nothing here touches
the filesystem, a process, or a clock -- adapters supply those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID = re.compile(r"^act-[0-9]{8}-[0-9]{3,}$")


def _clean(value: str, *, name: str, limit: int = 1024) -> str:
    if not value or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError(f"Release {name} is invalid")
    return value


class ActivationOutcome(str, Enum):
    """Terminal disposition of one activation attempt."""

    STAGED = "staged"
    ACTIVATED = "activated"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class ActivationStage(str, Enum):
    """How far an activation attempt got before it terminalized.

    An activation may only terminalize as ``ACTIVATED`` from ``HEALTH_VERIFIED``:
    switching the symlink is not activation, and a restart that cannot be proven to
    be serving the candidate is a failure, never a soft success.
    """

    PREPARED = "prepared"
    SYMLINK_SWITCHED = "symlink_switched"
    RUNTIME_RESTARTED = "runtime_restarted"
    HEALTH_VERIFIED = "health_verified"


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """The immutable identity of one installed release directory.

    ``build_fingerprint`` is the SHA-256 of the wheel the release was materialized
    from; ``tool_surface_hash`` is the connector tool-surface hash the release serves.
    Together they answer "what exactly is installed at ``releases/<commit_sha>``".
    """

    commit_sha: str
    package_version: str
    build_fingerprint: str
    tool_surface_hash: str
    source_worktree: str
    built_at: str

    def __post_init__(self) -> None:
        if not _COMMIT_SHA.fullmatch(self.commit_sha):
            raise ValueError("Release commit_sha must be lowercase hex (7-40 chars)")
        if not _SHA256.fullmatch(self.build_fingerprint):
            raise ValueError("Release build_fingerprint must be a sha256 digest")
        if not _SHA256.fullmatch(self.tool_surface_hash):
            raise ValueError("Release tool_surface_hash must be a sha256 digest")
        _clean(self.package_version, name="package_version", limit=128)
        _clean(self.source_worktree, name="source_worktree")
        _clean(self.built_at, name="built_at", limit=64)

    def to_dict(self) -> dict[str, str]:
        return {
            "commit_sha": self.commit_sha,
            "package_version": self.package_version,
            "build_fingerprint": self.build_fingerprint,
            "tool_surface_hash": self.tool_surface_hash,
            "source_worktree": self.source_worktree,
            "built_at": self.built_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ReleaseManifest:
        if not isinstance(raw, dict):
            raise ValueError("Release manifest must be a JSON object")
        try:
            return cls(
                commit_sha=_require_str(raw, "commit_sha"),
                package_version=_require_str(raw, "package_version"),
                build_fingerprint=_require_str(raw, "build_fingerprint"),
                tool_surface_hash=_require_str(raw, "tool_surface_hash"),
                source_worktree=_require_str(raw, "source_worktree"),
                built_at=_require_str(raw, "built_at"),
            )
        except KeyError as exc:
            raise ValueError(f"Release manifest missing field: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ActivationReceipt:
    """An append-only record of one `current` swap, sufficient to reproduce or reverse it."""

    receipt_id: str
    from_sha: str | None
    to_sha: str
    to_fingerprint: str
    tool_surface_hash: str
    rediscovery_required: bool
    outcome: ActivationOutcome
    activated_at: str
    from_fingerprint: str | None = None
    detail: str = ""
    stage: ActivationStage = ActivationStage.PREPARED
    observed_sha: str | None = None
    converged: bool = False
    cause_receipt_id: str | None = None

    def __post_init__(self) -> None:
        if not _RECEIPT_ID.fullmatch(self.receipt_id):
            raise ValueError("Activation receipt_id must look like act-YYYYMMDD-NNN")
        if not _COMMIT_SHA.fullmatch(self.to_sha):
            raise ValueError("Activation to_sha must be lowercase hex")
        if self.from_sha is not None and not _COMMIT_SHA.fullmatch(self.from_sha):
            raise ValueError("Activation from_sha must be lowercase hex or null")
        if not _SHA256.fullmatch(self.to_fingerprint):
            raise ValueError("Activation to_fingerprint must be a sha256 digest")
        if self.from_fingerprint is not None and not _SHA256.fullmatch(self.from_fingerprint):
            raise ValueError("Activation from_fingerprint must be a sha256 digest or null")
        if not _SHA256.fullmatch(self.tool_surface_hash):
            raise ValueError("Activation tool_surface_hash must be a sha256 digest")
        _clean(self.activated_at, name="activated_at", limit=64)
        if len(self.detail) > 2048:
            raise ValueError("Activation detail is too long")
        if self.observed_sha is not None and not _COMMIT_SHA.fullmatch(self.observed_sha):
            raise ValueError("Activation observed_sha must be lowercase hex or null")
        if self.cause_receipt_id is not None and not _RECEIPT_ID.fullmatch(self.cause_receipt_id):
            raise ValueError("Activation cause_receipt_id must look like act-YYYYMMDD-NNN")
        # An activation is only truthfully "activated" when the live runtime was
        # observed serving the candidate and health-verified.
        if self.outcome is ActivationOutcome.ACTIVATED and not (
            self.converged and self.stage is ActivationStage.HEALTH_VERIFIED
        ):
            raise ValueError(
                "Activation cannot be ACTIVATED without convergence and a verified health stage"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "from_sha": self.from_sha,
            "to_sha": self.to_sha,
            "from_fingerprint": self.from_fingerprint,
            "to_fingerprint": self.to_fingerprint,
            "tool_surface_hash": self.tool_surface_hash,
            "rediscovery_required": self.rediscovery_required,
            "outcome": self.outcome.value,
            "activated_at": self.activated_at,
            "detail": self.detail,
            "stage": self.stage.value,
            "observed_sha": self.observed_sha,
            "converged": self.converged,
            "cause_receipt_id": self.cause_receipt_id,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ActivationReceipt:
        if not isinstance(raw, dict):
            raise ValueError("Activation receipt must be a JSON object")
        outcome_raw = raw.get("outcome")
        if not isinstance(outcome_raw, str):
            raise ValueError("Activation receipt outcome must be a string")
        from_sha = raw.get("from_sha")
        from_fingerprint = raw.get("from_fingerprint")
        rediscovery = raw.get("rediscovery_required")
        observed_sha = raw.get("observed_sha")
        cause_receipt_id = raw.get("cause_receipt_id")
        stage_raw = raw.get("stage")
        stage = (
            ActivationStage(stage_raw) if isinstance(stage_raw, str) else ActivationStage.PREPARED
        )
        try:
            return cls(
                receipt_id=_require_str(raw, "receipt_id"),
                from_sha=from_sha if isinstance(from_sha, str) else None,
                to_sha=_require_str(raw, "to_sha"),
                from_fingerprint=from_fingerprint if isinstance(from_fingerprint, str) else None,
                to_fingerprint=_require_str(raw, "to_fingerprint"),
                tool_surface_hash=_require_str(raw, "tool_surface_hash"),
                rediscovery_required=bool(rediscovery),
                outcome=ActivationOutcome(outcome_raw),
                activated_at=_require_str(raw, "activated_at"),
                detail=raw.get("detail", "") if isinstance(raw.get("detail"), str) else "",
                stage=stage,
                observed_sha=observed_sha if isinstance(observed_sha, str) else None,
                converged=bool(raw.get("converged")),
                cause_receipt_id=cause_receipt_id if isinstance(cause_receipt_id, str) else None,
            )
        except KeyError as exc:
            raise ValueError(f"Activation receipt missing field: {exc}") from exc


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise ValueError(f"Field {key} must be a string")
    return value
