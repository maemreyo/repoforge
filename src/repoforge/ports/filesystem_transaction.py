"""Journaled workspace filesystem transaction boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..domain.filesystem_transaction import RecoveryReport, TransactionPlan, TransactionReceipt

FaultInjector = Callable[[str], None]
CommitReceiptFactory = Callable[[str, tuple[str, ...]], tuple[str, bytes]]


class FileTransaction(Protocol):
    """One crash-recoverable transaction engine bound to a workspace root."""

    def pending_transactions(self) -> tuple[str, ...]: ...

    def load_commit_receipt(self, name: str) -> bytes | None: ...

    def commit(
        self,
        plan: TransactionPlan,
        *,
        precommit_validator: Callable[[], None] | None = None,
        commit_receipt_factory: CommitReceiptFactory | None = None,
    ) -> TransactionReceipt: ...

    def recover_pending(self) -> RecoveryReport: ...


class FileTransactionFactory(Protocol):
    """Construct a transaction engine without exposing its concrete adapter."""

    def __call__(
        self,
        workspace_root: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> FileTransaction: ...
