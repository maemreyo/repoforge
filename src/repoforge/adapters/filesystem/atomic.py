"""One secure atomic writer, shared by every adapter that persists sensitive state.

Two independent copies of "write a private file safely" drifted apart once already: the
configuration store fsynced the parent directory after ``os.replace`` while the release
store did not, so a durable credential could be reported as written and then vanish in a
power loss because the *rename* was never flushed. The rules are all here, once:

* create the temporary file with its FINAL mode, so the content is never world-readable
  even transiently (``open(..., "w")`` would create ``0666 & ~umask``, commonly 0644, and
  would STAY 0644 if the process died before ``chmod``);
* exclusive create, so an attacker-planted temporary path is never written through;
* fsync the file before the rename, so the rename can never expose a partial file;
* fsync the parent directory after the rename, so the rename itself is durable.
"""

from __future__ import annotations

import os
from pathlib import Path


def fsync_dir(path: Path) -> None:
    """Flush a directory entry so a rename into it survives a power loss.

    Best-effort by design: some filesystems refuse ``O_RDONLY`` on a directory or reject
    ``fsync`` on a directory descriptor, and failing the whole write there would be worse
    than accepting the weaker durability the platform offers.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    dir_mode: int = 0o700,
) -> None:
    """Write ``data`` to ``path`` atomically and durably, never widening ``mode``."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int = 0o600,
    dir_mode: int = 0o700,
) -> None:
    """UTF-8 convenience wrapper over :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode, dir_mode=dir_mode)
