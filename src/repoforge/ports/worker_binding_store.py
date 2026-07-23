"""Durable sidecar store binding a running operation to its OS worker."""

from __future__ import annotations

from typing import Protocol

from ..domain.operation_worker import OperationWorkerBinding


class WorkerBindingStore(Protocol):
    def put(self, binding: OperationWorkerBinding) -> None:
        """Create or overwrite the binding for ``binding.operation_id``."""
        ...

    def get(self, operation_id: str) -> OperationWorkerBinding | None: ...

    def delete(self, operation_id: str) -> None:
        """Remove the binding; idempotent when it is already absent."""
        ...

    def list_all(self, *, max_records: int = 2_000) -> tuple[OperationWorkerBinding, ...]: ...
