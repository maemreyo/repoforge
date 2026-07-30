"""Capability-gated MCP progress reporter for ``operation wait``.

Bridges the application-layer :class:`ProgressReporter` seam to MCP
``notifications/progress``. A reporter is only ``enabled`` when the connected
client advertised progress support *and* supplied a progress token on the
request -- otherwise the wait falls back to poll guidance. ``emit`` is injected
so the gating logic is testable without a live session.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable

from ...domain.client_capabilities import ClientCapabilities, ClientFeature

MIN_PROGRESS_INTERVAL_S = 0.5
"""Floor between two pushed notifications.

The wait loop already emits only on a durable `updated_at` change, but nothing bounds how
fast a worker writes those. A sharded profile reporting per step start *and* completion can
burst, and every notification is a transport write on a request that is being held open, so
the frequency needs a ceiling that does not depend on worker behaviour. Dropping an
intermediate update is safe: the wait's own return value carries the authoritative state.
"""


class McpProgressReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        emit: Callable[[int, int | None, str | None], None],
        min_interval_s: float = MIN_PROGRESS_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._emit = emit
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._last_emit_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def report(self, *, current: int, total: int | None, message: str | None) -> None:
        if not self._enabled:
            return
        now = self._clock()
        # The first update always goes through, so a short wait still reports once.
        if self._last_emit_at is not None and now - self._last_emit_at < self._min_interval_s:
            return
        self._last_emit_at = now
        with contextlib.suppress(Exception):
            self._emit(current, total, message)


def build_progress_reporter(
    *,
    capabilities: ClientCapabilities,
    has_progress_token: bool,
    emit: Callable[[int, int | None, str | None], None],
    min_interval_s: float = MIN_PROGRESS_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
) -> McpProgressReporter:
    enabled = has_progress_token and capabilities.supports(ClientFeature.PROGRESS_NOTIFICATIONS)
    return McpProgressReporter(
        enabled=enabled,
        emit=emit,
        min_interval_s=min_interval_s,
        clock=clock,
    )
