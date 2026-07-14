"""Private atomic compare-and-swap persistence for durable operations."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import (
    OPERATION_SCHEMA_VERSION,
    OperationRetryability,
    OperationSnapshotBinding,
    OperationState,
    OperationTask,
    validate_operation_id,
    validate_operation_task,
)
from ...ports.locking import LockManager
from ...ports.operation_store import OperationRecordPage

_FORBIDDEN_KEYS = {
    "body",
    "content",
    "patch",
    "diff",
    "stdout",
    "stderr",
    "environment",
    "api_key",
    "access_token",
    "token",
    "secret",
    "password",
    "credential",
    "credentials",
}


class JsonOperationStore:
    def __init__(self, state_root: Path, locks: LockManager):
        self.root = state_root.expanduser().resolve() / "operations"
        self._locks = locks
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _error(
        message: str,
        *,
        code: ErrorCode,
        retryable: bool = False,
    ) -> RepoForgeError:
        return RepoForgeError(
            message,
            code=code,
            retryable=retryable,
            safe_next_action=(
                "Inspect ownership, permissions, free space, and operation state; then retry from a fresh status read."
            ),
        )

    def _path(self, operation_id: str) -> Path:
        return self.root / f"{validate_operation_id(operation_id)}.json"

    @staticmethod
    def _assert_safe(value: object, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in _FORBIDDEN_KEYS:
                    raise JsonOperationStore._error(
                        f"Operation record contains forbidden persisted field {path}{key}",
                        code=ErrorCode.OPERATION_CORRUPT,
                    )
                JsonOperationStore._assert_safe(item, f"{path}{key}.")
        elif isinstance(value, list):
            for item in value:
                JsonOperationStore._assert_safe(item, path)

    @staticmethod
    def _payload(task: OperationTask) -> dict[str, Any]:
        validate_operation_task(task)
        payload = asdict(task)
        payload["state"] = task.state.value
        payload["retryability"] = task.retryability.value
        JsonOperationStore._assert_safe(payload)
        return payload

    @staticmethod
    def _encode(task: OperationTask) -> bytes:
        return (
            json.dumps(
                JsonOperationStore._payload(task),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def encode_for_test(task: OperationTask) -> bytes:
        """Return deterministic bytes for persistence-contract tests."""
        return JsonOperationStore._encode(task)

    @staticmethod
    def _decode(data: bytes, *, expected_operation_id: str) -> OperationTask:
        try:
            raw: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JsonOperationStore._error(
                "Operation record is not valid UTF-8 JSON",
                code=ErrorCode.OPERATION_CORRUPT,
            ) from exc
        if not isinstance(raw, dict):
            raise JsonOperationStore._error(
                "Operation record must be a JSON object",
                code=ErrorCode.OPERATION_CORRUPT,
            )
        version = raw.get("schema_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != OPERATION_SCHEMA_VERSION
        ):
            raise JsonOperationStore._error(
                f"Unsupported operation schema version: {version!r}",
                code=ErrorCode.OPERATION_SCHEMA_UNSUPPORTED,
            )
        JsonOperationStore._assert_safe(raw)
        expected_fields = {
            "operation_id",
            "kind",
            "state",
            "phase",
            "progress_current",
            "progress_total",
            "progress_unit",
            "progress_message",
            "task_id",
            "workspace_id",
            "snapshot_binding",
            "result_reference",
            "error_code",
            "error_message",
            "retryability",
            "cancel_supported",
            "cancellation_requested_at",
            "created_at",
            "updated_at",
            "expires_at",
            "schema_version",
        }
        if set(raw) != expected_fields:
            raise JsonOperationStore._error(
                "Operation record fields do not match schema version 1",
                code=ErrorCode.OPERATION_CORRUPT,
            )
        if raw.get("operation_id") != expected_operation_id:
            raise JsonOperationStore._error(
                "Operation record identity does not match its filename",
                code=ErrorCode.OPERATION_CORRUPT,
            )
        binding_raw = raw.get("snapshot_binding")
        try:
            binding = (
                OperationSnapshotBinding(**binding_raw)
                if isinstance(binding_raw, dict)
                else None
                if binding_raw is None
                else (_ for _ in ()).throw(TypeError("snapshot_binding must be an object or null"))
            )
            task = OperationTask(
                operation_id=str(raw["operation_id"]),
                kind=str(raw["kind"]),
                state=OperationState(raw["state"]),
                phase=str(raw["phase"]),
                progress_current=raw["progress_current"],
                progress_total=raw["progress_total"],
                progress_unit=raw["progress_unit"],
                progress_message=raw["progress_message"],
                task_id=raw["task_id"],
                workspace_id=raw["workspace_id"],
                snapshot_binding=binding,
                result_reference=raw["result_reference"],
                error_code=raw["error_code"],
                error_message=raw["error_message"],
                retryability=OperationRetryability(raw["retryability"]),
                cancel_supported=raw["cancel_supported"],
                cancellation_requested_at=raw["cancellation_requested_at"],
                created_at=str(raw["created_at"]),
                updated_at=str(raw["updated_at"]),
                expires_at=raw["expires_at"],
                schema_version=raw["schema_version"],
            )
            return validate_operation_task(task)
        except (KeyError, TypeError, ValueError, RepoForgeError) as exc:
            if (
                isinstance(exc, RepoForgeError)
                and exc.code is ErrorCode.OPERATION_SCHEMA_UNSUPPORTED
            ):
                raise
            raise JsonOperationStore._error(
                "Operation record cannot be decoded safely",
                code=ErrorCode.OPERATION_CORRUPT,
            ) from exc

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _write(self, path: Path, data: bytes) -> None:
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600)
                self._fsync_dir(path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise self._error(
                f"Cannot persist operation record {path.name}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc

    def create(self, task: OperationTask) -> OperationTask:
        validate_operation_task(task)
        path = self._path(task.operation_id)
        with self._locks.lock(
            f"operation-{task.operation_id}",
            timeout_seconds=5,
            metadata={"operation": "create"},
        ):
            if path.exists():
                raise self._error(
                    f"Operation already exists: {task.operation_id}",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            self._write(path, self._encode(task))
        return task

    def read(self, operation_id: str) -> OperationTask | None:
        path = self._path(operation_id)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise self._error(
                f"Cannot read operation record {operation_id}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        return self._decode(data, expected_operation_id=operation_id)

    def save(self, task: OperationTask, *, expected_updated_at: str) -> OperationTask:
        validate_operation_task(task)
        path = self._path(task.operation_id)
        with self._locks.lock(
            f"operation-{task.operation_id}",
            timeout_seconds=5,
            metadata={"operation": "save"},
        ):
            current = self.read(task.operation_id)
            if current is None:
                raise self._error(
                    f"Operation not found: {task.operation_id}",
                    code=ErrorCode.OPERATION_NOT_FOUND,
                )
            if current.updated_at != expected_updated_at:
                raise self._error(
                    f"Operation changed since {expected_updated_at}; current updated_at is {current.updated_at}",
                    code=ErrorCode.OPERATION_STALE,
                    retryable=True,
                )
            self._write(path, self._encode(task))
        return task

    def list_records(self, *, max_records: int) -> OperationRecordPage:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= 2_000
        ):
            raise self._error(
                "max_records must be between 1 and 2000",
                code=ErrorCode.OPERATION_INVALID,
            )
        paths = sorted(self.root.glob("op-*.json"))
        scan_truncated = len(paths) > max_records
        records: list[OperationTask] = []
        for path in paths[:max_records]:
            record = self.read(path.stem)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: (item.updated_at, item.operation_id), reverse=True)
        return OperationRecordPage(tuple(records), scan_truncated)

    def delete(self, operation_id: str) -> None:
        path = self._path(operation_id)
        with self._locks.lock(
            f"operation-{operation_id}",
            timeout_seconds=5,
            metadata={"operation": "delete"},
        ):
            existed = path.exists()
            try:
                path.unlink(missing_ok=True)
                if existed:
                    self._fsync_dir(path.parent)
            except OSError as exc:
                raise self._error(
                    f"Cannot delete operation record {operation_id}",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=True,
                ) from exc
