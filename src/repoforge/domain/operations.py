"""Pure operational policy for idempotency and bounded automatic retries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import ErrorCode


class IdempotencyState(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    action: str
    key_hash: str
    request_fingerprint: str
    state: IdempotencyState
    updated_at: str
    updated_at_epoch: float
    correlation_id: str
    result: Any | None = None
    receipt_id: str | None = None
    operation_id: str | None = None


_KEYED_IDEMPOTENT_ACTIONS = frozenset(
    {
        "workspace_create",
        "workspace_push",
        "workspace_create_draft_pr",
        "workspace_update_draft_pr",
        "workspace_write_file",
        "workspace_edit",
        "workspace_mutate",
        "workspace_apply_patch",
    }
)
_TRANSIENT_RETRY_CODES = frozenset(
    {
        ErrorCode.COMMAND_TIMEOUT,
        ErrorCode.LOCK_TIMEOUT,
        ErrorCode.RUNTIME_UNAVAILABLE,
        ErrorCode.RUNTIME_RELOADING,
        ErrorCode.STATE_PERSISTENCE_FAILED,
    }
)

_UNCHANGED_STATE: dict[str, tuple[str, ...]] = {
    "workspace_create": (
        "The configured source repository and all existing workspaces remain unchanged.",
    ),
    "workspace_push": (
        "Workspace files and local commit history remain unchanged; remote state follows the explicit error message.",
    ),
    "workspace_create_draft_pr": (
        "Workspace files and local commit history remain unchanged; GitHub state follows the explicit error message.",
    ),
    "workspace_update_draft_pr": (
        "Workspace files and local commit history remain unchanged; GitHub state follows the explicit error message.",
    ),
    "workspace_write_file": (
        "The target file may have changed only when the error explicitly reports an uncertain mutation outcome.",
    ),
    "workspace_edit": (
        "Workspace files may have changed only when the error explicitly reports an uncertain mutation outcome.",
    ),
    "workspace_mutate": (
        "Workspace files and its idempotency receipt commit together or recover together.",
    ),
    "workspace_apply_patch": (
        "Workspace files may have changed only when the error explicitly reports an uncertain mutation outcome.",
    ),
}
_DEFAULT_UNCHANGED_STATE = (
    "No unreported configuration generation, workspace registry record, or local Git history change was committed.",
)


def unchanged_state_for(action: str) -> tuple[str, ...]:
    """Describe durable state that a failed write did not silently alter."""
    return _UNCHANGED_STATE.get(action, _DEFAULT_UNCHANGED_STATE)


def hash_idempotency_key(key: str) -> str:
    """Validate and irreversibly hash an operator-provided idempotency key."""
    if not isinstance(key, str) or not 8 <= len(key) <= 200:
        raise ValueError("Idempotency key must contain between 8 and 200 characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in key):
        raise ValueError("Idempotency key must not contain control characters")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def request_fingerprint(request: Any) -> str:
    """Hash canonical JSON input so one key cannot authorize different operations."""
    try:
        encoded = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Idempotent request must be canonical JSON data") from exc
    return hashlib.sha256(encoded).hexdigest()


def automatic_retry_allowed(
    action: str, error_code: ErrorCode, *, has_idempotency_key: bool
) -> bool:
    """Allow automated retry only for reviewed keyed workflows and transient failures."""
    return (
        has_idempotency_key
        and action in _KEYED_IDEMPOTENT_ACTIONS
        and error_code in _TRANSIENT_RETRY_CODES
    )
