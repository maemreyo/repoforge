"""Timed, metadata-bearing Unix advisory locks."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from ...domain.errors import ConfigError

try:
    import fcntl
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("RepoForge requires fcntl on a Unix-like platform") from exc


class FcntlLockManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _validate(name: str) -> str:
        if not name or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for ch in name
        ):
            raise ConfigError(f"Invalid lock name: {name!r}")
        return name

    def path_for(self, name: str) -> Path:
        return self.root / f"{self._validate(name)}.lock"

    @contextmanager
    def _acquire(
        self,
        name: str,
        *,
        shared: bool,
        timeout_seconds: float | None,
        metadata: dict[str, str] | None,
    ) -> Iterator[None]:
        path = self.path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        with path.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise ConfigError(f"LOCK_TIMEOUT: could not acquire {name!r}") from exc
                    time.sleep(0.05)
            if not shared:
                payload = {"pid": os.getpid(), "acquired_at_ns": time.time_ns(), **(metadata or {})}
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                yield
            finally:
                if not shared:
                    handle.seek(0)
                    handle.truncate()
                    handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def lock(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
        metadata: dict[str, str] | None = None,
    ) -> AbstractContextManager[None]:
        return self._acquire(
            name,
            shared=False,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )

    def shared_lock(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
        metadata: dict[str, str] | None = None,
    ) -> AbstractContextManager[None]:
        return self._acquire(
            name,
            shared=True,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )
