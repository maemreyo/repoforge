"""Request-local audit origin attribution without exposing raw session identities."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

AuditOrigin = Literal["model", "connector", "internal", "background_worker"]


@dataclass(frozen=True, slots=True)
class AuditAttribution:
    origin: AuditOrigin
    session_hash: str | None = None


_CURRENT_ATTRIBUTION: ContextVar[AuditAttribution | None] = ContextVar(
    "repoforge_audit_attribution",
    default=None,
)


def current_audit_attribution() -> AuditAttribution:
    return _CURRENT_ATTRIBUTION.get() or AuditAttribution(origin="internal")


@contextmanager
def bind_audit_attribution(
    *,
    origin: AuditOrigin,
    session_hash: str | None = None,
) -> Iterator[None]:
    token = _CURRENT_ATTRIBUTION.set(AuditAttribution(origin=origin, session_hash=session_hash))
    try:
        yield
    finally:
        _CURRENT_ATTRIBUTION.reset(token)
