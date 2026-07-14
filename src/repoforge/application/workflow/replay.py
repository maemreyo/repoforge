"""Deterministic workflow replay through isolated no-real-write adapters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.workflow_recording import (
    WorkflowFinalOutcome,
    WorkflowRecording,
    WorkflowResultCategory,
    WorkflowStateTransition,
    validate_workflow_recording,
)
from ...ports.workflow_replay import (
    WorkflowReplayAdapter,
    WorkflowReplayDecision,
    WorkflowReplayObservation,
)

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class WorkflowReplayStepResult:
    event_index: int
    selected_tool_id: str
    result_category: str
    stable_error_code: str | None
    state_transition: str
    matches_recording: bool


@dataclass(frozen=True, slots=True)
class WorkflowReplayResult:
    recording_id: str
    scenario_id: str
    steps: tuple[WorkflowReplayStepResult, ...]
    final_outcome: str
    complete: bool
    eligible_for_eval: bool
    matches_recording: bool
    replay_hash: str


class RecordedCategoryReplayAdapter:
    """Replay only recorded categories; perform no filesystem, GitHub, network, or subprocess work."""

    isolated = True
    real_writes_enabled = False

    def replay_step(self, decision: WorkflowReplayDecision) -> WorkflowReplayObservation:
        return WorkflowReplayObservation(
            result_category=decision.recorded_result_category,
            stable_error_code=decision.recorded_error_code,
            state_transition=decision.recorded_state_transition,
        )


class WorkflowReplayEngine:
    def __init__(self, adapter: WorkflowReplayAdapter):
        self.adapter = adapter

    def _assert_isolated(self) -> None:
        if not bool(getattr(self.adapter, "isolated", False)) or bool(
            getattr(self.adapter, "real_writes_enabled", True)
        ):
            raise RepoForgeError(
                "workflow replay adapter is not isolated from real writes",
                code=ErrorCode.WORKFLOW_REPLAY_UNSAFE,
                safe_next_action="Use an isolated adapter with real_writes_enabled=false.",
            )

    @staticmethod
    def _validate_observation(observation: WorkflowReplayObservation) -> None:
        if not isinstance(observation.result_category, WorkflowResultCategory):
            raise RepoForgeError(
                "workflow replay adapter returned an invalid result category",
                code=ErrorCode.WORKFLOW_REPLAY_UNSAFE,
            )
        if not isinstance(observation.state_transition, WorkflowStateTransition):
            raise RepoForgeError(
                "workflow replay adapter returned an invalid state transition",
                code=ErrorCode.WORKFLOW_REPLAY_UNSAFE,
            )
        if observation.result_category is WorkflowResultCategory.SUCCESS:
            if observation.stable_error_code is not None:
                raise RepoForgeError(
                    "successful workflow replay observation contains an error code",
                    code=ErrorCode.WORKFLOW_REPLAY_UNSAFE,
                )
        elif (
            not isinstance(observation.stable_error_code, str)
            or _ERROR_CODE.fullmatch(observation.stable_error_code) is None
        ):
            raise RepoForgeError(
                "failed workflow replay observation lacks a valid stable error code",
                code=ErrorCode.WORKFLOW_REPLAY_UNSAFE,
            )

    def replay(
        self,
        recording: WorkflowRecording,
        *,
        require_complete: bool = True,
    ) -> WorkflowReplayResult:
        normalized = validate_workflow_recording(recording)
        self._assert_isolated()
        if normalized.truncated and require_complete:
            raise RepoForgeError(
                "truncated workflow recording cannot be complete evaluation evidence",
                code=ErrorCode.WORKFLOW_RECORD_INCOMPLETE,
                safe_next_action="Replay diagnostically with require_complete=false or capture a complete record.",
            )

        results: list[WorkflowReplayStepResult] = []
        for index, event in enumerate(normalized.events):
            decision = WorkflowReplayDecision(
                event_index=index,
                selected_tool_id=event.selected_tool_id,
                tool_inventory_ids=event.tool_inventory_ids,
                arguments=event.arguments,
                recorded_result_category=event.result_category,
                recorded_error_code=event.stable_error_code,
                recorded_state_transition=event.state_transition,
                workspace_ref=event.workspace_ref,
                task_ref=event.task_ref,
                snapshot_ref=event.snapshot_ref,
                next_action_ids=event.next_action_ids,
            )
            observation = self.adapter.replay_step(decision)
            self._validate_observation(observation)
            matches = (
                observation.result_category is event.result_category
                and observation.stable_error_code == event.stable_error_code
                and observation.state_transition is event.state_transition
            )
            results.append(
                WorkflowReplayStepResult(
                    event_index=index,
                    selected_tool_id=event.selected_tool_id,
                    result_category=observation.result_category.value,
                    stable_error_code=observation.stable_error_code,
                    state_transition=observation.state_transition.value,
                    matches_recording=matches,
                )
            )

        payload = {
            "recording_id": normalized.recording_id,
            "scenario_id": normalized.scenario_id,
            "steps": [
                {
                    "event_index": item.event_index,
                    "selected_tool_id": item.selected_tool_id,
                    "result_category": item.result_category,
                    "stable_error_code": item.stable_error_code,
                    "state_transition": item.state_transition,
                    "matches_recording": item.matches_recording,
                }
                for item in results
            ],
            "final_outcome": normalized.final_outcome.value,
            "complete": not normalized.truncated,
        }
        replay_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        all_match = all(item.matches_recording for item in results)
        complete = not normalized.truncated
        return WorkflowReplayResult(
            recording_id=normalized.recording_id,
            scenario_id=normalized.scenario_id,
            steps=tuple(results),
            final_outcome=(
                WorkflowFinalOutcome.INCOMPLETE.value
                if normalized.truncated
                else normalized.final_outcome.value
            ),
            complete=complete,
            eligible_for_eval=complete,
            matches_recording=all_match,
            replay_hash=replay_hash,
        )
