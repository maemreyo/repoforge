"""Extended transaction factory for receipt-bound and fault-injected operations."""

from __future__ import annotations

from pathlib import Path

from .receipt_transaction import FaultInjector, JournaledFileTransaction


class ReceiptJournaledFileTransactionFactory:
    """Create the production engine while retaining reviewed fault injection hooks."""

    def create(self, workspace_root: Path) -> JournaledFileTransaction:
        return JournaledFileTransaction(workspace_root)

    def __call__(
        self,
        workspace_root: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> JournaledFileTransaction:
        return JournaledFileTransaction(workspace_root, fault_injector=fault_injector)
