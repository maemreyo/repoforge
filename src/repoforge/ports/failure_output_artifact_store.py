"""Persistence boundary for complete, secret-safe failure output artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FailureOutputArtifact:
    reference: str | None
    status: str


class FailureOutputArtifactStore(Protocol):
    def persist(self, content: str) -> FailureOutputArtifact: ...
