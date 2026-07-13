"""Pure bounded secret redaction for operator-facing diagnostics and errors."""

from __future__ import annotations

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
