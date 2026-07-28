"""Thread-safe operation admission and bounded drain state."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import ClassVar

from ...domain.errors import ConfigError, ErrorCode, RepoForgeError
from ...domain.redaction import redact_text
from ...ports.operation_gate import GateState


class InProcessOperationGate:
    _HASH_FIELDS = frozenset(
        {
            "server_build_sha",
            "tool_surface_hash",
            "input_contract_digest",
            "output_contract_digest",
            "process_start_identity",
        }
    )
    _STRING_LIMITS: ClassVar[dict[str, int]] = {
        "operation_id": 160,
        "receipt_id": 160,
        "server_build_sha": 64,
        "server_version": 160,
        "tool_surface_hash": 64,
        "input_contract_digest": 64,
        "output_contract_digest": 64,
        "process_start_identity": 64,
        "rediscovery_action": 160,
    }
    _INTEGER_FIELDS = frozenset({"config_generation", "runtime_protocol_version"})
    _SHA256 = re.compile(r"^[a-f0-9]{64}$")
    _OPERATION_ID = re.compile(r"^op-[a-f0-9]{24}$")
    _RECEIPT_ID = re.compile(r"^receipt-[a-f0-9]{24}$")
    _SAFE_ACTION = re.compile(r"^[a-z][a-z0-9_.-]{0,159}$")

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._state = GateState.OPEN
        self._active_reads = 0
        self._active_writes = 0
        #: Reference-counted so a reused/duplicate operation_id can never be
        #: dropped early; keyed by id, not by call, to answer "who is
        #: currently holding the gate open" during a stuck drain.
        self._active_operation_ids: dict[str, int] = {}
        self._reason = ""
        self._correlation_id = ""
        self._reconnect_details: tuple[tuple[str, object], ...] = ()

    @classmethod
    def _sanitize_reconnect_details(
        cls, details: Mapping[str, object] | None
    ) -> tuple[tuple[str, object], ...]:
        if details is None:
            return ()
        allowed = set(cls._STRING_LIMITS) | set(cls._INTEGER_FIELDS)
        unknown = sorted(set(details) - allowed)
        if unknown:
            raise ValueError(f"Unsupported reconnect detail fields: {', '.join(unknown)}")
        sanitized: list[tuple[str, object]] = []
        for field in sorted(details):
            value = details[field]
            if field in cls._INTEGER_FIELDS:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"Reconnect detail {field} must be a positive integer")
                sanitized.append((field, value))
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"Reconnect detail {field} must be a non-empty string")
            if field in cls._HASH_FIELDS:
                if cls._SHA256.fullmatch(value) is None:
                    raise ValueError(f"Reconnect detail {field} must be a lowercase SHA-256")
                sanitized.append((field, value))
                continue
            bounded = redact_text(value, limit=cls._STRING_LIMITS[field])
            if field == "operation_id" and cls._OPERATION_ID.fullmatch(bounded) is None:
                raise ValueError("Reconnect operation id is invalid")
            if field == "receipt_id" and cls._RECEIPT_ID.fullmatch(bounded) is None:
                raise ValueError("Reconnect receipt id is invalid")
            if field == "rediscovery_action" and cls._SAFE_ACTION.fullmatch(bounded) is None:
                raise ValueError("Reconnect rediscovery action is invalid")
            sanitized.append((field, bounded))
        return tuple(sanitized)

    @contextmanager
    def operation(self, operation_id: str, *, mutating: bool) -> Iterator[None]:
        with self._condition:
            if self._state is GateState.FAIL_CLOSED:
                raise ConfigError(
                    f"RUNTIME_FAIL_CLOSED: {self._reason}; correlation_id={self._correlation_id}"
                )
            if self._state is GateState.DRAINING:
                if self._reconnect_details:
                    raise RepoForgeError(
                        "RECONNECT_REQUIRED: runtime generation changed",
                        code=ErrorCode.RECONNECT_REQUIRED,
                        retryable=False,
                        safe_next_action=(
                            "Reconnect, rediscover the active contract, then resume the durable operation."
                        ),
                        unchanged_state=(
                            "The rejected request was not admitted to either generation.",
                        ),
                        details=dict(self._reconnect_details),
                    )
                raise ConfigError(
                    f"RUNTIME_RELOADING: {self._reason}; correlation_id={self._correlation_id}"
                )
            if mutating:
                self._active_writes += 1
            else:
                self._active_reads += 1
            self._active_operation_ids[operation_id] = (
                self._active_operation_ids.get(operation_id, 0) + 1
            )
        try:
            yield
        finally:
            with self._condition:
                if mutating:
                    self._active_writes -= 1
                else:
                    self._active_reads -= 1
                remaining = self._active_operation_ids.get(operation_id, 0) - 1
                if remaining > 0:
                    self._active_operation_ids[operation_id] = remaining
                else:
                    self._active_operation_ids.pop(operation_id, None)
                self._condition.notify_all()

    def begin_drain(
        self,
        *,
        reason: str,
        correlation_id: str,
        reconnect_details: Mapping[str, object] | None = None,
    ) -> None:
        sanitized = self._sanitize_reconnect_details(reconnect_details)
        with self._condition:
            self._state = GateState.DRAINING
            self._reason = reason
            self._correlation_id = correlation_id
            self._reconnect_details = sanitized
            self._condition.notify_all()

    def fail_closed(self, *, reason: str, correlation_id: str) -> None:
        with self._condition:
            self._state = GateState.FAIL_CLOSED
            self._reason = reason
            self._correlation_id = correlation_id
            self._reconnect_details = ()
            self._condition.notify_all()

    def reopen(self) -> None:
        with self._condition:
            self._state = GateState.OPEN
            self._reason = ""
            self._correlation_id = ""
            self._reconnect_details = ()
            self._condition.notify_all()

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._active_reads or self._active_writes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "state": self._state.value,
                "active_reads": self._active_reads,
                "active_writes": self._active_writes,
                "active_operation_ids": sorted(self._active_operation_ids),
                "reason": self._reason,
                "correlation_id": self._correlation_id,
            }
