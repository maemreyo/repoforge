"""In-process wakeup for durable-operation terminal transitions (#379 AC3).

Cross-process/after-restart waiters never see an event fire for an operation this process
didn't create or claim -- they fall back to the existing poll cadence unchanged (see
`durable_wait.wait_for_operation`). This only removes the *typical* up-to-`FOREGROUND_POLL_
SECONDS` detection lag for the dominant same-process case (every #379 promoted-inline run,
and every ordinary `background=true` run claimed by this same process's own
`OperationWorkLoop`); it is not a substitute for polling as the cross-process correctness
backstop, since no cross-process notify primitive exists in this codebase to build on.
"""

from __future__ import annotations

import threading


class OperationCompletionSignals:
    """Registry of one `threading.Event` per operation_id, for this process's lifetime.

    `register()` and `fire()` may race in either order -- both create the shared Event via
    the same `setdefault`, so a completion that fires before anyone ever waits still leaves
    a pre-set Event for a later `register()` to find (`Event.wait()` on an already-set Event
    returns immediately). Once fired, an Event is never removed or reset: an operation_id
    never returns to a non-terminal state, so a stale Event can only ever report the correct
    answer -- lazily bounded by how many distinct operation_ids this process has ever seen.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}

    def register(self, operation_id: str) -> threading.Event:
        with self._lock:
            return self._events.setdefault(operation_id, threading.Event())

    def fire(self, operation_id: str) -> None:
        with self._lock:
            event = self._events.setdefault(operation_id, threading.Event())
        event.set()
