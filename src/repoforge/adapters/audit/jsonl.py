"""Private, redacted, bounded JSONL audit sink."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError, ErrorCode
from ...domain.redaction import redact_data
from ...ports.clock import Clock
from ..system import SystemClock


class JsonlAuditSink:
    def __init__(
        self,
        state_root: Path,
        clock: Clock | None = None,
        *,
        max_bytes: int = 5_000_000,
        backup_count: int = 3,
        max_event_bytes: int = 64 * 1024,
    ):
        self.path = state_root / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._lock = threading.Lock()
        self._clock = clock or SystemClock()
        self._max_bytes = max(1, max_bytes)
        self._backup_count = max(1, backup_count)
        self._max_event_bytes = max(1_024, max_event_bytes)

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

    def _rotate(self, incoming_bytes: int) -> bool:
        if not self.path.is_file() or self.path.stat().st_size + incoming_bytes <= self._max_bytes:
            return False
        oldest = self.path.with_suffix(self.path.suffix + f".{self._backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = self.path.with_suffix(self.path.suffix + f".{index}")
            if source.exists():
                os.replace(source, self.path.with_suffix(self.path.suffix + f".{index + 1}"))
        os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        return True

    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None:
        payload = {
            "timestamp": self._clock.now_iso(),
            "pid": os.getpid(),
            "action": action,
            "success": success,
            "details": redact_data(details),
        }
        encoded = (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > self._max_event_bytes:
            payload["details"] = {
                "event_truncated": True,
                "event_sha256": hashlib.sha256(encoded).hexdigest(),
                "original_bytes": len(encoded),
            }
            encoded = (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode(
                "utf-8"
            )
        try:
            with self._lock:
                rotated = self._rotate(len(encoded))
                existed = self.path.exists()
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "ab", buffering=0) as handle:
                    handle.write(encoded)
                    os.fsync(handle.fileno())
                os.chmod(self.path, 0o600)
                if rotated or not existed:
                    self._fsync_dir(self.path.parent)
        except OSError as exc:
            raise ConfigError(
                f"STATE_PERSISTENCE_FAILED: cannot append private audit log {self.path}: {exc}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
                safe_next_action=(
                    "Check state_root ownership, permissions, free space, and filesystem health; "
                    "then reconcile the operation by correlation id before retrying."
                ),
                unchanged_state=(
                    "Existing durable application state was not rewritten by the failed audit append.",
                ),
            ) from exc


AuditLogger = JsonlAuditSink
