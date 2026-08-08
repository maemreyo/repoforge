"""Shared private, bounded, atomic JSON-file mechanics for state records.

``AtomicJsonFileStore`` owns the filesystem mechanics: safe identifiers,
per-record locking, atomic temp-file writes with fsync, bounded reads, and
listing.  ``JsonStateRepository`` (``json_state_repository``) layers typed
envelope encoding and revision compare-and-swap on top.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.locking import LockManager

T = TypeVar("T")

_COLLECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _state_error(
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
            raise _state_error("State collection name is unsafe", ErrorCode.STATE_INVALID)
        if (
            not isinstance(max_record_bytes, int)
            or isinstance(max_record_bytes, bool)
            or not 64 <= max_record_bytes <= 25 * 1024 * 1024
        ):
            raise _state_error(
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
            raise _state_error(
                f"Cannot initialize state collection {collection}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc

    def record_id(self, value: str) -> str:
        try:
            validated = self._id_validator(value)
        except (TypeError, ValueError, RepoForgeError) as exc:
            raise _state_error(
                "State record identifier is invalid", ErrorCode.STATE_INVALID
            ) from exc
        if (
            validated != value
            or _SAFE_RECORD_ID.fullmatch(validated) is None
            or "/" in validated
            or "\\" in validated
        ):
            raise _state_error("State record identifier is unsafe", ErrorCode.STATE_INVALID)
        return validated

    def path(self, record_id: str) -> Path:
        return self.root / f"{self.record_id(record_id)}.json"

    @contextmanager
    def locked(self, record_id: str, *, operation: str) -> Iterator[None]:
        safe_id = self.record_id(record_id)
        with (
            self._locks.shared_lock(
                f"state-lifecycle-{self._collection}",
                timeout_seconds=5,
                metadata={"operation": operation, "scope": "record_mutation"},
            ),
            self._locks.lock(
                f"state-{self._collection}-{safe_id}",
                timeout_seconds=5,
                metadata={"operation": operation},
            ),
        ):
            yield

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
            raise _state_error(
                f"Cannot read state record {path.name}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        if len(data) > self._max_record_bytes:
            raise _state_error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        return data

    def write_bytes(self, record_id: str, data: bytes) -> None:
        if len(data) > self._max_record_bytes:
            raise _state_error(
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
            raise _state_error(
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
            raise _state_error(
                f"Cannot delete state record {path.name}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc

    def list_ids(
        self, *, pattern: str, max_records: int, offset: int = 0
    ) -> tuple[tuple[str, ...], bool]:
        paths = sorted(self.root.glob(pattern))
        return (
            tuple(path.stem for path in paths[offset : offset + max_records]),
            offset + max_records < len(paths),
        )


__all__ = ["AtomicJsonFileStore", "_state_error"]
