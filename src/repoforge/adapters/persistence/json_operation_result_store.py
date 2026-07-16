"""Private atomic persistence for bounded durable-operation results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import validate_operation_id
from ...ports.locking import LockManager
from .json_state_repository import AtomicJsonFileStore

_SCHEMA_VERSION = 1


class JsonOperationResultStore:
    def __init__(
        self,
        state_root: Path,
        locks: LockManager,
        *,
        max_result_bytes: int = 1_000_000,
    ) -> None:
        self._files = AtomicJsonFileStore(
            state_root,
            collection="operation-results",
            locks=locks,
            id_validator=validate_operation_id,
            max_record_bytes=max_result_bytes,
        )
        self.root = self._files.root

    @staticmethod
    def _error(message: str, code: ErrorCode) -> RepoForgeError:
        return RepoForgeError(
            message,
            code=code,
            safe_next_action=(
                "Inspect the durable operation and its private result record before retrying."
            ),
        )

    @classmethod
    def _encode(cls, operation_id: str, result: dict[str, Any]) -> bytes:
        if not isinstance(result, dict):
            raise cls._error("Operation result must be a JSON object", ErrorCode.STATE_INVALID)
        try:
            return (
                json.dumps(
                    {
                        "operation_id": validate_operation_id(operation_id),
                        "result": result,
                        "schema_version": _SCHEMA_VERSION,
                    },
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise cls._error(
                "Operation result is not JSON serializable", ErrorCode.STATE_INVALID
            ) from exc

    @classmethod
    def _decode(cls, data: bytes, *, expected_operation_id: str) -> dict[str, Any]:
        try:
            raw: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise cls._error(
                "Operation result is not valid UTF-8 JSON", ErrorCode.OPERATION_CORRUPT
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {
            "operation_id",
            "result",
            "schema_version",
        }:
            raise cls._error(
                "Operation result fields do not match schema version 1",
                ErrorCode.OPERATION_CORRUPT,
            )
        if raw.get("schema_version") != _SCHEMA_VERSION:
            raise cls._error(
                f"Unsupported operation result schema version: {raw.get('schema_version')!r}",
                ErrorCode.OPERATION_SCHEMA_UNSUPPORTED,
            )
        if raw.get("operation_id") != expected_operation_id:
            raise cls._error(
                "Operation result identity does not match its filename",
                ErrorCode.OPERATION_CORRUPT,
            )
        result = raw.get("result")
        if not isinstance(result, dict):
            raise cls._error(
                "Operation result payload must be an object", ErrorCode.OPERATION_CORRUPT
            )
        return result

    def save(self, operation_id: str, result: dict[str, Any]) -> None:
        safe_id = validate_operation_id(operation_id)
        data = self._encode(safe_id, result)
        with self._files.locked(safe_id, operation="save"):
            self._files.write_bytes(safe_id, data)

    def read(self, operation_id: str) -> dict[str, Any] | None:
        safe_id = validate_operation_id(operation_id)
        data = self._files.read_bytes(safe_id)
        if data is None:
            return None
        return self._decode(data, expected_operation_id=safe_id)

    def delete(self, operation_id: str) -> None:
        safe_id = validate_operation_id(operation_id)
        with self._files.locked(safe_id, operation="delete"):
            self._files.delete_bytes(safe_id)
