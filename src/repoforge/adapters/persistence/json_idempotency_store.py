"""Private crash-safe idempotency record persistence."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError, ErrorCode
from ...domain.operations import IdempotencyRecord, IdempotencyState
from ...domain.redaction import sanitize_persisted_data

_SAFE_ACTION = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_SAFE_HASH = re.compile(r"^[a-f0-9]{64}$")


class JsonIdempotencyStore:
    def __init__(self, state_root: Path):
        self.root = state_root / "idempotency"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

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

    @staticmethod
    def _persistence_error(message: str, exc: BaseException, *, retryable: bool) -> ConfigError:
        return ConfigError(
            f"STATE_PERSISTENCE_FAILED: {message}: {exc}",
            code=ErrorCode.STATE_PERSISTENCE_FAILED,
            retryable=retryable,
            safe_next_action=(
                "Check ownership, permissions, free space, and filesystem health under state_root; "
                "then retry with the same idempotency key."
            ),
        )

    def _path(self, action: str, key_hash: str) -> Path:
        if not _SAFE_ACTION.fullmatch(action) or not _SAFE_HASH.fullmatch(key_hash):
            raise ConfigError("Invalid idempotency record identity")
        return self.root / f"{action}-{key_hash}.json"

    def load(self, action: str, key_hash: str) -> IdempotencyRecord | None:
        path = self._path(action, key_hash)
        if not path.is_file():
            return None
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            raw["state"] = IdempotencyState(raw["state"])
            record = IdempotencyRecord(**raw)
        except OSError as exc:
            raise self._persistence_error(
                f"cannot read idempotency record {path}", exc, retryable=True
            ) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise self._persistence_error(
                f"idempotency record is corrupt {path}", exc, retryable=False
            ) from exc
        if record.action != action or record.key_hash != key_hash:
            mismatch = ValueError("record identity does not match its filename")
            raise self._persistence_error(
                f"idempotency record is corrupt {path}", mismatch, retryable=False
            )
        return record

    def save(self, record: IdempotencyRecord) -> None:
        destination = self._path(record.action, record.key_hash)
        temporary = destination.with_name(
            f".{destination.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        )
        payload = asdict(record)
        payload["state"] = record.state.value
        payload["result"] = sanitize_persisted_data(payload.get("result"))
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            self._fsync_dir(destination.parent)
        except OSError as exc:
            raise self._persistence_error(
                f"cannot save idempotency record {destination}", exc, retryable=True
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, action: str, key_hash: str) -> None:
        destination = self._path(action, key_hash)
        existed = destination.exists()
        try:
            destination.unlink(missing_ok=True)
            if existed:
                self._fsync_dir(destination.parent)
        except OSError as exc:
            raise self._persistence_error(
                f"cannot delete idempotency record {destination}", exc, retryable=True
            ) from exc
