"""Production background-task and sleep adapters."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ThreadBackgroundTaskRunner:
    """Run one daemon task per stable key and release the key on every exit path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def submit(self, key: str, task: Callable[[], None]) -> bool:
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)

        def run() -> None:
            try:
                task()
            finally:
                with self._lock:
                    self._active.discard(key)

        threading.Thread(
            target=run,
            name=f"repoforge-{key}",
            daemon=True,
        ).start()
        return True


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
