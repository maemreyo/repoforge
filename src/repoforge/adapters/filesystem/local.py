"""Local filesystem implementation with atomic, mode-preserving writes."""

from __future__ import annotations
import os
import stat
from pathlib import Path


class LocalFileSystem:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def size(self, path: Path) -> int:
        return path.stat().st_size

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def mkdir(self, path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
        path.mkdir(parents=parents, exist_ok=exist_ok)

    def unlink(self, path: Path, *, missing_ok: bool = False) -> None:
        path.unlink(missing_ok=missing_ok)

    def write_bytes_atomic(
        self, path: Path, data: bytes, *, preserve_mode: bool = True
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = (
            stat.S_IMODE(path.stat().st_mode)
            if preserve_mode and path.exists()
            else None
        )
        temporary = path.with_name(f".{path.name}.rf-{os.getpid()}")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
