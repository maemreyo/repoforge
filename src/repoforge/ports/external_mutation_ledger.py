"""Durable budget boundary for externally visible mutations."""

from __future__ import annotations

from typing import Protocol


class ExternalMutationLedger(Protocol):
    def reserve(
        self,
        repo_id: str,
        marker: str,
        *,
        count: int,
        now_epoch: float,
        max_in_window: int,
        window_seconds: int,
    ) -> int:
        """Reserve bounded capacity once per marker and return current window usage."""
        ...
