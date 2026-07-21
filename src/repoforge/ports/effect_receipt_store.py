"""Persistence boundary for durable effect-boundary receipts."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.execution_receipt import EffectReceipt


class EffectReceiptStore(Protocol):
    def create(self, receipt: EffectReceipt) -> StateEnvelope[EffectReceipt]: ...

    def read(self, receipt_id: str) -> StateEnvelope[EffectReceipt] | None: ...

    def save(
        self,
        receipt: EffectReceipt,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[EffectReceipt]: ...

    def list_all(self, *, max_records: int = 2_000) -> StatePage[EffectReceipt]: ...

    def list_for_operation(
        self, operation_id: str, *, max_records: int = 500
    ) -> StatePage[EffectReceipt]: ...

    def list_for_idempotency(
        self,
        action: str,
        key_hash: str,
        *,
        max_records: int = 500,
    ) -> StatePage[EffectReceipt]: ...
