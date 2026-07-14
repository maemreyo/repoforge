"""Persistence boundary for sanitized workflow recordings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.workflow_recording import WorkflowRecording


@dataclass(frozen=True, slots=True)
class WorkflowRecordingPage:
    records: tuple[WorkflowRecording, ...]
    scan_truncated: bool


@dataclass(frozen=True, slots=True)
class WorkflowRetentionReport:
    deleted_for_age: int
    deleted_for_count: int
    deleted_for_bytes: int
    remaining_records: int
    total_bytes: int


class WorkflowRecordingStore(Protocol):
    def create(self, recording: WorkflowRecording) -> WorkflowRecording: ...

    def read(self, recording_id: str) -> WorkflowRecording | None: ...

    def list_records(self, *, max_records: int) -> WorkflowRecordingPage: ...

    def export_fixture(
        self,
        recording_id: str,
        fixture_root: Path,
        fixture_name: str,
    ) -> Path: ...

    def prune(
        self,
        *,
        now: str,
        retention_seconds: int,
        max_records: int,
        max_total_bytes: int,
    ) -> WorkflowRetentionReport: ...
