"""Private atomic JSON persistence for immutable execution-stage receipts."""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.execution_receipt import (
    EXECUTION_RECEIPT_SCHEMA_VERSION,
    StageReceipt,
    receipt_payload,
    stage_receipt_from_payload,
    validate_stage_receipt,
)
from ...ports.execution_receipt_store import ExecutionReceiptStore
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_RECEIPT_ID = re.compile(r"^receipt-[0-9a-f]{24}$")


def _receipt_id(value: str) -> str:
    if _RECEIPT_ID.fullmatch(value) is None:
        raise ValueError("invalid execution receipt id")
    return value


class _ReceiptCodec(StateCodec[StageReceipt]):
    schema_version = SchemaVersion(EXECUTION_RECEIPT_SCHEMA_VERSION)

    def encode(self, value: StageReceipt) -> dict[str, object]:
        validate_stage_receipt(value)
        return receipt_payload(value)

    def decode(self, payload: dict[str, object]) -> StageReceipt:
        return stage_receipt_from_payload(dict(payload))


class JsonExecutionReceiptStore(ExecutionReceiptStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records = JsonStateRepository(
            state_root,
            collection="execution-stage-receipts",
            locks=locks,
            codec=_ReceiptCodec(),
            id_validator=_receipt_id,
            max_record_bytes=512_000,
        )
        self.root = self._records.root

    def create(self, receipt: StageReceipt) -> StateEnvelope[StageReceipt]:
        validate_stage_receipt(receipt)
        existing = self._records.read(receipt.receipt_id)
        if existing is not None:
            if existing.value == receipt:
                return existing
            raise RepoForgeError(
                "Execution receipt id is already bound to different content",
                code=ErrorCode.ALREADY_EXISTS,
            )
        return self._records.create(receipt.receipt_id, receipt)

    def read(self, receipt_id: str) -> StateEnvelope[StageReceipt] | None:
        return self._records.read(receipt_id)

    @staticmethod
    def _page(page: StatePage[StageReceipt], predicate: object) -> StatePage[StageReceipt]:
        selected = [item for item in page.records if callable(predicate) and predicate(item.value)]
        selected.sort(key=lambda item: (item.value.ordinal, item.value.started_at, item.record_id))
        return StatePage(tuple(selected), page.scan_truncated)

    def list_for_plan(self, plan_id: str, *, max_records: int = 500) -> StatePage[StageReceipt]:
        page = self._records.list_records(max_records=max_records)
        return self._page(page, lambda receipt: receipt.plan_id == plan_id)

    def list_for_operation(
        self, operation_id: str, *, max_records: int = 500
    ) -> StatePage[StageReceipt]:
        page = self._records.list_records(max_records=max_records)
        return self._page(page, lambda receipt: receipt.operation_id == operation_id)
