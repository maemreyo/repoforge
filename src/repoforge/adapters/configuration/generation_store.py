"""Crash-safe immutable configuration generation storage."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ...domain.config_generation import (
    ApprovalEvent,
    CapabilityDeltaKind,
    ConfigGeneration,
    ConfigMutation,
    classify_capability_delta,
    sha256_text,
)
from ...domain.errors import ConfigError
from ...ports.locking import LockManager


class ConfigGenerationStore:
    FORMAT_VERSION = 3
    RETAINED_GENERATIONS = 20

    def __init__(self, source_path: Path, state_root: Path, locks: LockManager):
        self._source_path = source_path.expanduser().resolve()
        digest = sha256_text(str(self._source_path))[:16]
        self._root = state_root.expanduser().resolve() / "config-locks" / digest
        self.root = self._root
        self.generations = self.root / "generations-v3"
        self.accepted_pointer = self.root / "accepted-v3.json"
        self.active_pointer = self.root / "active-v3.json"
        self.activation_target_pointer = self.root / "activation-target-v3.json"
        self._active_resolved_path = self.root / "resolved.toml"
        self._locks = locks
        self.generations.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def source_path(self) -> Path:
        return self._source_path

    @property
    def active_resolved_path(self) -> Path:
        return self._active_resolved_path

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _atomic_write(cls, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            cls._fsync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _integer(raw: dict[str, Any], key: str, *, optional: bool = False) -> int | None:
        value = raw.get(key)
        if optional and value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        return value

    @classmethod
    def _generation_from_json(cls, raw: dict[str, Any], *, active: bool) -> ConfigGeneration:
        approval_raw = raw.get("approval")
        approval = ApprovalEvent(**approval_raw) if isinstance(approval_raw, dict) else None
        return ConfigGeneration(
            generation=cls._integer(raw, "generation") or 0,
            source_sha256=str(raw["source_sha256"]),
            resolved_sha256=str(raw["resolved_sha256"]),
            repository_fingerprints=tuple(
                (str(a), str(b)) for a, b in raw.get("repository_fingerprints", [])
            ),
            created_at=str(raw["created_at"]),
            reason=str(raw["reason"]),
            proposal_id=str(raw["proposal_id"]) if raw.get("proposal_id") is not None else None,
            approval=approval,
            delta=CapabilityDeltaKind(str(raw["delta"])),
            previous_generation=cls._integer(raw, "previous_generation", optional=True),
            correlation_id=str(raw.get("correlation_id", "")),
            active=active,
        )

    def _pointer_generation(self, path: Path) -> int | None:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = raw.get("generation") if isinstance(raw, dict) else None
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("generation must be positive")
            return value
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ConfigError(f"Invalid configuration pointer {path}: {exc}") from exc

    def _path(self, generation: int) -> Path:
        if generation <= 0:
            raise ConfigError("Config generation must be positive")
        return self.generations / str(generation)

    def _load(self, generation: int) -> ConfigGeneration:
        root = self._path(generation)
        metadata = root / "generation.json"
        source = root / "config.toml"
        resolved = root / "resolved.toml"
        if not metadata.is_file() or not source.is_file() or not resolved.is_file():
            raise ConfigError(f"Incomplete configuration generation: {generation}")
        try:
            raw = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Invalid generation metadata {metadata}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("format_version") != self.FORMAT_VERSION:
            raise ConfigError(f"Unsupported generation metadata: {metadata}")
        try:
            item = self._generation_from_json(
                raw, active=self._pointer_generation(self.active_pointer) == generation
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid generation metadata fields {metadata}: {exc}") from exc
        if sha256_text(source.read_text(encoding="utf-8")) != item.source_sha256:
            raise ConfigError(f"Source hash mismatch for generation {generation}")
        if sha256_text(resolved.read_text(encoding="utf-8")) != item.resolved_sha256:
            raise ConfigError(f"Resolved hash mismatch for generation {generation}")
        return item

    def current(self) -> ConfigGeneration | None:
        generation = self._pointer_generation(self.accepted_pointer)
        return self._load(generation) if generation is not None else None

    def active(self) -> ConfigGeneration | None:
        generation = self._pointer_generation(self.active_pointer)
        return self._load(generation) if generation is not None else None

    def activation_target(self) -> ConfigGeneration | None:
        generation = self._pointer_generation(self.activation_target_pointer)
        return self._load(generation) if generation is not None else None

    def stage_activation(
        self, generation: int, *, expected_active: int | None = None
    ) -> ConfigGeneration:
        with self._locks.lock(
            "config-activation", timeout_seconds=30, metadata={"operation": "stage"}
        ):
            active = self._pointer_generation(self.active_pointer)
            if expected_active is not None and active != expected_active:
                raise ConfigError(
                    f"STALE_ACTIVE_GENERATION: expected {expected_active}, found {active}"
                )
            selected = self._load(generation)
            self._atomic_write(
                self.activation_target_pointer,
                (json.dumps({"generation": generation}) + "\n").encode(),
            )
            return replace(selected, active=False)

    def clear_activation_target(self, *, expected_generation: int | None = None) -> None:
        with self._locks.lock(
            "config-activation", timeout_seconds=30, metadata={"operation": "clear-target"}
        ):
            current = self._pointer_generation(self.activation_target_pointer)
            if expected_generation is not None and current not in {None, expected_generation}:
                raise ConfigError(
                    f"STALE_ACTIVATION_TARGET: expected {expected_generation}, found {current}"
                )
            self.activation_target_pointer.unlink(missing_ok=True)
            self._fsync_dir(self.activation_target_pointer.parent)

    def history(self) -> tuple[ConfigGeneration, ...]:
        items: list[ConfigGeneration] = []
        for path in sorted(
            self.generations.iterdir() if self.generations.is_dir() else (),
            key=lambda item: int(item.name) if item.name.isdigit() else -1,
            reverse=True,
        ):
            if path.is_dir() and path.name.isdigit():
                items.append(self._load(int(path.name)))
        return tuple(items)

    def next_generation(self) -> int:
        """Return a monotonic generation number across the full immutable history."""

        history = self.history()
        return history[0].generation + 1 if history else 1

    def read_source_text(self) -> str:
        if not self.source_path.is_file():
            raise ConfigError(f"Configuration file not found: {self.source_path}")
        return self.source_path.read_text(encoding="utf-8")

    def generation_path(self, generation: int) -> Path:
        """Return the immutable directory for a validated generation."""
        self._load(generation)
        return self._path(generation)

    def resolved_path(self, generation: int) -> Path:
        """Return the immutable resolved config path for a validated generation."""
        return self.generation_path(generation) / "resolved.toml"

    def read_resolved_text(self, generation: int | None = None) -> str:
        selected = generation
        if selected is None:
            current = self.current()
            if current is None:
                raise ConfigError("No accepted configuration generation")
            selected = current.generation
        self._load(selected)
        return (self._path(selected) / "resolved.toml").read_text(encoding="utf-8")

    def _store_generation(
        self, generation: ConfigGeneration, source_text: str, resolved_text: str
    ) -> None:
        destination = self._path(generation.generation)
        if destination.exists():
            raise ConfigError(f"Configuration generation already exists: {generation.generation}")
        temporary = (
            self.generations / f".{generation.generation}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        )
        temporary.mkdir(mode=0o700)
        try:
            self._atomic_write(temporary / "config.toml", source_text.encode())
            self._atomic_write(temporary / "resolved.toml", resolved_text.encode())
            payload = {"format_version": self.FORMAT_VERSION, **asdict(generation)}
            payload["delta"] = generation.delta.value
            payload["active"] = False
            self._atomic_write(
                temporary / "generation.json",
                (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(),
            )
            self._fsync_dir(temporary)
            os.replace(temporary, destination)
            self._fsync_dir(self.generations)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def accept(self, mutation: ConfigMutation) -> ConfigGeneration:
        with self._locks.lock(
            "config-generation", timeout_seconds=30, metadata={"operation": "accept"}
        ):
            # Fail closed on immutable-history corruption and never reuse a historical number,
            # even when the accepted pointer was deliberately rolled back to an older generation.
            next_generation = self.next_generation()
            current = self.current()
            current_generation = current.generation if current else 0
            if (
                mutation.expected_generation is not None
                and mutation.expected_generation != current_generation
            ):
                raise ConfigError(
                    f"STALE_CONFIG_GENERATION: expected {mutation.expected_generation}, found {current_generation}"
                )
            actual_source_sha = (
                sha256_text(self.source_path.read_text(encoding="utf-8"))
                if self.source_path.is_file()
                else None
            )
            if (
                mutation.expected_source_sha256 is not None
                and actual_source_sha != mutation.expected_source_sha256
            ):
                raise ConfigError("Configuration changed concurrently; reload and retry")
            if current is not None:
                delta = classify_capability_delta(
                    self.read_resolved_text(current.generation), mutation.resolved_text
                )
                if delta.kind is CapabilityDeltaKind.EQUIVALENT:
                    # Formatting/comments in the editable source do not create a new immutable
                    # runtime generation. The accepted snapshot remains the reviewed source of
                    # truth until a semantic change is accepted.
                    return current
            else:
                delta_kind = CapabilityDeltaKind.EXPANSION
            if current is not None:
                delta_kind = classify_capability_delta(
                    self.read_resolved_text(current.generation), mutation.resolved_text
                ).kind
            if delta_kind in {CapabilityDeltaKind.EXPANSION, CapabilityDeltaKind.INCOMPATIBLE}:
                if mutation.approval is None or mutation.proposal_id is None:
                    raise ConfigError(
                        "APPROVAL_REQUIRED: capability expansion requires an approved proposal"
                    )
                if mutation.approval.proposal_id != mutation.proposal_id:
                    raise ConfigError("Approval event does not match the proposal")
            generation = ConfigGeneration(
                generation=next_generation,
                source_sha256=sha256_text(mutation.source_text),
                resolved_sha256=sha256_text(mutation.resolved_text),
                repository_fingerprints=tuple(sorted(mutation.repository_fingerprints)),
                created_at=mutation.created_at,
                reason=mutation.reason,
                proposal_id=mutation.proposal_id,
                approval=mutation.approval,
                delta=delta_kind,
                previous_generation=current_generation or None,
                correlation_id=mutation.correlation_id,
            )
            self._store_generation(generation, mutation.source_text, mutation.resolved_text)
            previous_source = self.source_path.read_bytes() if self.source_path.is_file() else None
            try:
                # The editable source is durable before the accepted pointer is committed. If the
                # pointer write fails, both the source and orphan generation are compensated.
                self._atomic_write(self.source_path, mutation.source_text.encode())
                self._atomic_write(
                    self.accepted_pointer,
                    (json.dumps({"generation": generation.generation}) + "\n").encode(),
                )
            except Exception:
                if previous_source is None:
                    self.source_path.unlink(missing_ok=True)
                else:
                    self._atomic_write(self.source_path, previous_source)
                shutil.rmtree(self._path(generation.generation), ignore_errors=True)
                self._fsync_dir(self.generations)
                raise
            self._prune()
            return generation

    def activate(self, generation: int, *, expected_active: int | None = None) -> ConfigGeneration:
        """Commit an already healthy generation as active.

        Callers must stage the target before launching the runtime. The active pointer is changed only
        after the supervisor has completed its health gate. Repeating the commit is idempotent.
        """
        with self._locks.lock(
            "config-activation", timeout_seconds=30, metadata={"operation": "commit"}
        ):
            active = self._pointer_generation(self.active_pointer)
            if active == generation:
                target = self._pointer_generation(self.activation_target_pointer)
                if target in {None, generation}:
                    self.activation_target_pointer.unlink(missing_ok=True)
                    self._fsync_dir(self.activation_target_pointer.parent)
                return replace(self._load(generation), active=True)
            if expected_active is not None and active != expected_active:
                raise ConfigError(
                    f"STALE_ACTIVE_GENERATION: expected {expected_active}, found {active}"
                )
            target = self._pointer_generation(self.activation_target_pointer)
            if target != generation:
                raise ConfigError(
                    f"ACTIVATION_TARGET_MISMATCH: staged {target}, attempted {generation}"
                )
            selected = self._load(generation)
            resolved = self.read_resolved_text(generation)
            self._atomic_write(self.active_resolved_path, resolved.encode())
            self._atomic_write(
                self.active_pointer, (json.dumps({"generation": generation}) + "\n").encode()
            )
            self.activation_target_pointer.unlink(missing_ok=True)
            self._fsync_dir(self.activation_target_pointer.parent)
            return replace(selected, active=True)

    def rollback(
        self,
        generation: int,
        *,
        expected_active: int | None,
        approval_token: str | None = None,
    ) -> ConfigGeneration:
        """Select an immutable generation as accepted without bypassing runtime activation.

        Runtime activation is deliberately owned by ``GenerationActivator`` so rollback receives
        the same drain, health and fail-closed guarantees as every other generation transition.
        """
        with self._locks.lock(
            "config-generation", timeout_seconds=30, metadata={"operation": "rollback"}
        ):
            target = self._load(generation)
            active = self.active()
            active_number = active.generation if active else None
            if expected_active is not None and active_number != expected_active:
                raise ConfigError(
                    f"STALE_ACTIVE_GENERATION: expected {expected_active}, found {active_number}"
                )
            if active is not None:
                delta = classify_capability_delta(
                    self.read_resolved_text(active.generation), self.read_resolved_text(generation)
                )
                if delta.kind in {
                    CapabilityDeltaKind.EXPANSION,
                    CapabilityDeltaKind.INCOMPATIBLE,
                }:
                    required = f"rollback:{generation}:{target.resolved_sha256[:16]}"
                    if approval_token != required:
                        raise ConfigError(
                            f"ROLLBACK_APPROVAL_REQUIRED: use approval token {required}"
                        )
            source_text = (self._path(generation) / "config.toml").read_text(encoding="utf-8")
            previous_source = self.source_path.read_bytes() if self.source_path.is_file() else None
            previous_accepted = self._pointer_generation(self.accepted_pointer)
            try:
                self._atomic_write(self.source_path, source_text.encode())
                self._atomic_write(
                    self.accepted_pointer,
                    (json.dumps({"generation": generation}) + "\n").encode(),
                )
            except Exception:
                if previous_source is None:
                    self.source_path.unlink(missing_ok=True)
                else:
                    self._atomic_write(self.source_path, previous_source)
                if previous_accepted is None:
                    self.accepted_pointer.unlink(missing_ok=True)
                else:
                    self._atomic_write(
                        self.accepted_pointer,
                        (json.dumps({"generation": previous_accepted}) + "\n").encode(),
                    )
                raise
            return replace(target, active=False)

    def import_legacy(
        self, source_text: str, resolved_text: str, *, created_at: str
    ) -> ConfigGeneration:
        current = self.current()
        if current is not None:
            return current
        mutation = ConfigMutation(
            source_text=source_text,
            resolved_text=resolved_text,
            repository_fingerprints=(),
            reason="legacy configuration import",
            created_at=created_at,
            expected_generation=0,
            expected_source_sha256=sha256_text(source_text) if self.source_path.is_file() else None,
            proposal_id="legacy-import",
            approval=ApprovalEvent(
                "migration", created_at, "legacy-import", sha256_text("legacy-import")
            ),
            correlation_id="legacy-import",
        )
        # Legacy import establishes the accepted immutable baseline only. Runtime activation
        # still passes through the normal staged health gate before the active pointer moves.
        return self.accept(mutation)

    def _prune(self) -> None:
        protected = {
            item
            for item in (
                self._pointer_generation(self.accepted_pointer),
                self._pointer_generation(self.active_pointer),
                self._pointer_generation(self.activation_target_pointer),
            )
            if item is not None
        }
        history = self.history()
        keep = {item.generation for item in history[: self.RETAINED_GENERATIONS]} | protected
        for item in history:
            if item.generation not in keep:
                shutil.rmtree(self._path(item.generation), ignore_errors=True)
