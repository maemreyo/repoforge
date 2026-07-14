"""Private atomic JSON persistence for typed state envelopes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Generic, TypeVar

from ...domain.durable_state import Revision, StateCodec, StateEnvelope, StatePage
from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.locking import LockManager

T = TypeVar("T")
_COLLECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AtomicJsonFileStore:
    """Shared private, bounded, atomic JSON-file mechanics."""

    def __init__(
        self,
        state_root: Path,
        *,
        collection: str,
        locks: LockManager,
        id_validator: Callable[[str], str],
        max_record_bytes: int = 1_000_000,
    ) -> None:
        if _COLLECTION.fullmatch(collection) is None:
            raise JsonStateRepository._error(
                "State collection name is unsafe", ErrorCode.STATE_INVALID
            )
        if (
            not isinstance(max_record_bytes, int)
            or isinstance(max_record_bytes, bool)
            or not 64 <= max_record_bytes <= 25 * 1024 * 1024
        ):
            raise JsonStateRepository._error(
                "max_record_bytes must be between 64 and 26214400",
                ErrorCode.STATE_INVALID,
            )
        self.root = state_root.expanduser().resolve() / collection
        self._collection = collection
        self._locks = locks
        self._id_validator = id_validator
        self._max_record_bytes = max_record_bytes
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
        except OSError as exc:
            raise JsonStateRepository._error(
                f"Cannot initialize state collection {collection}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc

    def record_id(self, value: str) -> str:
        try:
            validated = self._id_validator(value)
        except (TypeError, ValueError, RepoForgeError) as exc:
            raise JsonStateRepository._error(
                "State record identifier is invalid", ErrorCode.STATE_INVALID
            ) from exc
        if (
            validated != value
            or _SAFE_RECORD_ID.fullmatch(validated) is None
            or "/" in validated
            or "\\" in validated
        ):
            raise JsonStateRepository._error(
                "State record identifier is unsafe", ErrorCode.STATE_INVALID
            )
        return validated

    def path(self, record_id: str) -> Path:
        return self.root / f"{self.record_id(record_id)}.json"

    def locked(self, record_id: str, *, operation: str) -> AbstractContextManager[None]:
        safe_id = self.record_id(record_id)
        return self._locks.lock(
            f"state-{self._collection}-{safe_id}",
            timeout_seconds=5,
            metadata={"operation": operation},
        )

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

    def read_bytes(self, record_id: str) -> bytes | None:
        path = self.path(record_id)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise JsonStateRepository._error(
                f"Cannot read state record {path.name}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        if len(data) > self._max_record_bytes:
            raise JsonStateRepository._error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        return data

    def write_bytes(self, record_id: str, data: bytes) -> None:
        if len(data) > self._max_record_bytes:
            raise JsonStateRepository._error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        path = self.path(record_id)
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
            raise JsonStateRepository._error(
                f"Cannot persist state record {path.name}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc

    def delete_bytes(self, record_id: str) -> None:
        path = self.path(record_id)
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
            if existed:
                self._fsync_dir(path.parent)
        except OSError as exc:
            raise JsonStateRepository._error(
                f"Cannot delete state record {path.name}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc

    def list_ids(self, *, pattern: str, max_records: int) -> tuple[tuple[str, ...], bool]:
        paths = sorted(self.root.glob(pattern))
        return tuple(path.stem for path in paths[:max_records]), len(paths) > max_records


class JsonStateRepository(Generic[T]):
    """Bounded deterministic storage with private permissions and revision CAS."""

    def __init__(
        self,
        state_root: Path,
        *,
        collection: str,
        locks: LockManager,
        codec: StateCodec[T],
        id_validator: Callable[[str], str],
        max_record_bytes: int = 1_000_000,
    ) -> None:
        self._files = AtomicJsonFileStore(
            state_root,
            collection=collection,
            locks=locks,
            id_validator=id_validator,
            max_record_bytes=max_record_bytes,
        )
        self.root = self._files.root
        self.collection = collection
        self._codec = codec
        self._max_record_bytes = max_record_bytes

    @staticmethod
    def _error(
        message: str,
        code: ErrorCode,
        *,
        retryable: bool = False,
    ) -> RepoForgeError:
        return RepoForgeError(
            message,
            code=code,
            retryable=retryable,
            safe_next_action=(
                "Inspect state ownership, permissions, free space, schema compatibility, and the latest revision before retrying."
            ),
        )

    def _record_id(self, value: str) -> str:
        return self._files.record_id(value)

    def _path(self, record_id: str) -> Path:
        return self._files.path(record_id)

    def _encode(self, envelope: StateEnvelope[T]) -> bytes:
        payload = {
            "payload": self._codec.encode(envelope.value),
            "record_id": envelope.record_id,
            "revision": envelope.revision.value,
            "schema_version": envelope.schema_version.value,
        }
        try:
            encoded = (
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise self._error(
                "State payload is not JSON serializable", ErrorCode.STATE_INVALID
            ) from exc
        if len(encoded) > self._max_record_bytes:
            raise self._error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        return encoded

    def _decode(self, data: bytes, *, expected_record_id: str) -> StateEnvelope[T]:
        if len(data) > self._max_record_bytes:
            raise self._error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        try:
            raw: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._error(
                "State record is not valid UTF-8 JSON", ErrorCode.STATE_CORRUPT
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {
            "payload",
            "record_id",
            "revision",
            "schema_version",
        }:
            raise self._error(
                "State record fields do not match the envelope", ErrorCode.STATE_CORRUPT
            )
        if raw.get("record_id") != expected_record_id:
            raise self._error(
                "State record identity does not match its filename", ErrorCode.STATE_CORRUPT
            )
        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise self._error("State schema version is invalid", ErrorCode.STATE_CORRUPT)
        if version != self._codec.schema_version.value:
            raise self._error(
                f"Unsupported state schema version: {version}",
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            )
        revision_raw = raw.get("revision")
        if not isinstance(revision_raw, int) or isinstance(revision_raw, bool):
            raise self._error("State revision is invalid", ErrorCode.STATE_CORRUPT)
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise self._error("State payload must be an object", ErrorCode.STATE_CORRUPT)
        try:
            value = self._codec.decode(payload)
            revision = Revision(revision_raw)
        except (TypeError, ValueError, RepoForgeError) as exc:
            raise self._error(
                "State record cannot be decoded safely", ErrorCode.STATE_CORRUPT
            ) from exc
        return StateEnvelope(expected_record_id, self._codec.schema_version, revision, value)

    def _write(self, path: Path, data: bytes) -> None:
        self._files.write_bytes(path.stem, data)

    def create(self, record_id: str, value: T) -> StateEnvelope[T]:
        safe_id = self._record_id(record_id)
        path = self._path(safe_id)
        envelope = StateEnvelope(safe_id, self._codec.schema_version, Revision(1), value)
        data = self._encode(envelope)
        with self._files.locked(safe_id, operation="create"):
            if path.exists():
                raise self._error(
                    f"State record already exists: {safe_id}", ErrorCode.ALREADY_EXISTS
                )
            self._write(path, data)
        return envelope

    def read(self, record_id: str) -> StateEnvelope[T] | None:
        safe_id = self._record_id(record_id)
        data = self._files.read_bytes(safe_id)
        if data is None:
            return None
        return self._decode(data, expected_record_id=safe_id)

    def save(
        self,
        record_id: str,
        value: T,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[T]:
        safe_id = self._record_id(record_id)
        path = self._path(safe_id)
        with self._files.locked(safe_id, operation="save"):
            current = self.read(safe_id)
            if current is None:
                raise self._error(f"State record not found: {safe_id}", ErrorCode.STATE_NOT_FOUND)
            if current.revision != expected_revision:
                raise self._error(
                    f"State record changed from revision {expected_revision.value} to {current.revision.value}",
                    ErrorCode.STATE_STALE,
                    retryable=True,
                )
            envelope = StateEnvelope(
                safe_id,
                self._codec.schema_version,
                current.revision.next(),
                value,
            )
            self._write(path, self._encode(envelope))
        return envelope

    def list_records(self, *, max_records: int) -> StatePage[T]:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= 2_000
        ):
            raise self._error("max_records must be between 1 and 2000", ErrorCode.STATE_INVALID)
        record_ids, scan_truncated = self._files.list_ids(pattern="*.json", max_records=max_records)
        records: list[StateEnvelope[T]] = []
        for record_id in record_ids:
            item = self.read(record_id)
            if item is not None:
                records.append(item)
        records.sort(key=lambda item: (item.revision.value, item.record_id), reverse=True)
        return StatePage(tuple(records), scan_truncated)

    def delete(self, record_id: str) -> None:
        safe_id = self._record_id(record_id)
        with self._files.locked(safe_id, operation="delete"):
            self._files.delete_bytes(safe_id)
