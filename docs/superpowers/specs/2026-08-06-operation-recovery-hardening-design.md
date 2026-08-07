# Operation Recovery Hardening Design

**Date:** 2026-08-06  
**Status:** Approved for implementation  
**Scope:** RepoForge durable execution plane, operation recovery, cancellation, and operator repair

## Problem

RepoForge prevents duplicate execution by refusing to requeue work after a child may have started unless the child can be identified and contained. That fail-closed rule is correct, but the current system has no convergent outcome when containment evidence is missing or contradictory:

1. An expired claimed work item with `child_started=true` and no usable worker binding is preserved forever.
2. `operation cancel` records cancellation but cannot terminalize the operation when no cross-process binding can be reaped.
3. The isolated execution worker is considered healthy when its process is alive, even if its single-threaded loop has stopped making progress.
4. `workspace_execute_plan` keeps only an in-memory cancellation token; a restart loses the stage-child identity.
5. Operators can list or cancel operations, but cannot inspect an exact recovery decision or apply a safe repair.

The observed failure mode is a live execution-worker PID, stale operation leases, cancellation-requested operations that remain running for hours, and newly admitted work that stays queued.

## Goals

- Preserve duplicate-execution safety.
- Make every ambiguous recovery state visible with a typed blocker.
- Provide a supported, exact-state-bound operator repair workflow.
- Detect an execution worker that is alive but no longer progressing.
- Persist the currently executing plan-stage child before its launch gate opens.
- Make cancellation converge after the original process has restarted when child identity is available.
- Keep public MCP tool count unchanged; operation repair is an operator CLI workflow.
- Keep all reads and mutations bounded, deterministic, auditable, and redacted.

## Non-goals

- Blindly deleting stale operation records.
- Automatically requeueing work after the child spawn boundary.
- Signalling a PID or process group without PID-reuse-safe identity evidence.
- Adding a generic process-management or shell surface.
- Changing the fixed Forge v2 MCP tool contract.
- Treating a durable operation record as proof that a command or test passed.

## Design principles

### Safety before liveness

A repair may make progress only when it can prove the proposed transition is safe. An unproven child remains blocked. The improvement is that the blocked state becomes inspectable, actionable, and bounded rather than an anonymous `conflicts += 1` loop.

### Preview before apply

Repair is a two-step protocol:

```text
rf operation repair preview OPERATION_ID
rf operation repair apply OPERATION_ID --proposal-token TOKEN
```

The preview returns a deterministic proposal bound to:

- operation ID and `updated_at`;
- operation state, owner, attempt, and lease;
- work-item state, `updated_at`, owner, attempt, lease, and `child_started`;
- worker-binding identity fields when present;
- current process-reaper observation;
- proposed disposition and blocker codes.

The proposal token is a SHA-256 digest of the canonical bounded snapshot. Apply rereads all records, recomputes the proposal, and rejects any mismatch. No stale proposal can mutate newer state.

### Typed dispositions

The classifier returns one of:

- `already_terminal` — no mutation required;
- `cancel_queued` — pending queued work can be cancelled and removed;
- `requeue_unstarted` — expired claimed work that never crossed the spawn boundary can be requeued;
- `cancel_reaped` — cancellation was requested and a matching child was proven gone after reap;
- `orphan_reaped` — lease expired and a matching child was proven gone after reap;
- `blocked_missing_binding` — spawn boundary crossed but no durable child identity exists;
- `blocked_owner_mismatch` — binding owner does not match the work claim;
- `blocked_attempt_mismatch` — binding attempt does not match the work claim;
- `blocked_identity_unproven` — PID/group identity cannot be proven;
- `blocked_child_survived` — the child or one of its group members survived containment;
- `blocked_state_changed` — exact-state validation failed;
- `not_repairable` — the operation is not in a supported repair state.

Blocked dispositions are read-only. They preserve the operation, sidecar, and binding.

## Recovery convergence

`recover_operation_work` will use the same classifier as the operator repair service. Automatic recovery may apply only the same safe automatic actions it already performs:

- delete orphan sidecars for missing/terminal operations;
- cancel queued work;
- requeue expired unstarted work;
- terminalize work after a matching child has been proven contained.

When the classifier blocks, the recovery report records a bounded typed blocker instead of only incrementing a scalar conflict count. It does not delete or requeue the work.

The report adds:

- `blocked` count;
- `blockers`, a deterministic bounded tuple of operation ID, code, and detail;
- existing `conflicts` remains for optimistic-lock races and compatibility.

## Execution-worker progress liveness

### Durable heartbeat

The execution-worker binding gains additive fields:

- `heartbeat_at`;
- `loop_state`: `starting | idle | recovering | claiming | executing | stopping`;
- `current_operation_id`;
- `last_recovery_at`.

