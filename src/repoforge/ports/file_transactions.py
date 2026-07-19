"""Application boundary for crash-recoverable workspace file transactions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..domain.filesystem_transaction import (
    RecoveryReport,
    TransactionPlan,
    TransactionReceipt,
)

CommitReceiptFactory = Callable[[str, tuple[str, ...]], tuple[str, bytes]]
"""Given (transaction_id, changed_paths) return (name, payload) for the receipt."""


class FileTransaction(Protocol):
    def recover_pending(self) -> RecoveryReport: ...

    def load_commit_receipt(self, name: str) -> bytes | None: ...

    def commit(
        self,
        plan: TransactionPlan,
        *,
        precommit_validator: Callable[[], None] | None = None,
        commit_receipt_factory: CommitReceiptFactory | None = None,
    ) -> TransactionReceipt: ...


class FileTransactionFactory(Protocol):
    def create(self, workspace_root: Path) -> FileTransaction: ...
