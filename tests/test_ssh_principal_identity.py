"""Bounded GitHub SSH principal proof from verified key material."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.github.ssh_principal import GitHubSshPrincipalVerifier
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_remote_identity import SshKeyProof
from repoforge.ports.command import CommandResult

_NOW = "2026-08-01T00:00:00+00:00"
_FINGERPRINT = "SHA256:" + "A" * 43


class Material:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Material:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class Materials:
    def __init__(self, material: Material) -> None:
        self.material = material
        self.keys: list[SshKeyProof] = []

    def open_verified(self, expected: SshKeyProof) -> Material:
        self.keys.append(expected)
        return self.material


class Executor:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"argv": tuple(argv), **kwargs})
        return self.result


def _key() -> SshKeyProof:
    return SshKeyProof(
        canonical_path="/home/demo/.ssh/id_rsa_work",
        public_key_fingerprint=_FINGERPRINT,
        owner_uid=501,
        mode=0o600,
        observed_at=_NOW,
    )


def test_github_ssh_principal_uses_one_verified_key_and_exact_greeting(tmp_path: Path) -> None:
    material = Material(tmp_path / "operation-key")
    materials = Materials(material)
    executor = Executor(
        CommandResult(
            ("ssh",),
            str(tmp_path),
            1,
            "",
            "Hi matw-ngo! You've successfully authenticated, but GitHub does not provide shell access.\n",
        )
    )
    verifier = GitHubSshPrincipalVerifier(
        executor,
        materials=materials,
        cwd=tmp_path,
    )

    proof = verifier.verify(
        provider_host="github.com",
        expected_login="matw-ngo",
        expected_actor_id="173029271",
        key=_key(),
        observed_at=_NOW,
    )

    assert proof.principal_login == "matw-ngo"
    assert proof.expected_actor_id == "173029271"
    assert proof.key_fingerprint == _FINGERPRINT
    assert materials.keys == [_key()]
    assert material.closed is True
    call = executor.calls[0]
    assert call["argv"] == (
        "ssh",
        "-T",
        "-F",
        "/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-i",
        str(material.path),
        "git@github.com",
    )
    assert call["secrets"] == ()
    assert call["check"] is False


def test_github_ssh_principal_rejects_a_different_account_and_cleans_material(
    tmp_path: Path,
) -> None:
    material = Material(tmp_path / "operation-key")
    verifier = GitHubSshPrincipalVerifier(
        Executor(
            CommandResult(
                ("ssh",),
                str(tmp_path),
                1,
                "",
                "Hi maemreyo! You've successfully authenticated, but GitHub does not provide shell access.\n",
            )
        ),
        materials=Materials(material),
        cwd=tmp_path,
    )

    with pytest.raises(RepoForgeError) as failure:
        verifier.verify(
            provider_host="github.com",
            expected_login="matw-ngo",
            expected_actor_id="173029271",
            key=_key(),
            observed_at=_NOW,
        )

    assert failure.value.code is ErrorCode.GIT_TRANSPORT_PRINCIPAL_MISMATCH
    assert material.closed is True
