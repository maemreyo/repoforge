"""Approval queue orchestration and legacy pending-policy migration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...domain.approval import (
    ApprovalBinding,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalSubject,
    decide_approval,
)
from ...domain.errors import ConfigError, RepoForgeError
from ...ports.approval_store import ApprovalPayloadStore, ApprovalStore

_MAX_PENDING_CHANGES = 20


class PendingPolicyChangeStore:
    """Compatibility facade over the shared approval request and private payload stores."""

    def __init__(
        self,
        *,
        approvals: ApprovalStore,
        payloads: ApprovalPayloadStore,
        legacy_root: Path,
    ) -> None:
        self.approvals = approvals
        self.payloads = payloads
        self._legacy_root = legacy_root
        self._migrate_legacy()

    @property
    def root(self) -> Path:
        return self._legacy_root

    def _legacy_path(self, change_id: str) -> Path:
        try:
            from ...domain.approval import validate_approval_id

            validate_approval_id(change_id)
        except ValueError as exc:
            raise ConfigError(f"Invalid pending change id: {change_id!r}") from exc
        return self._legacy_root / f"{change_id}.json"

    @staticmethod
    def _request(record: dict[str, Any], payload_digest: str) -> ApprovalRequest:
        required = {
            "change_id",
            "repo_id",
            "reason",
            "created_at",
            "capability_delta",
            "expected_generation",
            "expected_source_sha256",
            "proposal_id",
        }
        missing = sorted(required - set(record))
        if missing:
            raise ConfigError(f"Pending policy change is missing fields: {missing}")
        return ApprovalRequest(
            request_id=str(record["change_id"]),
            action="repository_policy_change",
            subject=ApprovalSubject(
                "repository_policy",
                str(record["repo_id"]),
                f"Review repository policy change for {record['repo_id']}",
                str(record["capability_delta"]),
            ),
            binding=ApprovalBinding(
                str(record["proposal_id"]),
                payload_digest,
                int(record["expected_generation"]),
                str(record["expected_source_sha256"]),
            ),
            reason=str(record["reason"]),
            created_at=str(record["created_at"]),
            expires_at=None,
        )

    def save(self, record: dict[str, Any]) -> None:
        pending_count = sum(
            1
            for envelope in self.approvals.list_records(max_records=2_000).records
            if envelope.value.status is ApprovalStatus.PENDING
        )
        request_id = str(record.get("change_id", ""))
        existing = self.approvals.read(request_id) if request_id else None
        if existing is None and pending_count >= _MAX_PENDING_CHANGES:
            raise ConfigError(
                "PENDING_CHANGES_FULL: too many unapproved policy changes; approve or "
                "reject existing ones with `rf config pending` first"
            )
        payload = {str(key): value for key, value in record.items()}
        try:
            digest = self.payloads.save(request_id, payload)
            request = self._request(record, digest)
            if existing is None:
                self.approvals.create(request)
            elif (
                existing.value.status is not ApprovalStatus.PENDING
                or existing.value.binding.payload_digest != digest
            ):
                raise ConfigError(
                    f"Approval request already exists with different state: {request_id}"
                )
        except (RepoForgeError, ValueError, TypeError) as exc:
            if existing is None:
                self.payloads.delete(request_id)
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"Cannot persist pending policy change {request_id}: {exc}") from exc

    def load(self, change_id: str) -> dict[str, Any]:
        request = self.approvals.read(change_id)
        if request is None or request.value.status is not ApprovalStatus.PENDING:
            raise ConfigError(f"Unknown pending policy change: {change_id}")
        payload = self.payloads.read(change_id)
        if payload is None:
            raise ConfigError(f"Pending policy change payload is missing: {change_id}")
        digest = self.payloads.digest(payload)
        if digest != request.value.binding.payload_digest:
            raise ConfigError(f"Pending policy change payload is stale or corrupt: {change_id}")
        return payload

    def entries(self) -> list[dict[str, Any]]:
        return [self.load(str(item["change_id"])) for item in self.summaries()]

    def summaries(self) -> list[dict[str, Any]]:
        pending = [
            envelope.value
            for envelope in self.approvals.list_records(max_records=2_000).records
            if envelope.value.status is ApprovalStatus.PENDING
        ]
        pending.sort(key=lambda item: (item.created_at, item.request_id))
        summaries: list[dict[str, Any]] = []
        for request in pending:
            payload = self.payloads.read(request.request_id)
            if payload is None:
                raise ConfigError(f"Pending policy change payload is missing: {request.request_id}")
            summaries.append(
                {
                    "change_id": request.request_id,
                    "repo_id": request.subject.repo_id,
                    "reason": request.reason,
                    "created_at": request.created_at,
                    "capability_delta": request.subject.capability_delta,
                    "changes": payload.get("changes", []),
                    "expected_generation": request.binding.expected_generation,
                }
            )
        return summaries

    def _decide(
        self,
        change_id: str,
        status: ApprovalStatus,
        *,
        actor: str,
        decided_at: str,
        reason: str,
    ) -> None:
        envelope = self.approvals.read(change_id)
        if envelope is None:
            raise ConfigError(f"Unknown pending policy change: {change_id}")
        try:
            decided = decide_approval(
                envelope.value,
                status,
                actor=actor,
                decided_at=decided_at,
                reason=reason,
            )
            self.approvals.save(decided, expected_revision=envelope.revision)
        except (RepoForgeError, ValueError) as exc:
            raise ConfigError(f"Cannot decide pending policy change {change_id}: {exc}") from exc
        self.payloads.delete(change_id)

    def approve(self, change_id: str, *, actor: str, decided_at: str) -> None:
        self._decide(
            change_id,
            ApprovalStatus.ACCEPTED,
            actor=actor,
            decided_at=decided_at,
            reason="Operator approved the exact bound configuration proposal.",
        )

    def reject(self, change_id: str, *, actor: str, decided_at: str) -> None:
        self._decide(
            change_id,
            ApprovalStatus.DECLINED,
            actor=actor,
            decided_at=decided_at,
            reason="Operator declined the proposed capability change.",
        )

    def invalidate(self, change_id: str, *, actor: str, decided_at: str) -> None:
        self._decide(
            change_id,
            ApprovalStatus.INVALIDATED,
            actor=actor,
            decided_at=decided_at,
            reason="The exact proposal binding became stale or was administratively invalidated.",
        )

    def delete(self, change_id: str) -> None:
        """Compatibility alias for old callers; new callers should retain explicit actor/time."""
        self.invalidate(
            change_id,
            actor="repoforge",
            decided_at="1970-01-01T00:00:00+00:00",
        )

    def _migrate_legacy(self) -> None:
        if not self._legacy_root.is_dir():
            return
        for path in sorted(self._legacy_root.glob("*.json"))[:_MAX_PENDING_CHANGES]:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                record = {str(key): value for key, value in raw.items()}
                self.save(record)
            except (OSError, json.JSONDecodeError, ConfigError):
                continue
            path.unlink(missing_ok=True)
