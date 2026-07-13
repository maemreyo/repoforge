"""Backward-compatible local runtime state API through the composition root."""

from .bootstrap import (
    ManagedRuntime,
    RuntimeState,
    clear_runtime_state,
    managed_start_claim,
    read_managed_runtime,
    read_runtime_log,
    read_runtime_state,
    stop_managed_runtime,
    write_managed_runtime,
    write_runtime_state,
)

__all__ = [
    "ManagedRuntime",
    "RuntimeState",
    "clear_runtime_state",
    "managed_start_claim",
    "read_managed_runtime",
    "read_runtime_log",
    "read_runtime_state",
    "stop_managed_runtime",
    "write_managed_runtime",
    "write_runtime_state",
]
