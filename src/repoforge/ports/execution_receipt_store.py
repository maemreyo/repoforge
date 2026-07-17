"""Persistence boundary for immutable execution-stage receipts."""

from __future__ import annotations

from typing import Protocol

from ..domain.durable_state import StateEnvelope, StatePage
from ..domain.execution_receipt import StageReceipt


class ExecutionReceiptStore(Protocol):
    def create(self, receipt: StageReceipt) -> StateEnvelope[StageReceipt]: ...

    def read(self, receipt_id: str) -> StateEnvelope[StageReceipt] | None: ...

    def list_for_plan(self, plan_id: str, *, max_records: int = 500) -> StatePage[StageReceipt]: ...

    def list_for_operation(
        self, operation_id: str, *, max_records: int = 500
    ) -> StatePage[StageReceipt]: ...
