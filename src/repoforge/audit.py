"""Backward-compatible audit adapter imports through the composition root."""

from .bootstrap import JsonlAuditSink

AuditLogger = JsonlAuditSink

__all__ = ["AuditLogger", "JsonlAuditSink"]
