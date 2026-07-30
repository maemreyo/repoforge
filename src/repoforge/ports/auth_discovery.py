"""Read-only discovery boundaries for already-configured repository identities.

These protocols return safe candidates only. There is deliberately no method for switching
the active GitHub account, writing SSH configuration, or mutating Git configuration -- an
implementation that needed one would not satisfy the protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.auth_migration import NamedAccountCandidate, SshAliasCandidate


class NamedAccountDiscovery(Protocol):
    def candidates(self, *, host: str) -> tuple[NamedAccountCandidate, ...]: ...

    def verify(self, *, host: str, login: str) -> NamedAccountCandidate: ...


class SshAliasDiscovery(Protocol):
    def inspect(self, alias: str) -> SshAliasCandidate: ...


class AmbientAuthConflictReader(Protocol):
    """Bounded read-only view of the ambient state a migration must report, not adopt."""

    def environment_names(self) -> tuple[str, ...]: ...

    def git_config_values(self, cwd: Path, key: str) -> tuple[tuple[str, str], ...]: ...
