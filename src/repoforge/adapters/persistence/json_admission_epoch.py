"""JSON-backed durable worker-admission epoch (P1-3)."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ...domain.errors import ConfigError
from ...ports.admission_epoch import ADMISSION_CLOSING, ADMISSION_OPEN

_EPOCH_FILE = "runtime-admission-epoch.json"
_INITIAL_EPOCH = 1


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class JsonAdmissionEpochStore:
    """Durable admission epoch in one JSON file under the state root."""

    def __init__(self, state_root: Path) -> None:
        self._path = Path(state_root) / _EPOCH_FILE

    def read(self) -> tuple[int, str]:
        if not self._path.is_file():
            return _INITIAL_EPOCH, ADMISSION_OPEN
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # An unreadable fence must fail closed, not silently reopen: a restarter
            # that cannot prove admission is open must refuse to stop the incumbent.
            raise ConfigError(
                f"ADMISSION_EPOCH_UNREADABLE: cannot read the worker-admission epoch "
                f"{self._path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"ADMISSION_EPOCH_INVALID: {self._path} is not an object")
        epoch = raw.get("epoch")
        state = raw.get("state")
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch <= 0
            or state not in {ADMISSION_OPEN, ADMISSION_CLOSING}
        ):
            raise ConfigError(f"ADMISSION_EPOCH_INVALID: {self._path} has invalid fields")
        return epoch, str(state)

    def open_next(self) -> int:
        current, _state = self.read()
        next_epoch = current + 1
        self._write(next_epoch, ADMISSION_OPEN)
        return next_epoch

    def close(self) -> int:
        current, _state = self.read()
        self._write(current, ADMISSION_CLOSING)
        return current

    def _write(self, epoch: int, state: str) -> None:
        _atomic_write_json(self._path, {"epoch": epoch, "state": state})
