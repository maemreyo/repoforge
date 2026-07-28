"""Persistence boundary for durable runtime-activation receipts."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import Revision, StateEnvelope, StatePage
from ..domain.runtime_activation import RuntimeActivationReceipt


class RuntimeActivationStore(Protocol):
    def create(
        self, receipt: RuntimeActivationReceipt
    ) -> StateEnvelope[RuntimeActivationReceipt]: ...

    def read(self, receipt_id: str) -> StateEnvelope[RuntimeActivationReceipt] | None: ...

    def save(
        self,
        receipt: RuntimeActivationReceipt,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[RuntimeActivationReceipt]: ...

    def list_all(self, *, max_records: int = 2_000) -> StatePage[RuntimeActivationReceipt]: ...
