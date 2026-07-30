"""Request-local progress reporter for an open ``operation wait``.

The wait loop is a synchronous poll, so it cannot be handed an ``async``
notification sink directly. The MCP layer therefore binds a
:class:`~repoforge.ports.progress_reporter.ProgressReporter` for the duration of
one dispatch, exactly the way :mod:`repoforge.application.audit_context` binds
audit attribution, and the coordinator reads it from the ambient context.

Binding through a :class:`~contextvars.ContextVar` is what lets the wait run in a
worker thread: ``anyio.to_thread.run_sync`` copies the caller's context into the
worker, so a reporter bound around the offloaded call is visible inside it while
staying invisible to every other in-flight request.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ...ports.progress_reporter import NullProgressReporter, ProgressReporter

_CURRENT_REPORTER: ContextVar[ProgressReporter | None] = ContextVar(
    "repoforge_progress_reporter",
    default=None,
)


def current_progress_reporter() -> ProgressReporter:
    """The reporter bound for this request, or a disabled one when nothing is bound."""

    return _CURRENT_REPORTER.get() or NullProgressReporter()


@contextmanager
def bind_progress_reporter(reporter: ProgressReporter) -> Iterator[None]:
    token = _CURRENT_REPORTER.set(reporter)
    try:
        yield
    finally:
        _CURRENT_REPORTER.reset(token)


__all__ = ["bind_progress_reporter", "current_progress_reporter"]
