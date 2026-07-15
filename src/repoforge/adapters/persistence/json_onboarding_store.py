"""Private, atomic, optimistic JSON persistence for onboarding sessions."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.onboarding import (
    DiscoveryCandidate,
    DiscoveryExclusion,
    DiscoveryIdentity,
    ExclusionReason,
    OnboardingOptions,
    OnboardingRepositoryState,
    OnboardingSession,
    OnboardingStatus,
    RepositoryProgress,
    transition_session,
)
from ...ports.locking import LockManager

_FORBIDDEN_KEYS = {"api_key", "token", "secret", "stdout", "stderr", "patch", "diff", "environment"}


class JsonOnboardingStore:
    def __init__(self, root: Path, locks: LockManager):
        self.root = root.expanduser().resolve() / "onboarding"
        self._locks = locks

    def _path(self, session_id: str) -> Path:
        if not session_id or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for ch in session_id
        ):
            raise ConfigError("SESSION_NOT_FOUND: invalid session id")
        return self.root / f"{session_id}.json"

    @staticmethod
    def _assert_safe(value: object, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                if any(part in lowered for part in _FORBIDDEN_KEYS):
                    raise ConfigError(f"SESSION_CORRUPT: forbidden persisted field {path}{key}")
                JsonOnboardingStore._assert_safe(item, f"{path}{key}.")
        elif isinstance(value, list):
            for item in value:
                JsonOnboardingStore._assert_safe(item, path)

    @staticmethod
    def _encode(session: OnboardingSession) -> bytes:
        payload = asdict(session)
        JsonOnboardingStore._assert_safe(payload)
        return (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()

    @staticmethod
    def _decode(data: bytes) -> OnboardingSession:
        try:
            raw: Any = json.loads(data)
            if not isinstance(raw, dict) or raw.get("schema_version") != 1:
                raise ValueError("unsupported schema")
            JsonOnboardingStore._assert_safe(raw)
            options = OnboardingOptions(**raw["options"])
            repositories = []
            for item in raw.get("repositories", []):
                identity = DiscoveryIdentity(**item["candidate"]["identity"])
                candidate = DiscoveryCandidate(
                    identity, item["candidate"]["repo_id"], item["candidate"].get("parent_repo_id")
                )
                required = tuple(
                    (str(entry[0]), str(entry[1]), tuple(str(v) for v in entry[2]))
                    for entry in item.get("required_decisions", [])
                )
                repositories.append(
                    OnboardingRepositoryState(
                        candidate=candidate,
                        progress=RepositoryProgress(item["progress"]),
                        template=item.get("template", "standard"),
                        decisions=tuple(
                            (str(pair[0]), str(pair[1])) for pair in item.get("decisions", [])
                        ),
                        overrides=tuple(
                            (str(pair[0]), str(pair[1])) for pair in item.get("overrides", [])
                        ),
                        proposal_id=item.get("proposal_id"),
                        facts_fingerprint=item.get("facts_fingerprint"),
                        approval_sha256=item.get("approval_sha256"),
                        required_decisions=required,
                        proposal_json=item.get("proposal_json"),
                        error_code=item.get("error_code"),
                    )
                )
            exclusions = tuple(
                DiscoveryExclusion(
                    str(item["path"]),
                    ExclusionReason(item["reason"]),
                    str(item.get("detail", "")),
                    item.get("repo_id"),
                )
                for item in raw.get("exclusions", [])
            )
            return OnboardingSession(
                schema_version=1,
                session_id=str(raw["session_id"]),
                revision=int(raw["revision"]),
                created_at=str(raw["created_at"]),
                updated_at=str(raw["updated_at"]),
                status=OnboardingStatus(raw["status"]),
                config_path=str(raw["config_path"]),
                roots=tuple(str(v) for v in raw["roots"]),
                options=options,
                expected_source_sha256=raw.get("expected_source_sha256"),
                expected_generation=int(raw.get("expected_generation", 0)),
                repositories=tuple(repositories),
                exclusions=exclusions,
                warnings=tuple(str(v) for v in raw.get("warnings", [])),
                accepted_generation=raw.get("accepted_generation"),
                active_generation=raw.get("active_generation"),
                last_error=tuple(
                    (str(pair[0]), str(pair[1])) for pair in raw.get("last_error", [])
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConfigError("SESSION_CORRUPT: onboarding session cannot be decoded") from exc

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._fsync_dir(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def create(self, session: OnboardingSession) -> OnboardingSession:
        path = self._path(session.session_id)
        with self._locks.lock(
            f"onboarding-{session.session_id}", timeout_seconds=5, metadata={"operation": "create"}
        ):
            if path.exists():
                raise ConfigError("ALREADY_EXISTS: onboarding session already exists")
            self._write(path, self._encode(session))
        return session

    def read(self, session_id: str) -> OnboardingSession | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            return self._decode(path.read_bytes())
        except OSError as exc:
            raise ConfigError("SESSION_CORRUPT: onboarding session is unreadable") from exc

    def save(self, session: OnboardingSession, *, expected_revision: int) -> OnboardingSession:
        path = self._path(session.session_id)
        with self._locks.lock(
            f"onboarding-{session.session_id}", timeout_seconds=5, metadata={"operation": "save"}
        ):
            current = self.read(session.session_id)
            if current is None:
                raise ConfigError("SESSION_NOT_FOUND: onboarding session does not exist")
            if current.revision != expected_revision:
                raise ConfigError(
                    f"SESSION_STALE: expected revision {expected_revision}, found {current.revision}"
                )
            updated = replace(session, revision=expected_revision + 1)
            self._write(path, self._encode(updated))
            return updated

    def discard(self, session_id: str) -> None:
        path = self._path(session_id)
        lock_name = f"onboarding-{session_id}"
        lock_path = self._locks.path_for(lock_name)
        try:
            with self._locks.lock(
                lock_name,
                timeout_seconds=5,
                metadata={"operation": "discard"},
            ):
                path.unlink(missing_ok=True)
                self._fsync_dir(path.parent)
            lock_path.unlink(missing_ok=True)
            self._fsync_dir(lock_path.parent)
        except OSError as exc:
            raise ConfigError(
                "SESSION_PERSISTENCE_FAILED: provisional session cannot be discarded"
            ) from exc

    def cancel(
        self, session_id: str, *, expected_revision: int, updated_at: str
    ) -> OnboardingSession:
        current = self.read(session_id)
        if current is None:
            raise ConfigError("SESSION_NOT_FOUND: onboarding session does not exist")
        cancelled = transition_session(current, OnboardingStatus.CANCELLED, now=updated_at)
        return self.save(cancelled, expected_revision=expected_revision)
