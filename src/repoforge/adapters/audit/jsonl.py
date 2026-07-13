from __future__ import annotations
import json
import os
import threading
from pathlib import Path
from typing import Any
from ...ports.clock import Clock
from ..system import SystemClock


class JsonlAuditSink:
    def __init__(self, state_root: Path, clock: Clock | None = None):
        self.path = state_root / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._clock = clock or SystemClock()

    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None:
        payload = {
            "timestamp": self._clock.now_iso(),
            "pid": os.getpid(),
            "action": action,
            "success": success,
            "details": details,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as h:
            h.write(encoded + "\n")
            h.flush()


AuditLogger = JsonlAuditSink
