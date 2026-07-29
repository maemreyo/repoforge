"""Bounded command-output excerpts that keep the evidence the caller asked for."""

from __future__ import annotations

_OMISSION = "\n... <{} characters omitted> ...\n"

# A failing command announces itself at the tail: pytest's short test summary, a
# linter's final tally, a build's last error. A head-only cut therefore spends the
# whole budget on collection noise and discards the one thing the caller needs, so
# the tail is given twice the head's share.
_TAIL_SHARE = 2


def bound_command_excerpt(value: str, limit: int) -> str:
    """Bound ``value`` to at most ``limit`` characters, keeping its head and tail.

    Returns ``value`` unchanged when it already fits. Otherwise the head keeps a
    third of the remaining budget and the tail keeps the rest, separated by a
    marker naming the omitted character count. The result never exceeds ``limit``,
    so a contract field bounded to the same number always accepts it.
    """
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    # Reserve the marker at its widest: `omitted` can never exceed len(value), so
    # rendering the count with that many digits can only over-reserve, never under.
    reserved = len(_OMISSION.format(len(value)))
    budget = limit - reserved
    if budget < _TAIL_SHARE + 1:
        # Too small to carry both ends plus the marker; the tail alone is the
        # evidence, so spend the whole limit on it.
        return value[-limit:]
    head_length = budget // (_TAIL_SHARE + 1)
    tail_length = budget - head_length
    omitted = len(value) - head_length - tail_length
    return f"{value[:head_length]}{_OMISSION.format(omitted)}{value[-tail_length:]}"


__all__ = ["bound_command_excerpt"]
