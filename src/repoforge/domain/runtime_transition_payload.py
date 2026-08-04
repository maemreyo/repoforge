"""Payload codec for the durable RuntimeTransition record (F-009).

Separated from ``runtime_transition.py`` so that file stays under the 400-line
policy, mirroring the ``process_lease_payload`` split.
"""

from __future__ import annotations

from .runtime_transition import RuntimeTransition, RuntimeTransitionStatus


def runtime_transition_payload(transition: RuntimeTransition) -> dict[str, object]:
    """Serialize a transition for durable storage; status becomes its string value."""
    return {
        "transition_id": transition.transition_id,
        "status": transition.status.value,
        "target_generation": transition.target_generation,
        "config_generation": transition.config_generation,
        "correlation_id": transition.correlation_id,
        "started_at": transition.started_at,
        "updated_at": transition.updated_at,
        "completed_at": transition.completed_at,
        "error_code": transition.error_code,
        "error_message": transition.error_message,
        "previous_transition_id": transition.previous_transition_id,
        "kind": transition.kind,
        "from_sha": transition.from_sha,
        "to_sha": transition.to_sha,
    }


def runtime_transition_from_payload(payload: dict[str, object]) -> RuntimeTransition:
    """Decode a stored payload; an unknown status raises ValueError."""
    required = {
        "transition_id",
        "status",
        "correlation_id",
        "started_at",
        "updated_at",
    }
    if not required.issubset(payload):
        raise ValueError("runtime transition payload is missing required fields")
    return RuntimeTransition(
        transition_id=str(payload["transition_id"]),
        status=RuntimeTransitionStatus(str(payload["status"])),
        target_generation=_payload_optional_int(
            payload.get("target_generation"), "target_generation"
        ),
        config_generation=_payload_optional_int(
            payload.get("config_generation"), "config_generation"
        ),
        correlation_id=str(payload["correlation_id"]),
        started_at=str(payload["started_at"]),
        updated_at=str(payload["updated_at"]),
        completed_at=_payload_optional_str(payload.get("completed_at")),
        error_code=_payload_optional_str(payload.get("error_code")),
        error_message=_payload_optional_str(payload.get("error_message")),
        previous_transition_id=_payload_optional_str(payload.get("previous_transition_id")),
        kind=_payload_optional_str(payload.get("kind")) or "activate",
        from_sha=_payload_optional_str(payload.get("from_sha")),
        to_sha=_payload_optional_str(payload.get("to_sha")),
    )


def _payload_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _payload_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _payload_int(value, field)


def _payload_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "runtime_transition_from_payload",
    "runtime_transition_payload",
]
