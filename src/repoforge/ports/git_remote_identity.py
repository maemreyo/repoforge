"""Ports for repository-local Git remote and SSH identity evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.git_remote_identity import (
    ParsedGitRemote,
    ReviewedSshEndpoint,
    SshAliasDefinition,
    SshKeyProof,
    SshPrincipalProof,
)


@dataclass(frozen=True, slots=True)
class EffectiveUserPaths:
    home: Path
    ssh_config: Path

    def __post_init__(self) -> None:
        if not self.home.is_absolute() or not self.ssh_config.is_absolute():
            raise ValueError("effective-user paths must be absolute")


class GitRemoteParser(Protocol):
    def parse(self, remote_url: str) -> ParsedGitRemote: ...


class SshAliasResolver(Protocol):
    def resolve(self, alias: str) -> SshAliasDefinition: ...


class SshKeyInspector(Protocol):
    def inspect(self, identity_file: str, *, observed_at: str) -> SshKeyProof: ...


class SshIdentityMaterial(Protocol):
    @property
    def path(self) -> Path: ...

    def close(self) -> None: ...

    def __enter__(self) -> SshIdentityMaterial: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class SshIdentityMaterialProvider(Protocol):
    def open_verified(self, expected: SshKeyProof) -> SshIdentityMaterial: ...


class SshPrincipalVerifier(Protocol):
    def verify(
        self,
        *,
        provider_host: str,
        expected_login: str,
        expected_actor_id: str,
        key: SshKeyProof,
        observed_at: str,
    ) -> SshPrincipalProof: ...


class GitTransportEndpointRevalidator(Protocol):
    def revalidate(
        self,
        *,
        cwd: Path,
        raw_remote_url: str,
        expected: ReviewedSshEndpoint,
    ) -> ReviewedSshEndpoint: ...


__all__ = [
    "EffectiveUserPaths",
    "GitRemoteParser",
    "GitTransportEndpointRevalidator",
    "SshAliasResolver",
    "SshIdentityMaterial",
    "SshIdentityMaterialProvider",
    "SshKeyInspector",
    "SshPrincipalVerifier",
]
