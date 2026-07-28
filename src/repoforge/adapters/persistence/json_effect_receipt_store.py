"""Private CAS JSON persistence for durable effect-boundary receipts."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.execution_receipt import (
    EFFECT_RECEIPT_SCHEMA_VERSION,
    EffectReceipt,
    effect_receipt_from_payload,
    effect_receipt_payload,
    validate_effect_receipt,
)
from ...ports.effect_receipt_store import EffectReceiptStore
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

_RECEIPT_ID = re.compile(r"^receipt-[0-9a-f]{24}$")


def _receipt_id(value: str) -> str:
    if _RECEIPT_ID.fullmatch(value) is None:
        raise ValueError("invalid effect receipt id")
    return value


class _EffectReceiptCodec(StateCodec[EffectReceipt]):
    schema_version = SchemaVersion(EFFECT_RECEIPT_SCHEMA_VERSION)

    def encode(self, value: EffectReceipt) -> dict[str, object]:
        validate_effect_receipt(value)
        return effect_receipt_payload(value)

    def decode(self, payload: dict[str, object]) -> EffectReceipt:
        return effect_receipt_from_payload(dict(payload))


class JsonEffectReceiptStore(EffectReceiptStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records = JsonStateRepository(
            state_root,
            collection="effect-receipts",
            locks=locks,
            codec=_EffectReceiptCodec(),
            id_validator=_receipt_id,
            max_record_bytes=256_000,
        )
        self.root = self._records.root

    def create(self, receipt: EffectReceipt) -> StateEnvelope[EffectReceipt]:
        return self._records.create(receipt.receipt_id, validate_effect_receipt(receipt))

    def read(self, receipt_id: str) -> StateEnvelope[EffectReceipt] | None:
        return self._records.read(receipt_id)

    def save(
        self,
        receipt: EffectReceipt,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[EffectReceipt]:
        return self._records.save(
            receipt.receipt_id,
            validate_effect_receipt(receipt),
            expected_revision=expected_revision,
        )

    def list_all(self, *, max_records: int = 2_000) -> StatePage[EffectReceipt]:
        return self._records.list_records(max_records=max_records)

    @staticmethod
    def _page(
        page: StatePage[EffectReceipt],
        predicate: Callable[[EffectReceipt], bool],
    ) -> StatePage[EffectReceipt]:
        selected = [item for item in page.records if predicate(item.value)]
        selected.sort(key=lambda item: (item.value.updated_at, item.record_id), reverse=True)
        return StatePage(tuple(selected), page.scan_truncated)

    def list_for_operation(
        self, operation_id: str, *, max_records: int = 500
    ) -> StatePage[EffectReceipt]:
        page = self._records.list_records(max_records=max_records)
        return self._page(page, lambda receipt: receipt.operation_id == operation_id)

    def list_for_idempotency(
        self,
        action: str,
        key_hash: str,
        *,
        max_records: int = 500,
    ) -> StatePage[EffectReceipt]:
        page = self._records.list_records(max_records=max_records)
        return self._page(
            page,
            lambda receipt: receipt.action == action and receipt.idempotency_key_hash == key_hash,
        )
