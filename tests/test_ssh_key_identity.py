"""No-follow SSH private-key proof and operation-scoped materialization."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.git.ssh_key_identity import FileSshKeyIdentity
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.ports.command import CommandResult

NOW = "2026-08-01T00:00:00+00:00"
KEY_A = "PRIVATE KEY CANARY A\n"
KEY_B = "PRIVATE KEY CANARY B\n"


class FingerprintExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.before_read: object | None = None

    def environment(self) -> dict[str, str]:
        return {
            "PATH": "/safe/bin",
            "GH_TOKEN": "must-not-propagate",
            "SSH_AUTH_SOCK": "/tmp/must-not-propagate",
        }

    def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"argv": tuple(argv), **kwargs})
        callback = self.before_read
        if callable(callback):
            callback()
            self.before_read = None
        body = Path(argv[-1]).read_bytes()
        digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii").rstrip("=")
        return CommandResult(
            tuple(argv), str(kwargs["cwd"]), 0, f"2048 SHA256:{digest} fixture (RSA)\n", ""
        )


def _write(path: Path, body: str = KEY_A, mode: int = 0o600) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def _identity(tmp_path: Path) -> tuple[FileSshKeyIdentity, FingerprintExecutor]:
    executor = FingerprintExecutor()
    material_root = tmp_path / "material"
    material_root.mkdir()
    return FileSshKeyIdentity(executor, material_root=material_root), executor


def test_replacing_the_key_at_the_same_path_fails_open_verified(tmp_path: Path) -> None:
    path = _write(tmp_path / "id_work")
    identity, _ = _identity(tmp_path)
    proof = identity.inspect(str(path), observed_at=NOW)

    path.write_text(KEY_B, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(RepoForgeError) as failure:
        identity.open_verified(proof)
    assert failure.value.code is ErrorCode.GIT_TRANSPORT_KEY_MISMATCH


def test_fingerprint_uses_in_file_flag_for_portable_argv(tmp_path: Path) -> None:
    path = _write(tmp_path / "id_work")
    identity, executor = _identity(tmp_path)
    identity.inspect(str(path), observed_at=NOW)

    fingerprint_call = next(call for call in executor.calls if call["argv"][0] == "ssh-keygen")
    argv = fingerprint_call["argv"]
    assert argv[:5] == ("ssh-keygen", "-l", "-E", "sha256", "-f")
    assert Path(argv[5]).is_absolute()
    assert "-lf" not in argv


def test_materialization_copies_from_verified_descriptor_and_cleans_up(tmp_path: Path) -> None:
    path = _write(tmp_path / "id_work")
    identity, executor = _identity(tmp_path)
    proof = identity.inspect(str(path), observed_at=NOW)

    material = identity.open_verified(proof)
    material_path = material.path
    try:
        assert material_path != path
        assert material_path.read_text(encoding="utf-8") == KEY_A
        assert material_path.stat().st_mode & 0o777 == 0o600
        assert all(KEY_A not in repr(call) for call in executor.calls)
    finally:
        material.close()

    assert not material_path.exists()


def test_original_path_replacement_after_open_cannot_change_materialized_bytes(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path / "id_work")
    identity, executor = _identity(tmp_path)
    proof = identity.inspect(str(path), observed_at=NOW)
    executor.before_read = lambda: _write(path, KEY_B)

    material = identity.open_verified(proof)
    try:
        assert material.path.read_text(encoding="utf-8") == KEY_A
        assert path.read_text(encoding="utf-8") == KEY_B
    finally:
        material.close()


def test_inspection_rejects_a_key_reached_through_a_symlinked_parent(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    key = _write(real_directory / "id_work")
    alias_directory = tmp_path / "alias-directory"
    alias_directory.symlink_to(real_directory, target_is_directory=True)
    identity, _ = _identity(tmp_path)

    with pytest.raises(RepoForgeError) as failure:
        identity.inspect(str(alias_directory / key.name), observed_at=NOW)

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_KEY_UNSAFE


@pytest.mark.parametrize("unsafe_mode", [0o644, 0o660, 0o700])
def test_inspection_rejects_symlink_and_unsafe_key_modes(
    tmp_path: Path,
    unsafe_mode: int,
) -> None:
    target = _write(tmp_path / "target")
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    identity, _ = _identity(tmp_path)

    with pytest.raises(RepoForgeError):
        identity.inspect(str(alias), observed_at=NOW)

    target.chmod(unsafe_mode)
    with pytest.raises(RepoForgeError):
        identity.inspect(str(target), observed_at=NOW)
