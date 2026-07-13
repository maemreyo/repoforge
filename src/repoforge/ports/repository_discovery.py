"""Bounded local repository discovery boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.onboarding import DiscoveryIdentity


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    roots: tuple[Path, ...]
    max_depth: int
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    managed_workspace_roots: tuple[Path, ...]


class RepositoryDiscovery(Protocol):
    def discover(self, request: DiscoveryRequest) -> tuple[DiscoveryIdentity, ...]: ...
