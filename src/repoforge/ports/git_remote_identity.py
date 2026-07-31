"""Ports for repository-local Git remote and SSH identity evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.git_remote_identity import ParsedGitRemote, SshAliasDefinition


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


__all__ = ["EffectiveUserPaths", "GitRemoteParser", "SshAliasResolver"]
