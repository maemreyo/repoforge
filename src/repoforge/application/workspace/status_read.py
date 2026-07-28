"""Bounded workspace-lock acquisition shared by the workspace status readers.

An execution holds the workspace lock for the entire lifetime of its command, so a status read
that waits for that lock is only as responsive as the slowest verification. Status reads instead
wait a bounded interval and then read anyway, reporting which of the two happened rather than
presenting an unsynchronized read as an exclusive one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Literal

from ...domain.errors import ErrorCode, RepoForgeError
from ..context import ApplicationContext

ReadConsistency = Literal["locked", "concurrent_write"]

LOCKED: ReadConsistency = "locked"
CONCURRENT_WRITE: ReadConsistency = "concurrent_write"


@contextmanager
def status_read_lease(ctx: ApplicationContext, workspace_id: str) -> Iterator[ReadConsistency]:
    """Hold the workspace lock for a status read, or proceed without it and say so.

    Yields ``"locked"`` when the lock was acquired within
    ``server.status_read_lock_timeout_seconds`` and the read is therefore exclusive of RepoForge
    writers, and ``"concurrent_write"`` when another holder -- normally a running command -- kept
    it for longer. A ``"concurrent_write"`` read observes a tree that may be changing underneath
    it: individual facts are each true when sampled, but they are not guaranteed to describe one
    instant, and nothing derived from them may be written back to shared state.
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(
                ctx.locks.lock(
                    workspace_id,
                    timeout_seconds=ctx.config.server.status_read_lock_timeout_seconds,
                    metadata={"purpose": "status_read"},
                )
            )
        except RepoForgeError as exc:
            if exc.code is not ErrorCode.LOCK_TIMEOUT:
                raise
            yield CONCURRENT_WRITE
        else:
            yield LOCKED
