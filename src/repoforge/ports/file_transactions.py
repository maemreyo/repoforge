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


class FileTransaction(Protocol):
    def recover_pending(self) -> RecoveryReport: ...

    def commit(
        self,
        plan: TransactionPlan,
        *,
        precommit_validator: Callable[[], None] | None = None,
    ) -> TransactionReceipt: ...


class FileTransactionFactory(Protocol):
    def create(self, workspace_root: Path) -> FileTransaction: ...
