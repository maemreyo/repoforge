"""Private atomic persistence for durable operation work items."""

from __future__ import annotations

from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import validate_operation_id
from ...domain.operation_work import (
    OperationWorkItem,
    OperationWorkState,
    claim_work_item,
    work_item_from_payload,
    work_item_payload,
)
from ...ports.locking import LockManager
from ...ports.operation_work_queue import OperationWorkPage
from .json_state_repository import JsonStateRepository


class _OperationWorkCodec(StateCodec[OperationWorkItem]):
    schema_version = SchemaVersion(1)

    def encode(self, value: OperationWorkItem) -> dict[str, object]:
        return work_item_payload(value)

    def decode(self, payload: dict[str, object]) -> OperationWorkItem:
        return work_item_from_payload(dict(payload))


class JsonOperationWorkQueue:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records: JsonStateRepository[OperationWorkItem] = JsonStateRepository(
            state_root,
            collection="operation-work-v1",
            locks=locks,
            codec=_OperationWorkCodec(),
            id_validator=validate_operation_id,
            max_record_bytes=32_768,
        )
        self.root = self._records.root

    def create(self, item: OperationWorkItem) -> OperationWorkItem:
        self._records.create(item.operation_id, item)
        return item

    def read(self, operation_id: str) -> OperationWorkItem | None:
        envelope = self._records.read(operation_id)
        return envelope.value if envelope is not None else None

    def save(
        self,
        item: OperationWorkItem,
        *,
        expected_updated_at: str,
    ) -> OperationWorkItem:
        current = self._records.read(item.operation_id)
        if current is None:
            raise RepoForgeError(
                f"Operation work not found: {item.operation_id}",
                code=ErrorCode.OPERATION_NOT_FOUND,
            )
        if current.value.updated_at != expected_updated_at:
            raise RepoForgeError(
                "Operation work changed since the reviewed updated_at",
                code=ErrorCode.OPERATION_STALE,
                retryable=True,
            )
        self._records.save(
            item.operation_id,
            item,
            expected_revision=current.revision,
        )
        return item

    def claim_next(
        self,
        *,
        owner_id: str,
        now: str,
        lease_expires_at: str,
        compatible_kinds: frozenset[str],
        config_generation: int | None = None,
    ) -> OperationWorkItem | None:
        page = self._records.list_records(max_records=2_000)
        candidates = sorted(
            (
                envelope
                for envelope in page.records
                if envelope.value.state is OperationWorkState.QUEUED
                and envelope.value.request.kind in compatible_kinds
                and (
                    config_generation is None
                    or envelope.value.request.config_generation == config_generation
                )
            ),
            key=lambda envelope: (
                envelope.value.created_at,
                envelope.value.operation_id,
            ),
        )
        for envelope in candidates:
            claimed = claim_work_item(
                envelope.value,
                owner_id=owner_id,
                lease_expires_at=lease_expires_at,
                now=now,
            )
            try:
                saved = self._records.save(
                    claimed.operation_id,
                    claimed,
                    expected_revision=envelope.revision,
                )
            except RepoForgeError as exc:
                if exc.code is ErrorCode.STATE_STALE:
                    continue
                raise
            return saved.value
        return None

    def list_records(self, *, max_records: int) -> OperationWorkPage:
        page = self._records.list_records(max_records=max_records)
        records = tuple(envelope.value for envelope in page.records)
        return OperationWorkPage(records, page.scan_truncated, page.unreadable_record_ids)

    def delete(self, operation_id: str) -> None:
        self._records.delete(operation_id)
