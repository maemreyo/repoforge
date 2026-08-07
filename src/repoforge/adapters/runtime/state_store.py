"""Identity-validated runtime state persistence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.runtime import (
    RUNTIME_CONTROL_PROTOCOL_VERSION,
    RestartHistoryRecord,
    RuntimePhase,
    RuntimeRecord,
)


def process_identity(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    facts = completed.stdout.strip()
    if completed.returncode != 0 or not facts:
        return None
    return hashlib.sha256(facts.encode()).hexdigest()


class JsonRuntimeStore:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                directory = os.open(path.parent, os.O_RDONLY)
                os.fsync(directory)
                os.close(directory)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    def read(self) -> RuntimeRecord | None:
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid runtime state {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Runtime state {self.path} must be an object")
        try:
            record = RuntimeRecord(
                protocol_version=int(raw["protocol_version"]),
                phase=RuntimePhase(str(raw["phase"])),
                pid=int(raw["pid"]) if raw.get("pid") is not None else None,
                process_identity=str(raw["process_identity"])
                if raw.get("process_identity") is not None
                else None,
                active_generation=int(raw["active_generation"])
                if raw.get("active_generation") is not None
                else None,
                accepted_generation=int(raw["accepted_generation"]),
                tunnel_profile=str(raw["tunnel_profile"]),
                tunnel_profile_fingerprint=str(raw["tunnel_profile_fingerprint"]),
                tool_surface_hash=str(raw["tool_surface_hash"]),
                started_at=str(raw["started_at"]) if raw.get("started_at") is not None else None,
                updated_at=str(raw["updated_at"]),
                correlation_id=str(raw["correlation_id"]),
                child_pid=int(raw["child_pid"]) if raw.get("child_pid") is not None else None,
                child_process_identity=str(raw["child_process_identity"])
                if raw.get("child_process_identity") is not None
                else None,
                restart_count=int(raw.get("restart_count", 0)),
                last_error_code=str(raw["last_error_code"])
                if raw.get("last_error_code") is not None
                else None,
                last_error=str(raw["last_error"]) if raw.get("last_error") is not None else None,
                health=tuple((str(a), bool(b), str(c)) for a, b, c in raw.get("health", [])),
                package_version=(
                    str(raw["package_version"]) if raw.get("package_version") is not None else None
                ),
                executable=str(raw["executable"]) if raw.get("executable") is not None else None,
                running_release_sha=(
                    str(raw["running_release_sha"])
                    if raw.get("running_release_sha") is not None
                    else None
                ),
                install_origin=(
                    str(raw["install_origin"]) if raw.get("install_origin") is not None else None
                ),
                health_observed_at=(
                    str(raw["health_observed_at"])
                    if raw.get("health_observed_at") is not None
                    else None
                ),
                consecutive_health_failures=int(raw.get("consecutive_health_failures", 0)),
                # Read what the writer writes. Omitting these defaulted `restarts_total` to
                # 0 on every read while `restart_count` came back as written, so a record
                # saved after even one restart could not be decoded into a valid object and
                # the runtime refused to start.
                restarts_total=int(raw.get("restarts_total", 0)),
                last_restart_at=(
                    str(raw["last_restart_at"]) if raw.get("last_restart_at") is not None else None
                ),
                fail_closed_since=(
                    str(raw["fail_closed_since"])
                    if raw.get("fail_closed_since") is not None
                    else None
                ),
                incarnation_id=(
                    str(raw["incarnation_id"]) if raw.get("incarnation_id") is not None else None
                ),
                restart_history_provenance=str(raw.get("restart_history_provenance", "unknown")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid runtime state fields in {self.path}: {exc}") from exc
        if record.pid is not None and (
            record.process_identity is None
            or process_identity(record.pid) != record.process_identity
        ):
            self.clear()
            return None
        if record.child_pid is not None and (
            record.child_process_identity is None
            or process_identity(record.child_pid) != record.child_process_identity
        ):
            degraded = replace(
                record,
                phase=RuntimePhase.DEGRADED,
                child_pid=None,
                child_process_identity=None,
                last_error_code="CHILD_IDENTITY_MISMATCH",
                last_error="Recorded tunnel child is no longer the owned process",
            )
            self.write(degraded)
            return degraded
        return record

    def write(self, record: RuntimeRecord) -> None:
        payload = asdict(record)
        payload["phase"] = record.phase.value
        self._atomic(self.path, payload)

    def clear(self, *, expected_pid: int | None = None) -> None:
        if expected_pid is not None:
            current = self.read()
            if current is not None and current.pid != expected_pid:
                return
        self.path.unlink(missing_ok=True)

    def peek_restart_evidence(self) -> tuple[int, str | None] | None:
        """Read `restarts_total`/`last_restart_at` straight off disk, bypassing the
        pid-liveness self-heal entirely (#448 Slice 4 migration).

        For one-time restart-history-ledger seeding only: a release upgraded from
        before the ledger existed can have real evidence sitting in this file that
        `read()` would otherwise discard the instant it self-heals (which fires on
        essentially every real restart, since a fresh incarnation is never the same
        pid as the one that just died -- see `read()`'s own docstring). Best-effort
        and silent on any problem: this is migration evidence, not a correctness-
        critical read, so a corrupt or unreadable file just yields nothing to seed
        from rather than raising.
        """
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        total = raw.get("restarts_total")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            return None
        last_restart_at = raw.get("last_restart_at")
        return total, (last_restart_at if isinstance(last_restart_at, str) else None)


class JsonRestartHistoryStore:
    """A durable restart-history ledger, deliberately not `JsonRuntimeStore`.

    Backed by its own file, never touched by `JsonRuntimeStore`'s pid-liveness
    self-heal: that check is correct for "is this claim about a running process
    still true" but wrong for restart history, which is true regardless of
    whether the process that produced it is still alive (#448 Slice 4).
    """

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _decode(raw: Any, *, path: Path) -> RestartHistoryRecord:
        if not isinstance(raw, dict):
            raise ConfigError(f"Restart history state {path} must be an object")
        try:
            return RestartHistoryRecord(
                protocol_version=int(raw["protocol_version"]),
                restarts_total=int(raw["restarts_total"]),
                last_restart_at=(
                    str(raw["last_restart_at"]) if raw.get("last_restart_at") is not None else None
                ),
                incarnation_id=str(raw["incarnation_id"]),
                updated_at=str(raw["updated_at"]),
                last_event_id=(
                    str(raw["last_event_id"]) if raw.get("last_event_id") is not None else None
                ),
                last_restart_reason=(
                    str(raw["last_restart_reason"])
                    if raw.get("last_restart_reason") is not None
                    else None
                ),
                provenance=str(raw.get("provenance", "durable")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid restart history fields in {path}: {exc}") from exc

    def _read_unlocked(self) -> RestartHistoryRecord | None:
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid restart history state {self.path}: {exc}") from exc
        return self._decode(raw, path=self.path)

    def read(self) -> RestartHistoryRecord | None:
        return self._read_unlocked()

    def write(self, record: RestartHistoryRecord) -> None:
        JsonRuntimeStore._atomic(self.path, asdict(record))

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold an OS-level exclusive lock across one read-modify-write.

        `JsonRuntimeStore._atomic`'s write-then-`os.replace` already makes any SINGLE
        write atomic -- what it does not do is make read-then-decide-then-write
        atomic across TWO overlapping callers, which is exactly the shape of the
        supervisor handoff this whole ledger exists for: two incarnations can be
        briefly alive at once, and without this lock both could read the same
        baseline `restarts_total` and each write back the same incremented value,
        silently losing one restart's worth of evidence (#448 Slice 4). A sibling
        `.lock` file is used rather than locking the ledger file itself, so a
        concurrent plain `read()` is never blocked by a writer holding this lock.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def record_restart(
        self,
        *,
        incarnation_id: str,
        reason: str | None,
        occurred_at: str,
        event_id: str,
    ) -> RestartHistoryRecord:
        """Atomically increment `restarts_total` for one logical restart, exactly once.

        Guarded by `_exclusive()` for the entire read-modify-write, closing the
        lost-update race a bare `read()` + `write()` pair has under overlapping
        writers. Idempotent on `event_id`: a caller that replays the same logical
        restart event (e.g. retrying after a write it could not confirm landed)
        gets the existing record back unchanged rather than a second increment.
        """
        with self._exclusive():
            current = self._read_unlocked()
            if current is not None and current.last_event_id == event_id:
                return current
            record = RestartHistoryRecord(
                protocol_version=RUNTIME_CONTROL_PROTOCOL_VERSION,
                restarts_total=(current.restarts_total if current is not None else 0) + 1,
                last_restart_at=occurred_at,
                incarnation_id=incarnation_id,
                updated_at=occurred_at,
                last_event_id=event_id,
                last_restart_reason=reason,
                provenance="durable",
            )
            JsonRuntimeStore._atomic(self.path, asdict(record))
            return record

    def seed_if_missing(
        self,
        *,
        restarts_total: int,
        last_restart_at: str | None,
        incarnation_id: str,
        occurred_at: str,
    ) -> RestartHistoryRecord:
        """Initialize the ledger from legacy evidence, but ONLY if it does not exist yet.

        Migration case (#448 Slice 4): a ledger-unaware release's `RuntimeRecord` can
        carry real `restarts_total`/`last_restart_at` history with no ledger to match
        it, e.g. `restarts_total=6` the very first time this release runs. Seeding
        from that (marked `provenance="legacy_runtime_record"`, never silently as
        `"durable"`) is strictly better than starting over at a false `0`. Guarded by
        the same lock as `record_restart()` so two incarnations racing to seed for
        the first time cannot each seed independently and disagree; a ledger that
        already exists always wins over this legacy snapshot and is returned as-is,
        untouched.
        """
        with self._exclusive():
            current = self._read_unlocked()
            if current is not None:
                return current
            record = RestartHistoryRecord(
                protocol_version=RUNTIME_CONTROL_PROTOCOL_VERSION,
                restarts_total=max(0, restarts_total),
                last_restart_at=last_restart_at,
                incarnation_id=incarnation_id,
                updated_at=occurred_at,
                last_event_id=None,
                last_restart_reason=None,
                provenance="legacy_runtime_record",
            )
            JsonRuntimeStore._atomic(self.path, asdict(record))
            return record
