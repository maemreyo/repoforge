"""Capability-gated MCP progress reporter for ``operation wait``.

Bridges the application-layer :class:`ProgressReporter` seam to MCP
``notifications/progress``. A reporter is only ``enabled`` when the connected
client advertised progress support *and* supplied a progress token on the
request -- otherwise the wait falls back to poll guidance. ``emit`` is injected
so the gating logic is testable without a live session.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from ...domain.client_capabilities import ClientCapabilities, ClientFeature


class McpProgressReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        emit: Callable[[int, int | None, str | None], None],
    ) -> None:
        self._enabled = enabled
        self._emit = emit

    @property
    def enabled(self) -> bool:
        return self._enabled

    def report(self, *, current: int, total: int | None, message: str | None) -> None:
        if not self._enabled:
            return
        with contextlib.suppress(Exception):
            self._emit(current, total, message)


def build_progress_reporter(
    *,
    capabilities: ClientCapabilities,
    has_progress_token: bool,
    emit: Callable[[int, int | None, str | None], None],
) -> McpProgressReporter:
    enabled = has_progress_token and capabilities.supports(ClientFeature.PROGRESS_NOTIFICATIONS)
    return McpProgressReporter(enabled=enabled, emit=emit)
