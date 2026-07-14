"""Isolation boundary for deterministic workflow replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.workflow_recording import (
    WorkflowArgumentSummary,
    WorkflowResultCategory,
    WorkflowStateTransition,
)


@dataclass(frozen=True, slots=True)
class WorkflowReplayDecision:
    event_index: int
    selected_tool_id: str
    tool_inventory_ids: tuple[str, ...]
    arguments: tuple[WorkflowArgumentSummary, ...]
    recorded_result_category: WorkflowResultCategory
    recorded_error_code: str | None
    recorded_state_transition: WorkflowStateTransition
    workspace_ref: str | None
    task_ref: str | None
    snapshot_ref: str | None
    next_action_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowReplayObservation:
    result_category: WorkflowResultCategory
    stable_error_code: str | None
    state_transition: WorkflowStateTransition


class WorkflowReplayAdapter(Protocol):
    isolated: bool
    real_writes_enabled: bool

    def replay_step(self, decision: WorkflowReplayDecision) -> WorkflowReplayObservation: ...
