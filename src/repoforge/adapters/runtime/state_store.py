"""Identity-validated runtime state persistence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.runtime import RuntimePhase, RuntimeRecord


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
                install_origin=(
                    str(raw["install_origin"]) if raw.get("install_origin") is not None else None
                ),
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
