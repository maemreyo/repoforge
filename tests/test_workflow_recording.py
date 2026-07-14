from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

from repoforge.adapters.persistence.json_workflow_recording_store import (
    JsonWorkflowRecordingStore,
)
from repoforge.application.workflow.recorder import WorkflowRecordCommand
from repoforge.application.workflow.replay import (
    RecordedCategoryReplayAdapter,
    WorkflowReplayEngine,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.workflow_recording import (
    MAX_WORKFLOW_EVENTS,
    MAX_WORKFLOW_RECORD_BYTES,
    WORKFLOW_RECORDING_SCHEMA_VERSION,
    WorkflowEvent,
    WorkflowFinalOutcome,
    WorkflowMetrics,
    WorkflowRecording,
    WorkflowResultCategory,
    WorkflowStateTransition,
    new_workflow_recording,
    normalize_workflow_argument,
    validate_workflow_recording,
)
from repoforge.ports.workflow_replay import WorkflowReplayDecision, WorkflowReplayObservation
from repoforge.testing.fakes import InMemoryLockManager


def _event(
    offset: int,
    *,
    result: WorkflowResultCategory = WorkflowResultCategory.SUCCESS,
    error_code: str | None = None,
    arguments: tuple = (),
) -> WorkflowEvent:
    return WorkflowEvent(
        timestamp_offset_ms=offset,
        tool_inventory_ids=("repo_list", "workspace_status"),
        selected_tool_id="workspace_status",
        arguments=arguments,
        result_category=result,
        stable_error_code=error_code,
        workspace_ref="workspace-1",
        task_ref="task-1",
        snapshot_ref=f"sha256:{'a' * 64}",
        next_action_ids=("review_status",),
        state_transition=WorkflowStateTransition.PROGRESSED,
        arguments_truncated=False,
        result_truncated=False,
    )


def _recording(
    recording_id: str = "wr-000000000000000000000001",
    *,
    created_at: str = "2026-07-14T00:00:00+00:00",
    events: tuple[WorkflowEvent, ...] | None = None,
    final_outcome: WorkflowFinalOutcome = WorkflowFinalOutcome.COMPLETED,
    truncated: bool = False,
    truncation_reason: str | None = None,
) -> WorkflowRecording:
    selected = events or (_event(0),)
    return new_workflow_recording(
        recording_id=recording_id,
        scenario_id="direct-status",
        server_instructions_hash="b" * 64,
        tool_surface_hash="c" * 64,
        capability_flags=("github_read", "local_git_read"),
        events=selected,
        final_outcome=final_outcome,
        metrics=WorkflowMetrics(
            tool_calls=len(selected),
            duration_ms=max(item.timestamp_offset_ms for item in selected),
            retry_count=0,
            error_count=sum(
                item.result_category is not WorkflowResultCategory.SUCCESS for item in selected
            ),
        ),
        created_at=created_at,
        truncated=truncated,
        truncation_reason=truncation_reason,
    )


def test_domain_normalizes_arguments_without_retaining_secret_content_or_paths() -> None:
    summaries = (
        normalize_workflow_argument("api_key", "top-secret"),
        normalize_workflow_argument("prompt", "write the entire source"),
        normalize_workflow_argument("path", "/Users/person/private/repo.py"),
        normalize_workflow_argument("repo_id", "demo"),
        normalize_workflow_argument("count", 12),
    )
    event = _event(0, arguments=summaries)
    recording = _recording(events=(event,))
    encoded = JsonWorkflowRecordingStore.encode_for_test(recording)

    assert b"top-secret" not in encoded
    assert b"write the entire source" not in encoded
    assert b"/Users/person" not in encoded
    assert b"demo" not in encoded
    assert summaries[0].category.value == "omitted_secret"
    assert summaries[1].category.value == "omitted_content"
    assert summaries[2].category.value == "omitted_path"
    normalized_event = recording.events[0]
    assert tuple(item.name for item in normalized_event.arguments) == tuple(
        sorted(item.name for item in normalized_event.arguments)
    )


def test_direct_and_failure_records_are_byte_stable_and_strict(tmp_path: Path) -> None:
    direct = _recording()
    failure_event = _event(
        25,
        result=WorkflowResultCategory.POLICY_ERROR,
        error_code="PATH_DENIED",
    )
    failure = _recording(
        "wr-000000000000000000000002",
        events=(failure_event,),
        final_outcome=WorkflowFinalOutcome.FAILED,
    )

    assert JsonWorkflowRecordingStore.encode_for_test(
        direct
    ) == JsonWorkflowRecordingStore.encode_for_test(direct)
    assert JsonWorkflowRecordingStore.encode_for_test(
        failure
    ) == JsonWorkflowRecordingStore.encode_for_test(failure)
    assert validate_workflow_recording(direct) == direct
    assert validate_workflow_recording(failure) == failure

    store = JsonWorkflowRecordingStore(tmp_path, InMemoryLockManager())
    store.create(direct)
    store.create(failure)
    assert store.read(direct.recording_id) == direct
    assert store.read(failure.recording_id) == failure

    with pytest.raises(RepoForgeError) as bad_error:
        validate_workflow_recording(replace(direct, events=(_event(0, error_code="BAD"),)))
    assert bad_error.value.code is ErrorCode.WORKFLOW_RECORD_INVALID


def test_private_store_round_trips_detects_corruption_and_future_schema(tmp_path: Path) -> None:
    store = JsonWorkflowRecordingStore(tmp_path, InMemoryLockManager())
    recording = _recording()
    assert store.create(recording) == recording
    path = tmp_path / "workflow-recordings" / f"{recording.recording_id}.json"
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert store.read(recording.recording_id) == recording
    assert path.read_bytes() == JsonWorkflowRecordingStore.encode_for_test(recording)

    frame = json.loads(path.read_text(encoding="utf-8"))
    frame["payload_sha256"] = "0" * 64
    path.write_text(json.dumps(frame), encoding="utf-8")
    with pytest.raises(RepoForgeError) as corrupt:
        store.read(recording.recording_id)
    assert corrupt.value.code is ErrorCode.WORKFLOW_RECORD_CORRUPT

    path.write_bytes(JsonWorkflowRecordingStore.encode_for_test(recording))
    frame = json.loads(path.read_text(encoding="utf-8"))
    frame["recording"]["schema_version"] = WORKFLOW_RECORDING_SCHEMA_VERSION + 1
    canonical = json.dumps(
        frame["recording"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    frame["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(frame), encoding="utf-8")
    with pytest.raises(RepoForgeError) as future:
        store.read(recording.recording_id)
    assert future.value.code is ErrorCode.WORKFLOW_RECORD_SCHEMA_UNSUPPORTED


def test_fixture_export_and_retention_are_explicit_and_bounded(tmp_path: Path) -> None:
    store = JsonWorkflowRecordingStore(tmp_path / "state", InMemoryLockManager())
    records = [
        _recording(
            f"wr-{index:024x}",
            created_at=f"2026-07-{10 + index:02d}T00:00:00+00:00",
        )
        for index in range(1, 4)
    ]
    for recording in records:
        store.create(recording)

    exported = store.export_fixture(
        records[-1].recording_id, tmp_path / "fixtures", "scenario.json"
    )
    assert exported.name == "scenario.json"
    assert os.stat(exported).st_mode & 0o777 == 0o600
    assert exported.read_bytes() == JsonWorkflowRecordingStore.encode_for_test(records[-1])

    report = store.prune(
        now="2026-07-14T00:00:00+00:00",
        retention_seconds=3 * 24 * 60 * 60,
        max_records=1,
        max_total_bytes=MAX_WORKFLOW_RECORD_BYTES,
    )
    assert report.remaining_records == 1
    assert store.list_records(max_records=10).records == (records[-1],)


def test_recorder_marks_event_and_size_truncation_and_audits_safe_metadata(
    forge_env: ForgeEnvironment,
) -> None:
    recorder = forge_env.service.application.workflow_recorder
    events = tuple(_event(index) for index in range(MAX_WORKFLOW_EVENTS + 5))
    result = recorder.record(
        WorkflowRecordCommand(
            scenario_id="too-many-events",
            server_instructions_hash="b" * 64,
            tool_surface_hash="c" * 64,
            capability_flags=("local_git_read",),
            events=events,
            final_outcome=WorkflowFinalOutcome.COMPLETED,
            metrics=WorkflowMetrics(len(events), len(events), 0, 0),
        )
    )
    assert result.truncated is True
    assert result.final_outcome is WorkflowFinalOutcome.INCOMPLETE
    assert len(result.events) == MAX_WORKFLOW_EVENTS
    assert result.truncation_reason == "event_limit"

    many_arguments = tuple(
        normalize_workflow_argument(f"arg_{index:02d}", index) for index in range(64)
    )
    large_events = tuple(_event(index, arguments=many_arguments) for index in range(200))
    size_limited = recorder.record(
        WorkflowRecordCommand(
            scenario_id="size-limit",
            server_instructions_hash="b" * 64,
            tool_surface_hash="c" * 64,
            capability_flags=("local_git_read",),
            events=large_events,
            final_outcome=WorkflowFinalOutcome.COMPLETED,
            metrics=WorkflowMetrics(len(large_events), len(large_events), 0, 0),
        )
    )
    assert size_limited.truncated is True
    assert size_limited.truncation_reason == "size_limit"
    assert (
        len(JsonWorkflowRecordingStore.encode_for_test(size_limited)) <= MAX_WORKFLOW_RECORD_BYTES
    )

    audit = (forge_env.root / "state" / "audit.jsonl").read_text(encoding="utf-8")
    assert "too-many-events" in audit
    assert "top-secret" not in audit
    assert "/Users/" not in audit


class _UnsafeReplayAdapter:
    isolated = False
    real_writes_enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def replay_step(self, decision: WorkflowReplayDecision) -> WorkflowReplayObservation:
        self.calls += 1
        return WorkflowReplayObservation(
            result_category=decision.recorded_result_category,
            stable_error_code=decision.recorded_error_code,
            state_transition=decision.recorded_state_transition,
        )


def test_replay_is_deterministic_isolated_and_rejects_real_write_adapters() -> None:
    recording = _recording(
        events=(
            _event(0),
            _event(
                10,
                result=WorkflowResultCategory.PROVIDER_ERROR,
                error_code="COMMAND_FAILED",
            ),
        ),
        final_outcome=WorkflowFinalOutcome.FAILED,
    )
    engine = WorkflowReplayEngine(RecordedCategoryReplayAdapter())
    first = engine.replay(recording)
    second = engine.replay(recording)
    assert first == second
    assert first.eligible_for_eval is True
    assert first.replay_hash == second.replay_hash

    unsafe = _UnsafeReplayAdapter()
    with pytest.raises(RepoForgeError) as rejected:
        WorkflowReplayEngine(unsafe).replay(recording)
    assert rejected.value.code is ErrorCode.WORKFLOW_REPLAY_UNSAFE
    assert unsafe.calls == 0


def test_truncated_recordings_cannot_be_complete_eval_evidence() -> None:
    recording = _recording(
        final_outcome=WorkflowFinalOutcome.INCOMPLETE,
        truncated=True,
        truncation_reason="event_limit",
    )
    engine = WorkflowReplayEngine(RecordedCategoryReplayAdapter())
    with pytest.raises(RepoForgeError) as incomplete:
        engine.replay(recording)
    assert incomplete.value.code is ErrorCode.WORKFLOW_RECORD_INCOMPLETE

    diagnostic = engine.replay(recording, require_complete=False)
    assert diagnostic.eligible_for_eval is False
    assert diagnostic.complete is False
