"""Private CAS JSON persistence for runtime-activation receipts."""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.runtime_activation import (
    RUNTIME_ACTIVATION_SCHEMA_VERSION,
    RuntimeActivationReceipt,
    runtime_activation_receipt_from_payload,
    runtime_activation_receipt_payload,
    validate_runtime_activation_receipt,
)
from ...ports.locking import LockManager
from ...ports.runtime_activation_store import RuntimeActivationStore
from .json_state_repository import JsonStateRepository

_RECEIPT_ID = re.compile(r"^receipt-[a-f0-9]{24}$")


def _receipt_id(value: str) -> str:
    if _RECEIPT_ID.fullmatch(value) is None:
        raise ValueError("invalid runtime activation receipt id")
    return value


class _RuntimeActivationCodec(StateCodec[RuntimeActivationReceipt]):
    schema_version = SchemaVersion(RUNTIME_ACTIVATION_SCHEMA_VERSION)

    def encode(self, value: RuntimeActivationReceipt) -> dict[str, object]:
        return runtime_activation_receipt_payload(validate_runtime_activation_receipt(value))

    def decode(self, payload: dict[str, object]) -> RuntimeActivationReceipt:
        return runtime_activation_receipt_from_payload(dict(payload))


class JsonRuntimeActivationStore(RuntimeActivationStore):
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records = JsonStateRepository(
            state_root,
            collection="runtime-activations",
            locks=locks,
            codec=_RuntimeActivationCodec(),
            id_validator=_receipt_id,
            max_record_bytes=256_000,
        )
        self.root = self._records.root

    def create(self, receipt: RuntimeActivationReceipt) -> StateEnvelope[RuntimeActivationReceipt]:
        return self._records.create(
            receipt.receipt_id, validate_runtime_activation_receipt(receipt)
        )

    def read(self, receipt_id: str) -> StateEnvelope[RuntimeActivationReceipt] | None:
        return self._records.read(receipt_id)

    def save(
        self,
        receipt: RuntimeActivationReceipt,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[RuntimeActivationReceipt]:
        return self._records.save(
            receipt.receipt_id,
            validate_runtime_activation_receipt(receipt),
            expected_revision=expected_revision,
        )

    def list_all(self, *, max_records: int = 2_000) -> StatePage[RuntimeActivationReceipt]:
        return self._records.list_records(max_records=max_records)
