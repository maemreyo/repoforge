"""Authoritative JSON persistence for RuntimeTransitionStore.

The runtime transition ledger records every activation/switch/rollback attempt
durably BEFORE its pointer mutation, so a crash leaves a trace reconciliation can
act on (F-006). This store is the authoritative home for those transitions; the
SQLite shadow mirrors them for parity checking.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...domain.durable_state import Revision, SchemaVersion, StateCodec, StateEnvelope, StatePage
from ...domain.errors import ConfigError
from ...domain.runtime_transition import (
    RuntimeTransition,
    is_terminal,
    is_terminal_failure,
    runtime_transition_from_payload,
    runtime_transition_payload,
)
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

RUNTIME_TRANSITION_SCHEMA_VERSION = 1

_TRANSITION_ID = re.compile(r"^tran-[a-f0-9]{24}$")


def validate_transition_id(transition_id: str) -> str:
    if not isinstance(transition_id, str) or _TRANSITION_ID.fullmatch(transition_id) is None:
        raise ValueError("runtime transition id must look like tran-<24 hex>")
    return transition_id


class _RuntimeTransitionCodec(StateCodec[RuntimeTransition]):
    schema_version = SchemaVersion(RUNTIME_TRANSITION_SCHEMA_VERSION)

    def encode(self, value: RuntimeTransition) -> dict[str, object]:
        return runtime_transition_payload(value)

    def decode(self, payload: dict[str, object]) -> RuntimeTransition:
        return runtime_transition_from_payload(dict(payload))


class JsonRuntimeTransitionAdapter:
    """Authoritative JSON-backed RuntimeTransitionStore."""

    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records: JsonStateRepository[RuntimeTransition] = JsonStateRepository(
            state_root,
            collection="runtime-transitions",
            locks=locks,
            codec=_RuntimeTransitionCodec(),
            id_validator=validate_transition_id,
            max_record_bytes=8_192,
        )
        self.root = self._records.root

    def create(self, transition: RuntimeTransition) -> StateEnvelope[RuntimeTransition]:
        validate_transition_id(transition.transition_id)
        return self._records.create(transition.transition_id, transition)

    def read(self, transition_id: str) -> StateEnvelope[RuntimeTransition] | None:
        validate_transition_id(transition_id)
        return self._records.read(transition_id)

    def save(
        self,
        transition: RuntimeTransition,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[RuntimeTransition]:
        validate_transition_id(transition.transition_id)
        return self._records.save(
            transition.transition_id,
            transition,
            expected_revision=expected_revision,
        )

    def list_all(self, *, max_records: int = 2_000) -> StatePage[RuntimeTransition]:
        return self._records.list_records(max_records=max_records)

    def list_by_generation(
        self,
        generation: int,
        *,
        max_records: int = 100,
    ) -> StatePage[RuntimeTransition]:
        page = self._records.list_records(max_records=max_records)
        records = tuple(item for item in page.records if item.value.target_generation == generation)
        return StatePage(
            records=records,
            scan_truncated=page.scan_truncated,
            unreadable_record_ids=page.unreadable_record_ids,
        )

    def get_active_by_correlation(
        self, correlation_id: str, *, max_records: int = 100
    ) -> StateEnvelope[RuntimeTransition] | None:
        """The single non-terminal transition for one correlation id.

        Transition ids are random, so they carry no ordering information; the
        "latest" cannot be derived by comparing ids or revisions across attempts.
        The coordinator instead enforces a stronger invariant -- at most one
        non-terminal transition per correlation -- and this lookup returns that
        one. It fails closed (raises) when the invariant is violated or the scan
        cannot prove it held, because answering with a guess would let
        reconciliation terminalize the wrong attempt.
        """
        page = self._records.list_records(max_records=max_records)
        if page.scan_truncated or page.unreadable_record_ids:
            raise ConfigError(
                "RUNTIME_TRANSITION_LOOKUP_INCOMPLETE: the transition scan was "
                "truncated or contained unreadable records, so the active "
                f"transition for correlation {correlation_id} cannot be proven"
            )
        active = [
            item
            for item in page.records
            if item.value.correlation_id == correlation_id
            and not is_terminal(item.value.status)
            and not is_terminal_failure(item.value.status)
        ]
        if len(active) > 1:
            raise ConfigError(
                "RUNTIME_TRANSITION_INVARIANT_VIOLATED: more than one non-terminal "
                f"transition exists for correlation {correlation_id} ("
                + ", ".join(item.record_id for item in active)
                + ")"
            )
        return active[0] if active else None
