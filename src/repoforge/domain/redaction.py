"""Pure bounded secret redaction for operator-facing diagnostics and errors."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|control_plane_api_key|api[_-]?key|access[_-]?token|token|secret|password)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://[^/@\s:]+:)([^@/\s]+)(@)")


def redact_text(value: str, *, secrets: Iterable[str] = (), limit: int = 8_000) -> str:
    """Redact credential-shaped values and bound the resulting diagnostic text."""
    result = _BEARER.sub("Bearer <redacted>", value)
    result = _SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", result)
    result = _URL_CREDENTIALS.sub(r"\1<redacted>\3", result)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        result = result.replace(secret, "<redacted>")
    if len(result) <= limit:
        return result
    half = max(1, limit // 2)
    omitted = len(result) - (half * 2)
    return f"{result[:half]}\n... <{omitted} characters omitted> ...\n{result[-half:]}"


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
    """Recursively redact structured diagnostic/audit data without stringifying safe scalars."""
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            result[str(key)] = (
                "<redacted>"
                if normalized in _SENSITIVE_KEYS
                else redact_data(item, secrets=secrets)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [redact_data(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
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
                result[str(key)] = sanitize_persisted_data(item, secrets=secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_persisted_data(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    return value
