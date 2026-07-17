"""Bounded, session-local, in-memory-only tracking for advisory adoption nudges.

Issue #140 asks for three bounded, advisory nudges that suggest a cheaper or more
durable tool inside structured fields an agent already reads (``next_step`` /
``safe_next_action``), at the exact moment an inefficient usage pattern occurs:

1. repeated pending ``workspace_pr_checks`` polling of the same workspace names
   ``workspace_pr_watch``;
2. repeated consecutive single-file ``workspace_read_file`` reads of the same
   workspace name ``workspace_read_files``;
3. a failing required check in a ``workspace_pr_checks`` result names
   ``workspace_pr_check_details`` / ``workspace_pr_failure_evidence``.

Nudges 1 and 2 are frequency-based and require remembering recent call timestamps
across otherwise-independent requests within the same running service instance.
This module holds that memory. Nudge 3 is purely state-based (it only inspects the
result being built) and needs no tracker at all.

Everything here is:

- in-memory only -- never written to disk, never part of any persisted state;
- excluded from every audit payload -- callers only ever read a boolean/selector
  out of a tracker and fold it into an existing result's advisory text field;
- bounded regardless of session length -- each tracked key (a ``workspace_id``)
  keeps at most its pattern's threshold worth of timestamps, and the tracker as a
  whole keeps at most ``_MAX_TRACKED_KEYS`` least-recently-used keys, so a session
  that touches thousands of workspaces or files over hours cannot grow this
  tracker's memory without bound;
- purely advisory -- nothing here changes what any tool is allowed to do, only
  the text suggested in a result that already exists.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Final, final

_MAX_TRACKED_KEYS: Final[int] = 200


class _BoundedEventWindow:
    """Least-recently-used bounded per-key sliding time window of event counts.

    Each key keeps at most ``threshold`` timestamps (older ones are evicted by
    the deque's ``maxlen`` as new ones arrive), and the window additionally
    prunes any timestamp older than the caller-supplied ``window_seconds`` on
    every observation, so a count reflects only events within the trailing
    window. The tracker overall keeps at most ``_MAX_TRACKED_KEYS`` keys,
    evicting the least-recently-used one once that bound is exceeded.
    """

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold
        self._data: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, key: str, now_epoch: float, window_seconds: float) -> int:
        """Record one event for ``key``; return its count within the window."""
        with self._lock:
            events = self._data.get(key)
            if events is None:
                events = deque(maxlen=self._threshold)
                self._data[key] = events
            else:
                self._data.move_to_end(key)
            events.append(now_epoch)
            cutoff = now_epoch - window_seconds
            while events and events[0] < cutoff:
                events.popleft()
            count = len(events)
            while len(self._data) > _MAX_TRACKED_KEYS:
                self._data.popitem(last=False)
            return count

    def reset(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


@final
class AdoptionNudgeTracker:
    """Session-local (one instance per :class:`ApplicationContext`), in-memory
    tracker for the two frequency-based advisory nudges defined by issue #140.

    Deterministic thresholds:

    - ``workspace_pr_checks`` polled while pending 3+ times within 10 minutes
      for the same ``workspace_id`` -> suggest ``workspace_pr_watch``.
    - ``workspace_read_file`` called 5+ times within 1 minute for the same
      ``workspace_id`` without an intervening ``workspace_read_files`` call ->
      suggest batching.
    """

    PR_CHECK_POLL_WINDOW_SECONDS: Final[float] = 600.0
    PR_CHECK_POLL_THRESHOLD: Final[int] = 3
    FILE_READ_WINDOW_SECONDS: Final[float] = 60.0
    FILE_READ_THRESHOLD: Final[int] = 5
    # Issue #169: a repeated identical ad-hoc argv shape within a workspace suggests
    # the command belongs in an enrolled workspace_run_diagnostic template instead.
    ADHOC_ARGV_WINDOW_SECONDS: Final[float] = 3_600.0
    ADHOC_ARGV_THRESHOLD: Final[int] = 3
    # Issue #166: once shown, don't repeat the stale-workspace nudge on every
    # workspace_create/workspace_list call within this window.
    STALE_WORKSPACE_NUDGE_WINDOW_SECONDS: Final[float] = 3_600.0

    def __init__(self) -> None:
        self._pr_check_polls = _BoundedEventWindow(self.PR_CHECK_POLL_THRESHOLD)
        self._file_reads = _BoundedEventWindow(self.FILE_READ_THRESHOLD)
        self._adhoc_argv = _BoundedEventWindow(self.ADHOC_ARGV_THRESHOLD)
        self._stale_workspace_last_shown: float | None = None
        self._stale_workspace_lock = threading.Lock()

    def observe_pending_pr_check_poll(self, workspace_id: str, now_epoch: float) -> bool:
        """Record one pending poll; report whether the nudge threshold is met."""
        count = self._pr_check_polls.observe(
            workspace_id, now_epoch, self.PR_CHECK_POLL_WINDOW_SECONDS
        )
        return count >= self.PR_CHECK_POLL_THRESHOLD

    def reset_pr_check_polls(self, workspace_id: str) -> None:
        """Forget tracked polls once a workspace's checks are no longer pending."""
        self._pr_check_polls.reset(workspace_id)

    def observe_single_file_read(self, workspace_id: str, now_epoch: float) -> bool:
        """Record one single-file read; report whether the nudge threshold is met."""
        count = self._file_reads.observe(workspace_id, now_epoch, self.FILE_READ_WINDOW_SECONDS)
        return count >= self.FILE_READ_THRESHOLD

    def reset_file_reads(self, workspace_id: str) -> None:
        """Forget tracked single-file reads once batching is used for this workspace."""
        self._file_reads.reset(workspace_id)

    def observe_adhoc_argv(self, workspace_id: str, argv_shape_key: str, now_epoch: float) -> bool:
        """Record one ad-hoc run of this argv shape; report whether it has recurred enough
        to suggest enrolling it as a reviewed diagnostic template."""
        count = self._adhoc_argv.observe(
            f"{workspace_id}:{argv_shape_key}", now_epoch, self.ADHOC_ARGV_WINDOW_SECONDS
        )
        return count >= self.ADHOC_ARGV_THRESHOLD

    def observe_stale_workspace_nudge(self, now_epoch: float) -> bool:
        """Report whether the stale-workspace nudge may fire now, and if so, record it.

        Returns ``True`` (and starts/refreshes the rate-limit clock) the first time
        this is called, or once ``STALE_WORKSPACE_NUDGE_WINDOW_SECONDS`` has elapsed
        since it last returned ``True``; returns ``False`` (a suppressed repeat)
        otherwise.

        Callers must only invoke this once they have already decided the nudge would
        otherwise fire (e.g. the candidate count met the configured threshold), so a
        call with nothing to show never starts the rate-limit clock.
        """
        with self._stale_workspace_lock:
            last = self._stale_workspace_last_shown
            if last is not None and now_epoch - last < self.STALE_WORKSPACE_NUDGE_WINDOW_SECONDS:
                return False
            self._stale_workspace_last_shown = now_epoch
            return True
