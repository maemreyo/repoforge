"""Compatibility redaction helpers backed by the central egress policy."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from .egress import (
    EgressContentClass,
    EgressDestination,
    EgressPolicy,
    EgressRange,
    EgressRequest,
    evaluate_egress,
)


def _legacy_redaction(value: str, ranges: tuple[EgressRange, ...]) -> str:
    if not ranges:
        return value
    parts: list[str] = []
    cursor = 0
    for item in ranges:
        parts.append(value[cursor : item.start])
        parts.append("<redacted>")
        cursor = item.end
    parts.append(value[cursor:])
    return "".join(parts)


def _bound_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, limit // 2)
    omitted = len(value) - (half * 2)
    return f"{value[:half]}\n... <{omitted} characters omitted> ...\n{value[-half:]}"


def redact_text(value: str, *, secrets: Iterable[str] = (), limit: int = 8_000) -> str:
    """Redact credential-shaped values while preserving the legacy marker contract."""

    if not isinstance(value, str):
        value = str(value)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    encoded_bytes = len(value.encode("utf-8", errors="replace"))
    if encoded_bytes > 20_000_000:
        return "<redacted:oversized>"
    result = evaluate_egress(
        EgressRequest(
            value,
            EgressContentClass.DIAGNOSTIC,
            EgressDestination.DIAGNOSTIC,
            explicit_secrets=tuple(item for item in secrets if item),
            policy=EgressPolicy(
                max_input_bytes=max(1, encoded_bytes),
                max_output_chars=max(1, min(max(len(value), limit), 1_000_000)),
                max_output_lines=20_000,
                withhold_private_keys=False,
            ),
        )
    )
    redacted = _legacy_redaction(value, result.redaction_ranges)
    return _bound_text(redacted, limit)


_SENSITIVE_KEYS = {
    "authorization",
    "control_plane_api_key",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "secret",
    "password",
    "credential",
    "credentials",
}


def redact_data(value: object, *, secrets: Iterable[str] = ()) -> object:
    """Recursively redact structured diagnostic/audit data without changing safe scalars."""

    explicit = tuple(item for item in secrets if item)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            result[str(key)] = (
                "<redacted>"
                if normalized in _SENSITIVE_KEYS
                else redact_data(item, secrets=explicit)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [redact_data(item, secrets=explicit) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets=explicit)
    return value


_OMITTED_CONTENT_KEYS = {
    "body",
    "content",
    "patch",
    "diff",
    "stdout",
    "stderr",
    "environment",
}


def sanitize_persisted_data(value: object, *, secrets: Iterable[str] = ()) -> object:
    """Redact secrets and omit high-risk content from durable operational receipts."""

    explicit = tuple(item for item in secrets if item)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                result[str(key)] = "<redacted>"
            elif normalized in _OMITTED_CONTENT_KEYS:
                encoded = repr(item).encode("utf-8", errors="replace")
                result[f"{key}_omitted"] = True
                result[f"{key}_sha256"] = hashlib.sha256(encoded).hexdigest()
            else:
                result[str(key)] = sanitize_persisted_data(item, secrets=explicit)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_persisted_data(item, secrets=explicit) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets=explicit)
    return value
