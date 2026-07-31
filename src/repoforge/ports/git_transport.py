"""Operation-scoped Git transport boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.git_transport_identity import GitTransportEvidence, GitTransportSpec
from ..domain.repository_auth_broker import ProcessAuthContext
from .command import CommandResult


class GitTransportGateway(Protocol):
    def ls_remote(
        self,
        cwd: Path,
        remote_url: str,
        requested_ref: str | None,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> GitTransportEvidence: ...

    def fetch(
        self,
        cwd: Path,
        remote_url: str,
        refspec: str,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> CommandResult: ...

    def push(
        self,
        cwd: Path,
        remote_url: str,
        refspec: str,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> CommandResult: ...
