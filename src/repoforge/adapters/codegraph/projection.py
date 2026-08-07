"""Policy-filtered, snapshot-bound source projection for managed CodeGraph."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from ...domain.code_intelligence import CodeIntelligenceRequest
from ...domain.codegraph_config import CodeGraphOptions
from ...domain.errors import ConfigError
from ...ports.locking import LockManager
from ..filesystem.atomic import atomic_write_bytes, atomic_write_text, fsync_dir
from .manifest import ProjectionEntry, ProjectionManifest, ProjectionResult

_SUPPORTED_SUFFIXES = frozenset({".cjs", ".js", ".jsx", ".mjs", ".py", ".pyi", ".ts", ".tsx"})
_INDEX_DIR = ".index"
_MANIFEST = "projection.json"
_INCOMPLETE = "INCOMPLETE"

FaultInjector = Callable[[str, str | None], object]


class _ProjectionReadError(Exception):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class CodeGraphProjection:
    def __init__(
        self,
        state_root: Path,
        locks: LockManager,
        *,
        lock_timeout_seconds: float = 120.0,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        managed_root = state_root.expanduser().resolve()
        managed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for component in ("providers", "codegraph", "workspaces"):
            managed_root = managed_root / component
            self._ensure_directory(managed_root)
        self._root = managed_root
        self._locks = locks
        self._lock_timeout_seconds = lock_timeout_seconds
        self._fault_injector = fault_injector

    def operation(
        self,
        workspace_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AbstractContextManager[None]:
        self.workspace_root(workspace_id)
        timeout = self._lock_timeout_seconds if timeout_seconds is None else timeout_seconds
        return self._locks.lock(
            f"codegraph-operation-{workspace_id}",
            timeout_seconds=timeout,
            metadata={"provider": "codegraph", "workspace_id": workspace_id},
        )

    def workspace_root(self, workspace_id: str) -> Path:
        if not workspace_id or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
            for character in workspace_id
        ):
            raise ValueError("workspace_id has an invalid format")
        return self._root / workspace_id

    def prepare(
        self,
        request: CodeIntelligenceRequest,
        options: CodeGraphOptions,
    ) -> ProjectionResult:
        if not isinstance(request, CodeIntelligenceRequest):
            raise TypeError("request must use CodeIntelligenceRequest")
        if not isinstance(options, CodeGraphOptions):
            raise TypeError("options must use CodeGraphOptions")
        workspace_state = self.workspace_root(request.snapshot.workspace_id)
        lock_name = f"codegraph-projection-{request.snapshot.workspace_id}"
        with self._locks.lock(
            lock_name,
            timeout_seconds=self._lock_timeout_seconds,
            metadata={"snapshot_id": request.snapshot.snapshot_id},
        ):
            return self._prepare_locked(workspace_state, request, options)

    def invalidate(self, workspace_id: str) -> None:
        workspace_state = self.workspace_root(workspace_id)
        lock_name = f"codegraph-projection-{workspace_id}"
        with self._locks.lock(lock_name, timeout_seconds=self._lock_timeout_seconds):
            self._ensure_directory(workspace_state)
            atomic_write_text(workspace_state / _INCOMPLETE, "invalidated\n")
            (workspace_state / _MANIFEST).unlink(missing_ok=True)
            source_root = workspace_state / "source"
            self._ensure_directory(source_root)
            self._remove_managed(source_root / _INDEX_DIR)
            fsync_dir(workspace_state)

    def mark_complete(self, workspace_id: str, manifest_digest: str) -> None:
        workspace_state = self.workspace_root(workspace_id)
        lock_name = f"codegraph-projection-{workspace_id}"
        with self._locks.lock(lock_name, timeout_seconds=self._lock_timeout_seconds):
            self._ensure_directory(workspace_state)
            try:
                manifest_bytes = self._secure_read(workspace_state, _MANIFEST, 2_000_000)
            except _ProjectionReadError as exc:
                raise ValueError("projection manifest is unavailable for completion") from exc
            try:
                manifest = ProjectionManifest.from_json(manifest_bytes.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError("projection manifest is not UTF-8") from exc
            if manifest.workspace_id != workspace_id:
                raise ValueError("projection manifest workspace identity does not match")
            if manifest.manifest_digest != manifest_digest:
                raise ValueError("projection manifest digest does not match completion request")
            marker = workspace_state / _INCOMPLETE
            if marker.is_symlink():
                raise ValueError("projection incomplete marker must not be a symlink")
            marker.unlink(missing_ok=True)
            fsync_dir(workspace_state)

    def read_manifest(self, workspace_id: str) -> ProjectionManifest | None:
        workspace_state = self.workspace_root(workspace_id)
        with self._locks.lock(
            f"codegraph-projection-{workspace_id}",
            timeout_seconds=self._lock_timeout_seconds,
        ):
            try:
                payload = self._secure_read(workspace_state, _MANIFEST, 2_000_000)
                return ProjectionManifest.from_json(payload.decode("utf-8"))
            except (_ProjectionReadError, UnicodeError, ValueError):
                return None

    def dispose_workspace(self, workspace_id: str) -> None:
        workspace_state = self.workspace_root(workspace_id)
        with (
            self.operation(workspace_id),
            self._locks.lock(
                f"codegraph-projection-{workspace_id}",
                timeout_seconds=self._lock_timeout_seconds,
            ),
        ):
            self._remove_managed(workspace_state)
            fsync_dir(self._root)

    def cleanup_workspaces(
        self,
        active_workspace_ids: frozenset[str],
        *,
        limit: int,
    ) -> tuple[int, int, int]:
        if not isinstance(active_workspace_ids, frozenset) or any(
            not isinstance(workspace_id, str) or not workspace_id
            for workspace_id in active_workspace_ids
        ):
            raise ValueError("active_workspace_ids must be normalized text identities")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10_000:
            raise ValueError("cleanup limit must be an integer between 1 and 10000")
        removed = 0
        skipped = 0
        incomplete = 0
        for candidate in sorted(self._root.iterdir(), key=lambda path: path.name)[:limit]:
            workspace_id = candidate.name
            try:
                self.workspace_root(workspace_id)
                with (
                    self.operation(
                        workspace_id,
                        timeout_seconds=0,
                    ),
                    self._locks.lock(
                        f"codegraph-projection-{workspace_id}",
                        timeout_seconds=0,
                    ),
                ):
                    invalid = candidate.is_symlink() or not candidate.is_dir()
                    marker = candidate / _INCOMPLETE
                    is_incomplete = invalid or marker.is_symlink() or marker.exists()
                    if is_incomplete or workspace_id not in active_workspace_ids:
                        self._remove_managed(candidate)
                        removed += 1
                        incomplete += int(is_incomplete)
                        fsync_dir(self._root)
                    else:
                        skipped += 1
            except (ConfigError, ValueError):
                skipped += 1
        return removed, skipped, incomplete

    def _prepare_locked(
        self,
        workspace_state: Path,
        request: CodeIntelligenceRequest,
        options: CodeGraphOptions,
    ) -> ProjectionResult:
        source_root = workspace_state / "source"
        self._ensure_directory(workspace_state)
        atomic_write_text(workspace_state / _INCOMPLETE, request.snapshot.snapshot_id + "\n")
        (workspace_state / _MANIFEST).unlink(missing_ok=True)
        self._inject("after_incomplete", None)
        try:
            self._ensure_directory(source_root)
            self._ensure_directory(workspace_state / "home")
            self._clear_source(source_root)
            entries: list[ProjectionEntry] = []
            total_bytes = 0
            categories: set[str] = set()
            truncated = False
            denied_paths = set(request.denied_paths)
            if request.denied_paths:
                categories.add("denied")
            for relative_path in request.paths:
                if relative_path in denied_paths:
                    categories.add("denied")
                    continue
                if self._reserved(relative_path):
                    categories.add("denied")
                    continue
                if Path(relative_path).suffix.lower() not in _SUPPORTED_SUFFIXES:
                    categories.add("unsupported")
                    continue
                if len(entries) >= options.projection_max_files:
                    categories.add("budget")
                    truncated = True
                    continue
                remaining = options.projection_max_bytes - total_bytes
                if remaining <= 0:
                    categories.add("budget")
                    truncated = True
                    continue
                try:
                    data = self._secure_read(request.workspace_root, relative_path, remaining)
                except _ProjectionReadError as exc:
                    categories.add(exc.category)
                    if exc.category == "budget":
                        truncated = True
                    continue
                self._inject("before_materialize", relative_path)
                destination = source_root / relative_path
                atomic_write_bytes(destination, data)
                entries.append(
                    ProjectionEntry(
                        path=relative_path,
                        sha256=hashlib.sha256(data).hexdigest(),
                        size_bytes=len(data),
                    )
                )
                total_bytes += len(data)
                self._inject("after_materialize", relative_path)
            fsync_dir(source_root)
            manifest = ProjectionManifest.from_snapshot(
                request.snapshot,
                options_digest=options.options_digest,
                selection_digest=self._selection_digest(request),
                entries=tuple(entries),
                limitations=self._limitations(categories),
                truncated=truncated,
            )
            self._inject("before_manifest", None)
            atomic_write_text(workspace_state / _MANIFEST, manifest.to_json())
            fsync_dir(workspace_state)
            return ProjectionResult(source_root=source_root, manifest=manifest)
        except Exception:
            (workspace_state / _MANIFEST).unlink(missing_ok=True)
            if not (workspace_state / _INCOMPLETE).exists():
                atomic_write_text(workspace_state / _INCOMPLETE, "failed\n")
            fsync_dir(workspace_state)
            raise

    def _inject(self, step: str, path: str | None) -> None:
        if self._fault_injector is not None:
            self._fault_injector(step, path)

    @staticmethod
    def _reserved(relative_path: str) -> bool:
        first = relative_path.split("/", 1)[0]
        return first in {".git", _INDEX_DIR}

    @staticmethod
    def _selection_digest(request: CodeIntelligenceRequest) -> str:
        payload = {
            "denied_paths": list(request.denied_paths),
            "paths": list(request.paths),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _limitations(categories: set[str]) -> tuple[str, ...]:
        messages = {
            "budget": "Projection file or byte budgets truncated supported source files.",
            "denied": "Repository policy denied paths before projection materialization.",
            "missing": "Missing or concurrently changed source files were omitted.",
            "nonregular": "Non-regular source files were omitted from the projection.",
            "symlink": "Symlinked source paths were omitted from the projection.",
            "unreadable": "Unreadable source files were omitted from the projection.",
            "unsupported": "Unsupported-language files were omitted from the projection.",
        }
        return tuple(messages[category] for category in sorted(categories))

    @staticmethod
    def _clear_source(source_root: Path) -> None:
        for entry in source_root.iterdir():
            if entry.name == _INDEX_DIR:
                if entry.is_symlink() or not entry.is_dir():
                    CodeGraphProjection._remove_managed(entry)
                continue
            CodeGraphProjection._remove_managed(entry)
        fsync_dir(source_root)

    @staticmethod
    def _remove_managed(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
            return
        shutil.rmtree(path)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            return
        if stat.S_ISLNK(metadata.st_mode):
            path.unlink()
            path.mkdir(mode=0o700)
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"managed CodeGraph path is not a directory: {path.name}")

    @staticmethod
    def _secure_read(root: Path, relative_path: str, max_bytes: int) -> bytes:
        flags_directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            current = os.open(root, flags_directory)
            descriptors.append(current)
            parts = relative_path.split("/")
            for part in parts[:-1]:
                try:
                    current = os.open(part, flags_directory, dir_fd=current)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise _ProjectionReadError("symlink") from exc
                    if exc.errno == errno.ENOENT:
                        raise _ProjectionReadError("missing") from exc
                    raise _ProjectionReadError("unreadable") from exc
                descriptors.append(current)
            try:
                file_descriptor = os.open(parts[-1], flags_file, dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise _ProjectionReadError("symlink") from exc
                if exc.errno == errno.ENOENT:
                    raise _ProjectionReadError("missing") from exc
                raise _ProjectionReadError("unreadable") from exc
            descriptors.append(file_descriptor)
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise _ProjectionReadError("nonregular")
            if before.st_size > max_bytes:
                raise _ProjectionReadError("budget")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(file_descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(file_descriptor)
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity_before != identity_after or len(data) != before.st_size:
                raise _ProjectionReadError("missing")
            if len(data) > max_bytes:
                raise _ProjectionReadError("budget")
            return data
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


__all__ = ["CodeGraphProjection", "FaultInjector"]
