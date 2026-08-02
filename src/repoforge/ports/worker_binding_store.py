"""Durable sidecar store binding a running operation to its OS worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.operation_worker import OperationWorkerBinding


@dataclass(frozen=True, slots=True)
class WorkerBindingPage:
    """One bounded scan of the worker-binding store.

    ``scan_complete`` is ``False`` when the store held more records than the
    scan budget, and ``unreadable_ids`` lists records that could not be
    decoded. A handoff that cannot prove it saw every binding must fail
    closed rather than assume the unseen records are safe (F-008).
    """

    records: tuple[OperationWorkerBinding, ...]
    scan_complete: bool = True
    unreadable_ids: tuple[str, ...] = ()


class WorkerBindingStore(Protocol):
    def put(self, binding: OperationWorkerBinding) -> None:
        """Create or overwrite the binding for ``binding.operation_id``."""
        ...

    def get(self, operation_id: str) -> OperationWorkerBinding | None: ...

    def delete(self, operation_id: str) -> None:
        """Remove the binding; idempotent when it is already absent."""
        ...

    def delete_if_unchanged(self, binding: OperationWorkerBinding) -> bool:
        """Remove the binding only while it still matches ``binding`` exactly.

        Returns ``False`` without deleting when the stored record has been replaced
        since it was read, so a generation handoff can never release a *newer*
        generation's binding that was written after its scan.
        """
        ...

    def list_all(self, *, max_records: int = 2_000) -> WorkerBindingPage:
        """Scan every binding within ``max_records``, reporting scan completeness."""
        ...
