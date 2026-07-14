"""Bounded creation and retention of sanitized workflow recordings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.workflow_recording import (
    MAX_WORKFLOW_EVENTS,
    WorkflowEvent,
    WorkflowFinalOutcome,
    WorkflowMetrics,
    WorkflowRecording,
    new_workflow_recording,
)
from ...ports.workflow_recording_store import (
    WorkflowRecordingPage,
    WorkflowRecordingStore,
    WorkflowRetentionReport,
)
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkflowRecordCommand:
    scenario_id: str
    server_instructions_hash: str
    tool_surface_hash: str
    capability_flags: tuple[str, ...]
    events: tuple[WorkflowEvent, ...]
    final_outcome: WorkflowFinalOutcome
    metrics: WorkflowMetrics


class WorkflowRecorder:
    def __init__(self, ctx: ApplicationContext, store: WorkflowRecordingStore):
        self.ctx = ctx
        self.store = store

    def record(self, command: WorkflowRecordCommand) -> WorkflowRecording:
        if not command.events:
            raise RepoForgeError(
                "workflow recording requires at least one sanitized event",
                code=ErrorCode.WORKFLOW_RECORD_INVALID,
            )
        recording_id = f"wr-{self.ctx.ids.new_hex(24)}"
        created_at = self.ctx.clock.now_iso()
        events = command.events[:MAX_WORKFLOW_EVENTS]
        truncated = len(command.events) > len(events)
        reason = "event_limit" if truncated else None

        def build(
            current_events: tuple[WorkflowEvent, ...], current_reason: str | None
        ) -> WorkflowRecording:
            is_truncated = current_reason is not None
            return new_workflow_recording(
                recording_id=recording_id,
                scenario_id=command.scenario_id,
                server_instructions_hash=command.server_instructions_hash,
                tool_surface_hash=command.tool_surface_hash,
                capability_flags=command.capability_flags,
                events=current_events,
                final_outcome=(
                    WorkflowFinalOutcome.INCOMPLETE if is_truncated else command.final_outcome
                ),
                metrics=command.metrics,
                created_at=created_at,
                truncated=is_truncated,
                truncation_reason=current_reason,
            )

        def operation() -> WorkflowRecording:
            nonlocal events, reason
            while events:
                recording = build(events, reason)
                try:
                    return self.store.create(recording)
                except RepoForgeError as exc:
                    if exc.code is not ErrorCode.WORKFLOW_RECORD_TOO_LARGE or len(events) == 1:
                        raise
                    events = events[:-1]
                    reason = "event_and_size_limit" if truncated else "size_limit"
            raise RepoForgeError(
                "workflow recording could not retain one bounded event",
                code=ErrorCode.WORKFLOW_RECORD_TOO_LARGE,
            )

        return self.ctx.audited(
            "workflow_record_create",
            {
                "recording_id": recording_id,
                "scenario_id": command.scenario_id,
                "requested_event_count": len(command.events),
                "event_limit_applied": truncated,
            },
            operation,
            mutating=True,
        )

    def read(self, recording_id: str) -> WorkflowRecording:
        recording = self.store.read(recording_id)
        if recording is None:
            raise RepoForgeError(
                f"workflow recording not found: {recording_id}",
                code=ErrorCode.WORKFLOW_RECORD_NOT_FOUND,
            )
        return recording

    def list_records(self, *, max_records: int = 100) -> WorkflowRecordingPage:
        return self.store.list_records(max_records=max_records)

    def export_fixture(
        self,
        recording_id: str,
        fixture_root: Path,
        fixture_name: str,
    ) -> Path:
        return self.ctx.audited(
            "workflow_record_export",
            {
                "recording_id": recording_id,
                "fixture_name": fixture_name,
            },
            lambda: self.store.export_fixture(recording_id, fixture_root, fixture_name),
            mutating=True,
        )

    def prune(
        self,
        *,
        retention_seconds: int = 30 * 24 * 60 * 60,
        max_records: int = 1_000,
        max_total_bytes: int = 64 * 1024 * 1024,
    ) -> WorkflowRetentionReport:
        return self.ctx.audited(
            "workflow_record_prune",
            {
                "retention_seconds": retention_seconds,
                "max_records": max_records,
                "max_total_bytes": max_total_bytes,
            },
            lambda: self.store.prune(
                now=self.ctx.clock.now_iso(),
                retention_seconds=retention_seconds,
                max_records=max_records,
                max_total_bytes=max_total_bytes,
            ),
            mutating=True,
        )
