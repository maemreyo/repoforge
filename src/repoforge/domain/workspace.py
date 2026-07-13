"""Workspace domain records, independent of persistence and locking adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VerificationReceipt:
    profile: str
    fingerprint: str
    completed_at: str
    commands: list[dict[str, Any]]


@dataclass
class WorkspaceRecord:
    workspace_id: str
    repo_id: str
    path: str
    branch: str
    base: str
    remote: str
    created_at: str
    last_verification: VerificationReceipt | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
