"""System clock and random identifier adapters."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


class SystemClock:
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class UuidGenerator:
    def new_hex(self, length: int = 10) -> str:
        if length <= 0:
            raise ValueError("length must be positive")
        return uuid.uuid4().hex[:length]
