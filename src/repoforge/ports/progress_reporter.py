"""Push progress for a long-running operation during an open ``operation wait``.

Turn-based agents learn that a background operation finished by polling. When a
client advertises progress-notification support (and supplies a progress token),
a bounded ``operation wait`` can instead stream progress on the open request so
one call replaces many polls. A reporter that is not ``enabled`` is a no-op and
the caller falls back to the existing poll guidance -- never push into a client
that cannot consume it.
"""

from __future__ import annotations

from typing import Protocol


class ProgressReporter(Protocol):
    @property
    def enabled(self) -> bool:
        """Whether this client can actually receive pushed progress."""
        ...

    def report(self, *, current: int, total: int | None, message: str | None) -> None:
        """Emit one progress update; implementations must never raise."""
        ...


class NullProgressReporter:
    """Disabled reporter: the wait falls back to poll guidance."""

    enabled = False

    def report(self, *, current: int, total: int | None, message: str | None) -> None:
        return None
