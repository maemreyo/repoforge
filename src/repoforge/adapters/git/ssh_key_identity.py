"""No-follow SSH key inspection and operation-scoped materialization."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.git_remote_identity import SshKeyProof
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult

_MAX_KEY_BYTES = 1_000_000
_FINGERPRINT = re.compile(r"^\d+\s+(SHA256:[A-Za-z0-9+/]{43})(?:\s|$)")


class _FingerprintExecutor(Protocol):
    def run_isolated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult: ...


def _key_error(
    message: str,
    *,
    code: ErrorCode = ErrorCode.GIT_TRANSPORT_KEY_MISMATCH,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=(
            "No Git or SSH network command was executed.",
            "No operator key file was modified.",
        ),
        safe_next_action="Re-inspect and migrate the repository SSH identity before retrying.",
    )


def _unsafe_key_error(message: str) -> RepoForgeError:
    return _key_error(message, code=ErrorCode.GIT_TRANSPORT_KEY_UNSAFE)


@dataclass(frozen=True, slots=True)
class _OpenedKey:
    descriptor: int
    canonical_path: Path
    owner_uid: int
    mode: int


class FileSshIdentityMaterial:
    """One temporary private-key copy whose lifetime is explicitly bounded."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(FileNotFoundError):
            self._path.unlink()

    def __enter__(self) -> FileSshIdentityMaterial:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class FileSshKeyIdentity:
    """Bind an SSH private key to its public fingerprint without trusting its path alone."""

    def __init__(self, executor: _FingerprintExecutor, *, material_root: Path) -> None:
        if not material_root.is_absolute():
            raise ValueError("material_root must be absolute")
        material_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = material_root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("material_root must be one real directory")
        if metadata.st_uid != os.geteuid():
            raise ValueError("material_root must be owned by the effective user")
        os.chmod(material_root, 0o700)
        self._executor = executor
        self._material_root = material_root

    def inspect(self, identity_file: str, *, observed_at: str) -> SshKeyProof:
        opened = self._open(identity_file)
        try:
            material = self._materialize(opened.descriptor)
            try:
                fingerprint = self._fingerprint(material.path)
            finally:
                material.close()
            return SshKeyProof(
                canonical_path=str(opened.canonical_path),
                public_key_fingerprint=fingerprint,
                owner_uid=opened.owner_uid,
                mode=opened.mode,
                observed_at=observed_at,
            )
        finally:
            os.close(opened.descriptor)

    def open_verified(self, expected: SshKeyProof) -> FileSshIdentityMaterial:
        opened = self._open(expected.canonical_path)
        material: FileSshIdentityMaterial | None = None
        try:
            if (
                str(opened.canonical_path) != expected.canonical_path
                or opened.owner_uid != expected.owner_uid
                or opened.mode != expected.mode
            ):
                raise _key_error("SSH key file metadata no longer matches the reviewed proof.")
            material = self._materialize(opened.descriptor)
            fingerprint = self._fingerprint(material.path)
            if fingerprint != expected.public_key_fingerprint:
                raise _key_error("SSH key fingerprint no longer matches the reviewed proof.")
            return material
        except BaseException:
            if material is not None:
                material.close()
            raise
        finally:
            os.close(opened.descriptor)

    def _open(self, identity_file: str) -> _OpenedKey:
        if (
            not isinstance(identity_file, str)
            or not identity_file.startswith("/")
            or "\x00" in identity_file
            or len(identity_file) > 4096
        ):
            raise _unsafe_key_error("SSH identity file must be one bounded absolute path.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(identity_file, flags)
        except OSError as exc:
            raise _unsafe_key_error("SSH identity file is unavailable or is a symlink.") from exc
        try:
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            if not stat.S_ISREG(metadata.st_mode):
                raise _unsafe_key_error("SSH identity file must be a regular file.")
            if metadata.st_uid != os.geteuid():
                raise _unsafe_key_error("SSH identity file must be owned by the effective user.")
            if mode not in {0o400, 0o600}:
                raise _unsafe_key_error("SSH identity file mode must be exactly 0400 or 0600.")
            if metadata.st_size <= 0 or metadata.st_size > _MAX_KEY_BYTES:
                raise _unsafe_key_error("SSH identity file exceeds the reviewed size bounds.")
            supplied = Path(identity_file)
            canonical = supplied.resolve(strict=True)
            if canonical != supplied:
                raise _unsafe_key_error(
                    "SSH identity file path cannot traverse symlinks or relative components."
                )
            canonical_metadata = os.stat(canonical, follow_symlinks=False)
            if (canonical_metadata.st_dev, canonical_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise _unsafe_key_error("SSH identity file changed while it was being inspected.")
            return _OpenedKey(descriptor, canonical, metadata.st_uid, mode)
        except BaseException:
            os.close(descriptor)
            raise

    def _materialize(self, descriptor: int) -> FileSshIdentityMaterial:
        fd, raw_path = tempfile.mkstemp(prefix="repoforge-ssh-", dir=self._material_root)
        path = Path(raw_path)
        try:
            os.fchmod(fd, 0o600)
            os.lseek(descriptor, 0, os.SEEK_SET)
            copied = 0
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > _MAX_KEY_BYTES:
                    raise _unsafe_key_error(
                        "SSH identity file exceeded its reviewed bound while copying."
                    )
                os.write(fd, chunk)
            os.fsync(fd)
        except BaseException:
            with suppress(FileNotFoundError):
                path.unlink()
            raise
        finally:
            os.close(fd)
        return FileSshIdentityMaterial(path)

    def _fingerprint(self, material_path: Path) -> str:
        result = self._executor.run_isolated(
            ["ssh-keygen", "-lf", "-E", "sha256", str(material_path)],
            cwd=self._material_root,
            environment={"PATH": os.environ.get("PATH", "")},
            secrets=(),
            check=False,
            output_limit=4096,
        )
        if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
            raise _key_error("SSH key fingerprint could not be derived safely.")
        first_line = result.stdout.strip().splitlines()
        match = _FINGERPRINT.match(first_line[0]) if len(first_line) == 1 else None
        if match is None:
            raise _key_error("SSH key fingerprint output was malformed or ambiguous.")
        return match.group(1)


__all__ = ["FileSshIdentityMaterial", "FileSshKeyIdentity"]
