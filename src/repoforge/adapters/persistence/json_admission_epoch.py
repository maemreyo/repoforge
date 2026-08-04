"""JSON-backed durable worker-admission epoch (P1-3, F-012)."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
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
        return self._read_all()[:2]

    def _read_all(self) -> tuple[int, str, dict[str, object] | None]:
        if not self._path.is_file():
            return _INITIAL_EPOCH, ADMISSION_OPEN, None
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
        permit = raw.get("permit")
        if permit is not None and not isinstance(permit, dict):
            raise ConfigError(f"ADMISSION_EPOCH_INVALID: {self._path} has an invalid permit")
        return epoch, str(state), permit

    def open_next(self) -> int:
        current, _state = self.read()
        next_epoch = current + 1
        self._write(next_epoch, ADMISSION_OPEN, permit=None)
        return next_epoch

    def close(self) -> int:
        current, _state = self.read()
        # A new handoff starts with an empty permit slot: a stale permit from a
        # previous handoff must never be claimable in this one (F-012).
        self._write(current, ADMISSION_CLOSING, permit=None)
        return current

    def issue_permit(self, *, target: str | None) -> str:
        """Issue a single-use replacement permit for the current epoch (F-012).

        Rotates the slot: writing a new permit atomically invalidates any previous
        handoff's permit, so a failed candidate's permit can never be reused by a
        rollback -- the rollback must issue its own, bound to its own target.
        """
        current, state = self.read()
        token = secrets.token_urlsafe(24)
        self._write(
            current,
            state,
            permit={
                "epoch": current,
                "token": token,
                "target": target,
                "used": False,
            },
        )
        return token

    def claim_permit(self, epoch: int, *, token: str, target: str | None) -> bool:
        """Atomically claim the current epoch's permit (F-012); False on any mismatch."""
        current, state, permit = self._read_all()
        if state != ADMISSION_CLOSING or current != epoch:
            return False
        if not isinstance(permit, dict) or permit.get("epoch") != epoch:
            return False
        if permit.get("used"):
            return False
        if permit.get("token") != token:
            return False
        bound_target = permit.get("target")
        if bound_target is not None and bound_target != target:
            return False
        self._write(current, state, permit={**permit, "used": True})
        return True

    def _write(
        self,
        epoch: int,
        state: str,
        *,
        permit: dict[str, object] | None,
    ) -> None:
        payload: dict[str, object] = {"epoch": epoch, "state": state}
        if permit is not None:
            payload["permit"] = permit
        _atomic_write_json(self._path, payload)
