"""JSON-backed durable worker-admission epoch (P1-3, F-012)."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from ...domain.errors import ConfigError
from ...ports.admission_epoch import ADMISSION_CLOSING, ADMISSION_OPEN
from ...ports.locking import LockManager
from ..filesystem.atomic import atomic_write_text
from ..locking.fcntl import FcntlLockManager

_EPOCH_FILE = "runtime-admission-epoch.json"
_INITIAL_EPOCH = 1
_STORE_LOCK = "worker-admission-epoch-store"


class JsonAdmissionEpochStore:
    """Durable admission epoch with atomic store-level read-modify-write."""

    def __init__(self, state_root: Path, locks: LockManager | None = None) -> None:
        root = Path(state_root)
        self._path = root / _EPOCH_FILE
        self._locks = locks or FcntlLockManager(root / "locks")

    def read(self) -> tuple[int, str]:
        return self._read_all()[:2]

    def _read_all(self) -> tuple[int, str, dict[str, object] | None]:
        if not self._path.is_file():
            return _INITIAL_EPOCH, ADMISSION_OPEN, None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
        with self._locks.lock(
            _STORE_LOCK,
            timeout_seconds=5,
            metadata={"operation": "admission_open_next"},
        ):
            current, _state, _permit = self._read_all()
            next_epoch = current + 1
            self._write(next_epoch, ADMISSION_OPEN, permit=None)
            return next_epoch

    def close(self) -> int:
        with self._locks.lock(
            _STORE_LOCK,
            timeout_seconds=5,
            metadata={"operation": "admission_close"},
        ):
            current, _state, _permit = self._read_all()
            self._write(current, ADMISSION_CLOSING, permit=None)
            return current

    def issue_permit(self, *, target: str | None) -> str:
        with self._locks.lock(
            _STORE_LOCK,
            timeout_seconds=5,
            metadata={"operation": "admission_issue_permit"},
        ):
            current, state, _permit = self._read_all()
            token = secrets.token_urlsafe(32)
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
        with self._locks.lock(
            _STORE_LOCK,
            timeout_seconds=5,
            metadata={"operation": "admission_claim_permit"},
        ):
            current, state, permit = self._read_all()
            if state != ADMISSION_CLOSING or current != epoch:
                return False
            if not isinstance(permit, dict) or permit.get("epoch") != epoch:
                return False
            if permit.get("used") or permit.get("token") != token:
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
        atomic_write_text(
            self._path,
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
        )
