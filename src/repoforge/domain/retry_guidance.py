"""Pure signature comparison and consecutive-failure counting for verification retries.

Issue #167: turn blind retry bursts into investigation. A bounded per-workspace,
per-target history of the most recent failure signature (error code plus, when
present, the failing step index and exit code) lets a failing run tell whether a
retry could possibly change the outcome, before the caller blindly reruns.

The history lives in ``WorkspaceRecord.metadata`` (the existing durable state
record), so it is pruned automatically whenever the workspace itself is removed --
no separate lifecycle to manage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The metadata key under which every target's last-failure signature is kept.
METADATA_KEY = "retry_guidance_history"
#: Bound the number of distinct targets (profile/diagnostic ids) tracked per
#: workspace, regardless of how many are ever run against it.
MAX_TRACKED_TARGETS = 16
#: A full/default profile that fails faster than this is a lint/syntax-class
#: failure the quick profile or a targeted diagnostic would have caught cheaper.
DEFAULT_FAST_FAIL_THRESHOLD_SECONDS = 10.0
NOT_FOUND_CODES = frozenset({"NOT_FOUND", "DIAGNOSTIC_TOOL_MISSING"})


@dataclass(frozen=True, slots=True)
class FailureSignature:
    error_code: str
    failed_step: int | None = None
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class RetryGuidance:
    identical_failure_repeat: int
    statement: str
    safe_next_action: str


def _signature_matches(previous: dict[str, Any], signature: FailureSignature) -> bool:
    return (
        previous.get("error_code") == signature.error_code
        and previous.get("failed_step") == signature.failed_step
        and previous.get("exit_code") == signature.exit_code
    )


def record_and_compare(
    metadata: dict[str, Any],
    *,
    target: str,
    fingerprint: str,
    signature: FailureSignature,
) -> tuple[int, RetryGuidance | None]:
    """Record one failure; return its consecutive-repeat count and guidance if repeated.

    Mutates ``metadata`` in place (the caller is responsible for persisting it).
    Detection resets whenever the fingerprint differs from the stored one, so a
    mutation between runs always yields a fresh ``repeat`` count of 1 (no guidance).
    """
    history = metadata.get(METADATA_KEY)
    if not isinstance(history, dict):
        history = {}
        metadata[METADATA_KEY] = history

    previous = history.get(target)
    repeat = 1
    if (
        isinstance(previous, dict)
        and previous.get("fingerprint") == fingerprint
        and _signature_matches(previous, signature)
    ):
        repeat = int(previous.get("repeat", 1)) + 1

    history[target] = {
        "fingerprint": fingerprint,
        "error_code": signature.error_code,
        "failed_step": signature.failed_step,
        "exit_code": signature.exit_code,
        "repeat": repeat,
    }
    while len(history) > MAX_TRACKED_TARGETS:
        history.pop(next(iter(history)))

    if repeat < 2:
        return repeat, None
    return repeat, RetryGuidance(
        identical_failure_repeat=repeat,
        statement=(
            f"This is the identical failure {repeat} times in a row with no workspace "
            "mutation in between; nothing has changed since the last failing run."
        ),
        safe_next_action=(
            "Investigate instead of retrying: review the failed-step evidence, target the "
            "failing check with workspace_run_diagnostic, or iterate with the quick profile."
        ),
    )


def clear(metadata: dict[str, Any], *, target: str) -> bool:
    """Forget a target's tracked failure signature after a successful run.

    Returns whether anything was actually removed (so the caller can skip an
    unnecessary durable-state write).
    """
    history = metadata.get(METADATA_KEY)
    if not isinstance(history, dict) or target not in history:
        return False
    del history[target]
    return True


def not_found_guidance() -> RetryGuidance:
    """Guidance for a missing dependency/executable -- always shown on first occurrence."""
    return RetryGuidance(
        identical_failure_repeat=0,
        statement="A dependency or command is missing in this worktree; retrying will not fix this.",
        safe_next_action=(
            "Run the repository's enrolled setup profile (if any) or install the missing "
            "dependency before retrying the same command."
        ),
    )


def fast_fail_guidance(
    duration_seconds: float, *, threshold_seconds: float
) -> RetryGuidance | None:
    """Guidance for a full/default profile that failed faster than the lint/syntax threshold."""
    if duration_seconds >= threshold_seconds:
        return None
    return RetryGuidance(
        identical_failure_repeat=0,
        statement=(
            f"This run failed in {duration_seconds:.1f}s, well inside the "
            f"{threshold_seconds:.0f}s fast-fail threshold -- likely a lint/syntax/import-class "
            "failure the full profile is too slow and expensive to keep rediscovering."
        ),
        safe_next_action=(
            "Iterate with the quick profile or workspace_run_diagnostic instead of rerunning "
            "the full profile for every edit."
        ),
    )


__all__ = [
    "DEFAULT_FAST_FAIL_THRESHOLD_SECONDS",
    "MAX_TRACKED_TARGETS",
    "METADATA_KEY",
    "NOT_FOUND_CODES",
    "FailureSignature",
    "RetryGuidance",
    "clear",
    "fast_fail_guidance",
    "not_found_guidance",
    "record_and_compare",
]
