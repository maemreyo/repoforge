"""Private atomic persistence for operation worker bindings (sidecar to operations)."""

from __future__ import annotations

from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec
from ...domain.operation_task import validate_operation_id
from ...domain.operation_worker import (
    OPERATION_WORKER_BINDING_SCHEMA_VERSION,
    OperationWorkerBinding,
    validate_operation_worker_binding,
    worker_binding_from_payload,
    worker_binding_payload,
)
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository


class _WorkerBindingCodec(StateCodec[OperationWorkerBinding]):
    schema_version = SchemaVersion(OPERATION_WORKER_BINDING_SCHEMA_VERSION)

    def encode(self, value: OperationWorkerBinding) -> dict[str, object]:
        return worker_binding_payload(value)

    def decode(self, payload: dict[str, object]) -> OperationWorkerBinding:
        return worker_binding_from_payload(dict(payload))


class JsonWorkerBindingStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records: JsonStateRepository[OperationWorkerBinding] = JsonStateRepository(
            state_root,
            collection="operation-workers",
            locks=locks,
            codec=_WorkerBindingCodec(),
            id_validator=validate_operation_id,
            max_record_bytes=8_192,
        )
        self.root = self._records.root

    def put(self, binding: OperationWorkerBinding) -> None:
        validate_operation_worker_binding(binding)
        current = self._records.read(binding.operation_id)
        if current is None:
            self._records.create(binding.operation_id, binding)
            return
        self._records.save(
            binding.operation_id,
            binding,
            expected_revision=current.revision,
        )

    def get(self, operation_id: str) -> OperationWorkerBinding | None:
        envelope = self._records.read(operation_id)
        return envelope.value if envelope is not None else None

    def delete(self, operation_id: str) -> None:
        self._records.delete(operation_id)

    def list_all(self, *, max_records: int = 2_000) -> tuple[OperationWorkerBinding, ...]:
        page = self._records.list_records(max_records=max_records)
        return tuple(item.value for item in page.records)
