"""Coverage for TaskCapsule v2 (#208): identity, path_scope, instructions, overrides,
delivery state, and lease fields."""

from __future__ import annotations

import pytest

from repoforge.domain.rules_engine import OverridePolicy, OverrideRejectedError
from repoforge.domain.task_capsule import (
    LOCAL_OPERATOR_PRINCIPAL,
    InstructionOrigin,
    RecordedBy,
    TaskCapsule,
    TrustLevel,
    acquire_lease,
    add_instruction,
    add_override,
    escalate_rule,
    record_guide_delivered,
    record_mutation,
    release_lease,
)


def _task() -> TaskCapsule:
    return TaskCapsule.new(
        task_id="task-" + "a" * 24,
        intent="do the thing",
        acceptance_criteria=("it works",),
        constraints=(),
        repo_ids=("demo",),
        created_at="2026-07-18T00:00:00+00:00",
        path_scope=("src/**",),
    )


def test_new_task_defaults_to_the_single_local_operator_principal() -> None:
    task = _task()
    assert task.principal == LOCAL_OPERATOR_PRINCIPAL
    assert task.path_scope == ("src/**",)
    assert task.task_revision == 1
    assert task.instructions == ()
    assert task.overrides == ()
    assert task.guides_delivered == ()
    assert task.escalated_rules == ()
    assert task.mutation_count == 0
    assert task.lease_holder is None


def test_add_instruction_bumps_task_revision_and_records_dual_provenance() -> None:
    task = _task()
    updated = add_instruction(
        task,
        instruction_id="instr-1",
        content="use dataclass immutability",
        asserted_origin=InstructionOrigin.USER,
        recorded_by=RecordedBy.MODEL,
        trust=TrustLevel.RELAYED_UNVERIFIED,
        updated_at="2026-07-18T00:01:00+00:00",
    )
    assert updated.task_revision == 2
    assert len(updated.instructions) == 1
    instruction = updated.instructions[0]
    assert instruction.asserted_origin is InstructionOrigin.USER
    assert instruction.recorded_by is RecordedBy.MODEL
    assert instruction.trust is TrustLevel.RELAYED_UNVERIFIED
    assert instruction.revision == 2
    # Original capsule is untouched (pure/immutable).
    assert task.task_revision == 1
    assert task.instructions == ()


def test_add_override_rejected_when_policy_is_never() -> None:
    task = _task()
    with pytest.raises(OverrideRejectedError):
        add_override(
            task,
            override_id="ov-1",
            rule_id="rule.x",
            override_policy=OverridePolicy.NEVER,
            scope=("src/**",),
            reason="prototype",
            actor="operator",
            updated_at="2026-07-18T00:01:00+00:00",
        )
    assert task.overrides == ()  # rejected attempt never partially lands


def test_add_override_accepted_when_policy_is_task_and_records_scope_reason_actor() -> None:
    task = _task()
    updated = add_override(
        task,
        override_id="ov-1",
        rule_id="rule.x",
        override_policy=OverridePolicy.TASK,
        scope=("src/**",),
        reason="prototype, relax new_dependency",
        actor="operator",
        updated_at="2026-07-18T00:01:00+00:00",
    )
    assert len(updated.overrides) == 1
    override = updated.overrides[0]
    assert override.rule_id == "rule.x"
    assert override.scope == ("src/**",)
    assert override.reason == "prototype, relax new_dependency"
    assert override.actor == "operator"
    assert updated.task_revision == 2


def test_add_override_accepted_when_policy_is_approval() -> None:
    task = _task()
    updated = add_override(
        task,
        override_id="ov-1",
        rule_id="rule.x",
        override_policy=OverridePolicy.APPROVAL,
        scope=(),
        reason="approved change",
        actor="operator",
        updated_at="2026-07-18T00:01:00+00:00",
    )
    assert len(updated.overrides) == 1


def test_record_guide_delivered_is_idempotent() -> None:
    task = _task()
    once = record_guide_delivered(task, "guide-1", updated_at="2026-07-18T00:01:00+00:00")
    twice = record_guide_delivered(once, "guide-1", updated_at="2026-07-18T00:02:00+00:00")
    assert once.guides_delivered == ("guide-1",)
    assert twice.guides_delivered == ("guide-1",)
    assert twice.updated_at == "2026-07-18T00:01:00+00:00"  # no-op: no new updated_at stamp


def test_escalate_rule_is_idempotent_and_scoped_to_this_capsule() -> None:
    task = _task()
    escalated = escalate_rule(task, "rule.x", updated_at="2026-07-18T00:01:00+00:00")
    escalated_again = escalate_rule(escalated, "rule.x", updated_at="2026-07-18T00:02:00+00:00")
    assert escalated.escalated_rules == ("rule.x",)
    assert escalated_again.escalated_rules == ("rule.x",)

    fresh_task = _task()
    assert fresh_task.escalated_rules == ()  # escalation dies with the task


def test_record_mutation_increments_counter() -> None:
    task = _task()
    once = record_mutation(task, updated_at="2026-07-18T00:01:00+00:00")
    twice = record_mutation(once, updated_at="2026-07-18T00:02:00+00:00")
    assert once.mutation_count == 1
    assert twice.mutation_count == 2


def test_lease_acquire_and_release() -> None:
    task = _task()
    leased = acquire_lease(
        task,
        holder="foreman-1",
        expires_at="2026-07-18T01:00:00+00:00",
        updated_at="2026-07-18T00:01:00+00:00",
    )
    assert leased.lease_holder == "foreman-1"
    assert leased.lease_expires_at == "2026-07-18T01:00:00+00:00"

    released = release_lease(leased, updated_at="2026-07-18T00:02:00+00:00")
    assert released.lease_holder is None
    assert released.lease_expires_at is None


def test_lease_acquire_rejects_a_different_holder_while_held() -> None:
    task = _task()
    leased = acquire_lease(
        task,
        holder="foreman-1",
        expires_at="2026-07-18T01:00:00+00:00",
        updated_at="2026-07-18T00:01:00+00:00",
    )
    with pytest.raises(ValueError):
        acquire_lease(
            leased,
            holder="foreman-2",
            expires_at="2026-07-18T01:00:00+00:00",
            updated_at="2026-07-18T00:02:00+00:00",
        )


def test_resume_projection_includes_instructions_and_overrides() -> None:
    task = _task()
    task = add_instruction(
        task,
        instruction_id="instr-1",
        content="prefer exact edits",
        asserted_origin=InstructionOrigin.USER,
        recorded_by=RecordedBy.MODEL,
        trust=TrustLevel.RELAYED_UNVERIFIED,
        updated_at="2026-07-18T00:01:00+00:00",
    )
    task = add_override(
        task,
        override_id="ov-1",
        rule_id="rule.x",
        override_policy=OverridePolicy.TASK,
        scope=(),
        reason="reason",
        actor="operator",
        updated_at="2026-07-18T00:02:00+00:00",
    )
    projection = task.resume_projection()
    assert projection["principal"] == LOCAL_OPERATOR_PRINCIPAL
    assert projection["path_scope"] == ["src/**"]
    assert projection["task_revision"] == 3
    assert len(projection["instructions"]) == 1
    assert projection["instructions"][0]["asserted_origin"] == "user"
    assert projection["instructions"][0]["trust"] == "relayed_unverified"
    assert len(projection["overrides"]) == 1
    assert projection["overrides"][0]["rule_id"] == "rule.x"
