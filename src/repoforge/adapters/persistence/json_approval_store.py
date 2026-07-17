"""Private approval metadata and payload persistence adapters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...domain.approval import (
    APPROVAL_SCHEMA_VERSION,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalSubject,
    validate_approval_id,
)
from ...domain.durable_state import Revision, SchemaVersion, StateEnvelope, StatePage
from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.locking import LockManager
from .json_state_repository import AtomicJsonFileStore, JsonStateRepository


class ApprovalRequestCodec:
    schema_version = SchemaVersion(APPROVAL_SCHEMA_VERSION)

    def encode(self, value: ApprovalRequest) -> dict[str, object]:
        return {
            "action": value.action,
            "binding": {
                "proposal_id": value.binding.proposal_id,
                "payload_digest": value.binding.payload_digest,
                "expected_generation": value.binding.expected_generation,
                "expected_source_sha256": value.binding.expected_source_sha256,
            },
            "created_at": value.created_at,
            "decision": (
                {
                    "status": value.decision.status.value,
                    "actor": value.decision.actor,
                    "decided_at": value.decision.decided_at,
                    "reason": value.decision.reason,
                }
                if value.decision is not None
                else None
            ),
            "expires_at": value.expires_at,
            "reason": value.reason,
            "request_id": value.request_id,
            "status": value.status.value,
            "subject": {
                "kind": value.subject.kind,
                "repo_id": value.subject.repo_id,
                "summary": value.subject.summary,
                "capability_delta": value.subject.capability_delta,
            },
        }

    def decode(self, payload: dict[str, object]) -> ApprovalRequest:
        if set(payload) != {
            "action",
            "binding",
            "created_at",
            "decision",
            "expires_at",
            "reason",
            "request_id",
            "status",
            "subject",
        }:
            raise ValueError("approval request fields do not match schema version 1")
        subject_raw = payload["subject"]
        binding_raw = payload["binding"]
        if not isinstance(subject_raw, dict) or set(subject_raw) != {
            "kind",
            "repo_id",
            "summary",
            "capability_delta",
        }:
            raise ValueError("approval subject is invalid")
        if not isinstance(binding_raw, dict) or set(binding_raw) != {
            "proposal_id",
            "payload_digest",
            "expected_generation",
            "expected_source_sha256",
        }:
            raise ValueError("approval binding is invalid")
        generation = binding_raw["expected_generation"]
        if generation is not None and (
            not isinstance(generation, int) or isinstance(generation, bool)
        ):
            raise ValueError("approval expected_generation is invalid")
        status = ApprovalStatus(str(payload["status"]))
        decision_raw = payload["decision"]
        decision: ApprovalDecision | None
        if decision_raw is None:
            decision = None
        elif isinstance(decision_raw, dict) and set(decision_raw) == {
            "status",
            "actor",
            "decided_at",
            "reason",
        }:
            decision = ApprovalDecision(
                ApprovalStatus(str(decision_raw["status"])),
                str(decision_raw["actor"]),
                str(decision_raw["decided_at"]),
                str(decision_raw["reason"]),
            )
        else:
            raise ValueError("approval decision is invalid")
        return ApprovalRequest(
            request_id=str(payload["request_id"]),
            action=str(payload["action"]),
            subject=ApprovalSubject(
                str(subject_raw["kind"]),
                str(subject_raw["repo_id"]) if subject_raw["repo_id"] is not None else None,
                str(subject_raw["summary"]),
                str(subject_raw["capability_delta"])
                if subject_raw["capability_delta"] is not None
                else None,
            ),
            binding=ApprovalBinding(
                str(binding_raw["proposal_id"]),
                str(binding_raw["payload_digest"]),
                generation,
                str(binding_raw["expected_source_sha256"])
                if binding_raw["expected_source_sha256"] is not None
                else None,
            ),
            reason=str(payload["reason"]),
            created_at=str(payload["created_at"]),
            expires_at=str(payload["expires_at"]) if payload["expires_at"] is not None else None,
            status=status,
            decision=decision,
        )


class JsonApprovalStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._repository = JsonStateRepository[ApprovalRequest](
            state_root,
            collection="approvals",
            locks=locks,
            codec=ApprovalRequestCodec(),
            id_validator=validate_approval_id,
            max_record_bytes=256_000,
        )
        self.root = self._repository.root

    def create(self, request: ApprovalRequest) -> StateEnvelope[ApprovalRequest]:
        return self._repository.create(request.request_id, request)

    def read(self, request_id: str) -> StateEnvelope[ApprovalRequest] | None:
        return self._repository.read(request_id)

    def save(
        self, request: ApprovalRequest, *, expected_revision: Revision
    ) -> StateEnvelope[ApprovalRequest]:
        return self._repository.save(
            request.request_id, request, expected_revision=expected_revision
        )

    def list_records(self, *, max_records: int) -> StatePage[ApprovalRequest]:
        return self._repository.list_records(max_records=max_records)


class JsonApprovalPayloadStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._files = AtomicJsonFileStore(
            state_root,
            collection="approval-payloads",
            locks=locks,
            id_validator=validate_approval_id,
            max_record_bytes=4_000_000,
        )
        self.root = self._files.root

    @staticmethod
    def _encoded(payload: dict[str, object]) -> tuple[bytes, str]:
        encoded_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded_payload).hexdigest()
        envelope = (
            json.dumps(
                {"payload": payload, "payload_digest": digest, "schema_version": 1},
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        return envelope, digest

    def digest(self, payload: dict[str, object]) -> str:
        return self._encoded(payload)[1]

    def save(self, request_id: str, payload: dict[str, object]) -> str:
        encoded, digest = self._encoded(payload)
        with self._files.locked(request_id, operation="save"):
            existing = self._files.read_bytes(request_id)
            if existing is not None:
                current = self.read(request_id)
                if current is None:
                    raise RepoForgeError(
                        "Approval payload disappeared during save",
                        code=ErrorCode.STATE_CORRUPT,
                    )
                _, current_digest = self._encoded(current)
                if current_digest != digest:
                    raise RepoForgeError(
                        "Approval payload identity is already bound to different content",
                        code=ErrorCode.ALREADY_EXISTS,
                    )
                return digest
            self._files.write_bytes(request_id, encoded)
        return digest

    def read(self, request_id: str) -> dict[str, object] | None:
        data = self._files.read_bytes(request_id)
        if data is None:
            return None
        try:
            raw = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepoForgeError(
                "Approval payload is corrupt",
                code=ErrorCode.STATE_CORRUPT,
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {
            "payload",
            "payload_digest",
            "schema_version",
        }:
            raise RepoForgeError(
                "Approval payload fields are invalid", code=ErrorCode.STATE_CORRUPT
            )
        if raw["schema_version"] != 1 or not isinstance(raw["payload"], dict):
            raise RepoForgeError(
                "Approval payload schema is unsupported", code=ErrorCode.STATE_CORRUPT
            )
        payload = {str(key): value for key, value in raw["payload"].items()}
        _, digest = self._encoded(payload)
        if raw["payload_digest"] != digest:
            raise RepoForgeError("Approval payload digest mismatch", code=ErrorCode.STATE_CORRUPT)
        return payload

    def delete(self, request_id: str) -> None:
        with self._files.locked(request_id, operation="delete"):
            self._files.delete_bytes(request_id)
