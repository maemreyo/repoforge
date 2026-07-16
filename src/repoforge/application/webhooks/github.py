"""Pure, bounded GitHub webhook authentication and routing helpers."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

SUPPORTED_GITHUB_EVENTS = frozenset(
    {"issues", "sub_issues", "issue_dependencies", "projects_v2_item"}
)
_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def verify_github_signature(body: bytes, header: str, secret: bytes) -> bool:
    """Verify GitHub's HMAC-SHA256 signature using constant-time comparison."""
    if not secret or not isinstance(header, str):
        return False
    match = _SIGNATURE.fullmatch(header.strip())
    if match is None:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1))


def affected_repository(event: str, payload: dict[str, Any]) -> str | None:
    if event not in SUPPORTED_GITHUB_EVENTS:
        return None
    repository = payload.get("repository")
    full_name = repository.get("full_name") if isinstance(repository, dict) else None
    if isinstance(full_name, str) and _REPOSITORY.fullmatch(full_name):
        return full_name
    return None


def project_owner(payload: dict[str, Any]) -> str | None:
    for key in ("organization", "sender"):
        raw = payload.get(key)
        login = raw.get("login") if isinstance(raw, dict) else None
        if isinstance(login, str) and login.strip():
            return login.strip()
    return None