Older bindings decode with these fields absent. The child process updates its own binding through an atomic store method. Heartbeats occur:

- after child lease claim and application construction;
- before and after a recovery pass;
- before claim, after no-work, and before executing a claimed operation;
- periodically while a command is running, using the existing operation monitor callback;
- on orderly stop.

Heartbeat writes are bounded and do not contain command output, repository paths, or secrets.

### Supervisor health

The execution-worker client exposes a typed health observation:

- process alive;
- heartbeat available;
- heartbeat age;
- loop state/current operation;
- healthy/stale result and detail.

The supervisor reports separate checks for process liveness and progress liveness. A missing heartbeat receives a short startup grace. A stale heartbeat fails runtime health even when the PID is alive.

The supervisor does not start a competing worker while the stale process remains live. Existing restart/fail-closed policy terminates the stale worker through the group-aware lifecycle path first; only a confirmed terminal lifecycle permits replacement.

## Durable plan-stage child ownership

`WorkspacePlanExecutor` creates a cancellation token with strict spawn and bind observers for the parent plan operation:

1. Before Popen, validate that the parent operation is still running and cancellation has not been requested.
2. After Popen and before the launch gate opens, persist `OperationWorkerBinding(operation_id=parent_plan_operation_id, ...)`.
3. On stage completion or failure, delete that exact binding with `delete_if_unchanged`.
4. On process restart, generic operation cancellation can use the durable binding and process reaper to terminate the active stage child and terminalize the parent plan.

Only one plan stage executes at a time, so one binding per parent operation is sufficient. Cache-hit stages spawn no child and create no binding.

## Operator repair service

New modules:

- `domain/operation_repair.py` — immutable proposal, blocker, disposition, canonical digest;
- `application/operations/repair.py` — inspect/classify/apply with exact-state validation;
- CLI wiring under `rf operation repair preview|apply`.

Apply semantics:

- `already_terminal`: idempotent no-op;
- `cancel_queued`: transition to cancelled, delete work sidecar;
- `requeue_unstarted`: CAS-save requeued work, transition operation to pending;
- `cancel_reaped`: transition to cancelled, delete work and exact binding;
- `orphan_reaped`: transition to orphaned with `OPERATION_WORKER_LOST`, delete work and exact binding;
- any blocked/not-repairable disposition: reject without mutation with an actionable typed error.

Every apply is audited with identifiers, disposition, blocker codes, and snapshot digests only. No process output or file content is logged.

## Evidence truth

- A running/pending operation remains `evidence_complete=false`.
- A cancelled operation is complete only after the state transition is durably recorded.
- A failed/orphaned operation requires an error code.
- A successful operation requires an available result payload.
- Repair output never labels a blocked or incomplete operation as passed or verified.
- Runtime and CLI output explicitly distinguish `blocked`, `repaired`, `already_terminal`, and `stale_proposal`.

## Compatibility

- Execution-worker binding additions are optional on decode and emitted on new writes.
- Existing operation/work/binding stores remain readable.
- Existing operation status/list/cancel behavior remains stable.
- Existing `conflicts` report field remains; new blocker evidence is additive.
- No MCP schema or tool inventory changes.

## Test strategy

### Repair classifier and apply

- terminal no-op;
- queued cancellation;
- expired unstarted requeue;
- matching live child reaped then cancelled/orphaned;
- missing binding, owner mismatch, attempt mismatch, PID reuse, leaderless group, and survived SIGKILL all block without mutation;
- stale operation/work/binding snapshot rejects apply;
- repeat apply is idempotent where safe;
- audit payload contains no sensitive data.

### Recovery

- existing safe paths remain green;
- ambiguous containment returns typed blockers and preserves durable state;
- blocker collection is deterministic and bounded;
- scan truncation remains fail-closed.

### Worker liveness

- old binding payload decodes;
- heartbeat update uses CAS and preserves identity;
- supervisor accepts fresh heartbeat;
- stale heartbeat makes health fail while PID remains alive;
- generation/dead-process restart behavior remains unchanged;
- stale live worker is terminated before replacement.

### Execute plan

- binding is persisted before command launch;
- binding is removed after stage success/failure;
- cross-process cancellation reaps a real stage child and terminalizes parent plan;
- binding failure prevents launch;
- cache-hit stages do not create bindings.

## Rollout

1. Ship tests and typed repair classifier.
2. Add operator CLI preview/apply.
3. Add recovery blocker evidence.
4. Add plan-stage durable binding.
5. Add execution-worker heartbeat and supervisor health.
6. Run affected tests, strict typing/lint/build, then the authoritative full gate before publication.

No automatic repair is enabled for unproven child identity at any stage.
