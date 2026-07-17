"""Crash-recoverable, journaled file transactions for one managed worktree."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from ...domain.filesystem_transaction import (
    CreateFile,
    DeleteFile,
    MoveFile,
    RecoveryReport,
    SimulatedTransactionCrash,
    TransactionAction,
    TransactionPlan,
    TransactionReceipt,
    TransactionRecoveryError,
    TransactionValidationError,
    WriteFile,
)

FaultInjector = Callable[[str], None]
_MANIFEST_SCHEMA_VERSION = 1
_MAX_ACTIONS = 100


class JournaledFileTransaction:
    """Apply ordered file actions with durable rollback/finalization recovery."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        root = workspace_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise TransactionValidationError("workspace_root must be an existing directory")
        self.root = root
        self._fault_injector = fault_injector
        root_key = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:24]
        self._journal_root = root.parent / ".repoforge-transactions" / root_key
        self.last_stage_device: int | None = None

    def _checkpoint(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def pending_transactions(self) -> tuple[str, ...]:
        if not self._journal_root.is_dir():
            return ()
        return tuple(
            path.name
            for path in sorted(self._journal_root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / "manifest.json").is_file()
        )

    def commit(
        self,
        plan: TransactionPlan,
        *,
        precommit_validator: Callable[[], None] | None = None,
    ) -> TransactionReceipt:
        """Validate, stage, durably apply, mark committed, then purge rollback data."""

        actions = self._validate_plan(plan)
        self.recover_pending()
        transaction_id = uuid.uuid4().hex
        tx_dir = self._journal_root / transaction_id
        manifest: dict[str, object] | None = None
        try:
            manifest = self._prepare(tx_dir, transaction_id, actions)
            self._checkpoint("after_manifest_prepared")
            for index, action in enumerate(actions):
                self._checkpoint(f"before_apply:{index}")
                self._apply_action(tx_dir, index, action)
                self._checkpoint(f"after_apply:{index}")
            if precommit_validator is not None:
                precommit_validator()
            self._checkpoint("before_commit_marker")
            manifest["phase"] = "committed"
            self._write_manifest(tx_dir, manifest)
            self._checkpoint("after_commit_marker")
            self._purge_transaction(tx_dir)
        except SimulatedTransactionCrash:
            raise
        except Exception as primary:
            if manifest is None:
                self._remove_unprepared(tx_dir)
                raise
            try:
                self._rollback(tx_dir, manifest)
            except Exception as rollback:
                raise TransactionRecoveryError(
                    f"Transaction failed: {primary}; rollback failure: {rollback}"
                ) from primary
            raise

        changed = sorted({path for action in actions for path in self._action_paths(action)})
        return TransactionReceipt(transaction_id, tuple(changed))

    def recover_pending(self) -> RecoveryReport:
        """Rollback prepared journals and finalize committed journals idempotently."""

        rolled_back = 0
        finalized = 0
        if not self._journal_root.is_dir():
            return RecoveryReport(rolled_back=0, finalized=0)
        for tx_dir in sorted(self._journal_root.iterdir(), key=lambda item: item.name):
            if not tx_dir.is_dir():
                continue
            manifest_path = tx_dir / "manifest.json"
            if not manifest_path.is_file():
                raise TransactionRecoveryError(f"Transaction journal {tx_dir.name} has no manifest")
            manifest = self._read_manifest(manifest_path)
            phase = manifest.get("phase")
            if phase == "committed":
                self._purge_transaction(tx_dir)
                finalized += 1
            elif phase == "prepared":
                try:
                    self._rollback(tx_dir, manifest)
                except Exception as exc:
                    raise TransactionRecoveryError(
                        f"Could not recover transaction {tx_dir.name}: {exc}"
                    ) from exc
                rolled_back += 1
            else:
                raise TransactionRecoveryError(
                    f"Transaction journal {tx_dir.name} has invalid phase {phase!r}"
                )
        return RecoveryReport(rolled_back=rolled_back, finalized=finalized)

    def _validate_plan(self, plan: TransactionPlan) -> tuple[TransactionAction, ...]:
        actions = tuple(plan.actions)
        if not actions:
            raise TransactionValidationError("Transaction plan must contain at least one action")
        if len(actions) > _MAX_ACTIONS:
            raise TransactionValidationError(
                f"Transaction plan exceeds the {_MAX_ACTIONS}-action limit"
            )

        virtual_exists: dict[str, bool] = {}
        for index, action in enumerate(actions):
            if isinstance(action, WriteFile):
                path = self._normalize(action.path)
                exists = self._virtual_exists(path, virtual_exists)
                if not exists:
                    raise TransactionValidationError(
                        f"actions[{index}]: write target does not exist: {path}"
                    )
                self._assert_regular_if_present(path)
            elif isinstance(action, CreateFile):
                path = self._normalize(action.path)
                if self._virtual_exists(path, virtual_exists):
                    raise TransactionValidationError(
                        f"actions[{index}]: create target already exists: {path}"
                    )
                if action.mode < 0 or action.mode > 0o777:
                    raise TransactionValidationError(
                        f"actions[{index}]: create mode must be between 0 and 0o777"
                    )
                virtual_exists[path] = True
            elif isinstance(action, DeleteFile):
                path = self._normalize(action.path)
                if not self._virtual_exists(path, virtual_exists):
                    raise TransactionValidationError(
                        f"actions[{index}]: delete target does not exist: {path}"
                    )
                self._assert_regular_if_present(path)
                virtual_exists[path] = False
            elif isinstance(action, MoveFile):
                source = self._normalize(action.source)
                destination = self._normalize(action.destination)
                if source == destination:
                    raise TransactionValidationError(
                        f"actions[{index}]: move source and destination must differ"
                    )
                if not self._virtual_exists(source, virtual_exists):
                    raise TransactionValidationError(
                        f"actions[{index}]: move source does not exist: {source}"
                    )
                if self._virtual_exists(destination, virtual_exists):
                    raise TransactionValidationError(
                        f"actions[{index}]: move destination already exists: {destination}"
                    )
                self._assert_regular_if_present(source)
                virtual_exists[source] = False
                virtual_exists[destination] = True
            else:
                raise TransactionValidationError(
                    f"actions[{index}]: unsupported transaction action {type(action).__name__}"
                )
        return actions

    def _normalize(self, raw: str) -> str:
        normalized = raw.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise TransactionValidationError(f"Unsafe transaction path: {raw!r}")
        candidate = (self.root / Path(*path.parts)).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise TransactionValidationError(
                f"Transaction path escapes workspace root: {raw!r}"
            ) from exc
        current = self.root
        for part in path.parts:
            current /= part
            if current.is_symlink():
                raise TransactionValidationError(
                    f"Transaction paths cannot traverse symlinks: {raw!r}"
                )
        return path.as_posix()

    def _absolute(self, relative: str) -> Path:
        return self.root / Path(*PurePosixPath(relative).parts)

    def _virtual_exists(self, relative: str, state: dict[str, bool]) -> bool:
        if relative not in state:
            state[relative] = self._absolute(relative).exists()
        return state[relative]

    def _assert_regular_if_present(self, relative: str) -> None:
        path = self._absolute(relative)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise TransactionValidationError(
                f"Transaction target must be a regular file: {relative}"
            )

    def _prepare(
        self,
        tx_dir: Path,
        transaction_id: str,
        actions: tuple[TransactionAction, ...],
    ) -> dict[str, object]:
        (tx_dir / "staged").mkdir(parents=True)
        (tx_dir / "backups").mkdir()
        (tx_dir / "trash").mkdir()
        root_device = self.root.stat().st_dev
        stage_device = tx_dir.stat().st_dev
        self.last_stage_device = stage_device
        if root_device != stage_device:
            raise TransactionValidationError(
                "Transaction journal must be on the workspace filesystem"
            )

        serialized: list[dict[str, object]] = []
        for index, action in enumerate(actions):
            entry = self._serialize_action(index, action)
            if isinstance(action, (WriteFile, CreateFile)):
                stage = tx_dir / str(entry["stage"])
                stage.parent.mkdir(parents=True, exist_ok=True)
                mode = action.mode if isinstance(action, CreateFile) else 0o600
                self._write_staged(stage, action.data, mode)
                self._checkpoint(f"after_stage_fsync:{index}")
            serialized.append(entry)

        manifest: dict[str, object] = {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "workspace_key": self._journal_root.name,
            "phase": "prepared",
            "actions": serialized,
        }
        self._write_manifest(tx_dir, manifest)
        return manifest

    def _serialize_action(
        self,
        index: int,
        action: TransactionAction,
    ) -> dict[str, object]:
        if isinstance(action, WriteFile):
            path = self._normalize(action.path)
            return {
                "kind": "write",
                "path": path,
                "stage": f"staged/{index:04d}",
                "backup": f"backups/{index:04d}",
                "preserve_mode": action.preserve_mode,
                "created_parents": self._missing_parents(path),
            }
        if isinstance(action, CreateFile):
            path = self._normalize(action.path)
            return {
                "kind": "create",
                "path": path,
                "stage": f"staged/{index:04d}",
                "mode": action.mode,
                "created_parents": self._missing_parents(path),
            }
        if isinstance(action, DeleteFile):
            path = self._normalize(action.path)
            return {
                "kind": "delete",
                "path": path,
                "trash": f"trash/{index:04d}",
                "created_parents": [],
            }
        source = self._normalize(action.source)
        destination = self._normalize(action.destination)
        return {
            "kind": "move",
            "source": source,
            "destination": destination,
            "created_parents": self._missing_parents(destination),
        }

    def _missing_parents(self, relative: str) -> list[str]:
        candidate = self._absolute(relative).parent
        missing: list[str] = []
        while candidate != self.root and not candidate.exists():
            missing.append(candidate.relative_to(self.root).as_posix())
            candidate = candidate.parent
        return list(reversed(missing))

    def _write_staged(self, path: Path, data: bytes, mode: int) -> None:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
        self._fsync_file(path)
        self._fsync_dir(path.parent)

    def _write_manifest(self, tx_dir: Path, manifest: dict[str, object]) -> None:
        temporary = tx_dir / "manifest.tmp"
        encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, tx_dir / "manifest.json")
        self._fsync_dir(tx_dir)

    def _read_manifest(self, path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TransactionRecoveryError(
                f"Cannot read transaction manifest {path.parent.name}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise TransactionRecoveryError("Transaction manifest must be a JSON object")
        if payload.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
            raise TransactionRecoveryError("Unsupported transaction manifest schema")
        if payload.get("workspace_key") != self._journal_root.name:
            raise TransactionRecoveryError("Transaction manifest belongs to another workspace")
        actions = payload.get("actions")
        if not isinstance(actions, list):
            raise TransactionRecoveryError("Transaction manifest actions must be an array")
        return payload

    def _apply_action(
        self,
        tx_dir: Path,
        index: int,
        action: TransactionAction,
    ) -> None:
        if isinstance(action, WriteFile):
            target = self._absolute(self._normalize(action.path))
            stage = tx_dir / "staged" / f"{index:04d}"
            backup = tx_dir / "backups" / f"{index:04d}"
            self._ensure_parent(target)
            if action.preserve_mode:
                os.chmod(stage, stat.S_IMODE(target.stat().st_mode))
                self._fsync_file(stage)
            os.replace(target, backup)
            self._fsync_dirs((target.parent, backup.parent))
            self._checkpoint(f"after_backup:{index}")
            os.replace(stage, target)
            self._fsync_dir(target.parent)
            return
        if isinstance(action, CreateFile):
            target = self._absolute(self._normalize(action.path))
            stage = tx_dir / "staged" / f"{index:04d}"
            self._ensure_parent(target)
            os.replace(stage, target)
            self._fsync_dir(target.parent)
            return
        if isinstance(action, DeleteFile):
            target = self._absolute(self._normalize(action.path))
            trash = tx_dir / "trash" / f"{index:04d}"
            os.replace(target, trash)
            self._fsync_dirs((target.parent, trash.parent))
            return
        source = self._absolute(self._normalize(action.source))
        destination = self._absolute(self._normalize(action.destination))
        self._ensure_parent(destination)
        os.replace(source, destination)
        self._fsync_dirs((source.parent, destination.parent))

    def _rollback(self, tx_dir: Path, manifest: dict[str, object]) -> None:
        raw_actions = manifest.get("actions")
        if not isinstance(raw_actions, list):
            raise TransactionRecoveryError("Transaction manifest actions are invalid")
        for index in range(len(raw_actions) - 1, -1, -1):
            raw = raw_actions[index]
            if not isinstance(raw, dict):
                raise TransactionRecoveryError("Transaction action journal is invalid")
            self._checkpoint(f"before_rollback:{index}")
            self._rollback_action(tx_dir, raw)
            self._checkpoint(f"after_rollback:{index}")
        parents = {
            parent
            for raw in raw_actions
            if isinstance(raw, dict)
            for parent in raw.get("created_parents", [])
            if isinstance(parent, str)
        }
        for relative in sorted(parents, key=lambda item: item.count("/"), reverse=True):
            path = self._absolute(relative)
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                if path.exists() and any(path.iterdir()):
                    continue
                raise
        self._purge_transaction(tx_dir)

    def _rollback_action(self, tx_dir: Path, raw: dict[str, object]) -> None:
        kind = raw.get("kind")
        if kind == "write":
            target = self._absolute(self._required_string(raw, "path"))
            backup = tx_dir / self._required_string(raw, "backup")
            if backup.exists():
                self._unlink_file_if_present(target)
                self._ensure_parent(target)
                os.replace(backup, target)
                self._fsync_dir(target.parent)
            return
        if kind == "create":
            target = self._absolute(self._required_string(raw, "path"))
            stage = tx_dir / self._required_string(raw, "stage")
            if not stage.exists():
                self._unlink_file_if_present(target)
                self._fsync_dir(target.parent)
            return
        if kind == "delete":
            target = self._absolute(self._required_string(raw, "path"))
            trash = tx_dir / self._required_string(raw, "trash")
            if trash.exists():
                self._unlink_file_if_present(target)
                self._ensure_parent(target)
                os.replace(trash, target)
                self._fsync_dir(target.parent)
            return
        if kind == "move":
            source = self._absolute(self._required_string(raw, "source"))
            destination = self._absolute(self._required_string(raw, "destination"))
            if destination.exists() and not source.exists():
                self._ensure_parent(source)
                os.replace(destination, source)
                self._fsync_dirs((source.parent, destination.parent))
            return
        raise TransactionRecoveryError(f"Unknown transaction action kind: {kind!r}")

    def _required_string(self, raw: dict[str, object], key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str):
            raise TransactionRecoveryError(f"Transaction action is missing {key}")
        return value

    def _action_paths(self, action: TransactionAction) -> tuple[str, ...]:
        if isinstance(action, MoveFile):
            return (self._normalize(action.source), self._normalize(action.destination))
        return (self._normalize(action.path),)

    def _ensure_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync_dir(path.parent)

    def _unlink_file_if_present(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
        raise TransactionRecoveryError(f"Rollback target is not a regular file: {path.name}")

    def _remove_unprepared(self, tx_dir: Path) -> None:
        if tx_dir.exists():
            shutil.rmtree(tx_dir)
            self._cleanup_empty_journal_roots()

    def _purge_transaction(self, tx_dir: Path) -> None:
        self._checkpoint("before_purge")
        shutil.rmtree(tx_dir)
        self._fsync_dir(tx_dir.parent)
        self._cleanup_empty_journal_roots()

    def _cleanup_empty_journal_roots(self) -> None:
        try:
            self._journal_root.rmdir()
        except OSError:
            return
        parent = self._journal_root.parent
        with contextlib.suppress(OSError):
            parent.rmdir()

    def _fsync_file(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_dir(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_dirs(self, paths: Iterable[Path]) -> None:
        seen: set[Path] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            self._fsync_dir(path)


class JournaledFileTransactionFactory:
    """Create one journaled transaction engine for a reviewed workspace root."""

    def create(self, workspace_root: Path) -> JournaledFileTransaction:
        return JournaledFileTransaction(workspace_root)
