# Operation Recovery Hardening Implementation Plan

> Execute each task test-first. Do not implement a production behavior until its focused test has been observed failing for the expected reason.

**Goal:** Make RepoForge's durable execution plane converge safely after worker loss, expose exact-state operator repair, detect alive-but-stuck execution workers, and make plan-stage cancellation crash-safe.

**Architecture:** A shared pure repair classifier converts operation/work/binding/reaper evidence into a typed disposition. Automatic recovery and operator repair consume the same decision model. The execution worker publishes progress heartbeat into its existing durable binding. Plan stages use the existing gated cancellation token to persist parent-operation child identity before command launch.

**Tech stack:** Python 3.10+, dataclasses, existing JSON durable-state repositories, existing process reaper, pytest, strict mypy, ruff.

---

## Task 1 — Typed operation-repair domain model

**Files:**
- Create: `src/repoforge/domain/operation_repair.py`
- Create: `tests/test_operation_repair.py`
- Modify: `tests/coverage-map.json`
- Modify: `tests/test-groups.toml` only if the new test is not already covered by an existing operations group glob.

**RED:** Add tests for canonical proposal digest, deterministic blocker ordering, bounded details, and stale-token mismatch.

**GREEN:** Implement immutable disposition/blocker/proposal models and canonical SHA-256 token generation. No I/O in the domain module.

**Verify:** `uv run pytest tests/test_operation_repair.py -q`

## Task 2 — Shared repair classifier and exact-state apply service

**Files:**
- Create: `src/repoforge/application/operations/repair.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `tests/test_operation_repair.py`
- Modify coverage mapping as required.

**RED:** Cover terminal no-op, queued cancel, expired unstarted requeue, reaped cancel/orphan, missing binding, owner mismatch, attempt mismatch, unproven identity, survived child, and stale operation/work/binding snapshots.

**GREEN:** Implement preview and apply. Apply must recompute the exact proposal token and reject blocked or changed state without mutation. Reuse `OperationManager`, `OperationWorkQueue`, `WorkerBindingStore`, and `ProcessReaper`.

**Verify:** focused repair tests.

## Task 3 — Operator CLI repair workflow

**Files:**
- Modify: `src/repoforge/interfaces/cli/main.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `tests/test_cli.py` or the existing operation CLI test file
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/operations/OPERATIONS.md` or the closest existing operation runbook.

**RED:** Parse and dispatch `rf operation repair preview ID` and `apply ID --proposal-token TOKEN`. Assert blocked apply exits non-zero and preview is read-only.

**GREEN:** Thin CLI adapters only; transition policy remains in the repair service. Do not add an MCP tool.

**Verify:** focused CLI tests plus repair tests.

## Task 4 — Recovery blocker evidence

**Files:**
- Modify: `src/repoforge/application/operations/recovery.py`
- Modify: `tests/test_operation_execution_worker.py`
- Modify: `src/repoforge/application/operations/repair.py`

**RED:** Replace the anonymous-conflict expectation for missing binding with typed blocker evidence while asserting the operation/work state remains unchanged. Add owner/attempt/unproven/survived blocker cases and deterministic bounding.

**GREEN:** Route containment failures through the shared classifier. Preserve `conflicts` for actual CAS conflicts; add `blocked` and bounded blocker records to `OperationWorkRecoveryReport`.

**Verify:** `tests/test_operation_execution_worker.py` focused selectors and repair tests.

## Task 5 — Durable execute-plan stage binding

**Files:**
- Modify: `src/repoforge/application/workspace/execute_plan.py`
- Modify: `src/repoforge/ports/cancellation.py` only if a release observer is required.
- Modify: `tests/test_workspace_execution_plan.py` or the current plan execution test file.

**RED:** Assert binding exists before a real stage command is released, is removed after success/failure, bind failure prevents launch, and a restarted service can cancel/reap the stage child via the parent operation ID.

**GREEN:** Add strict `on_spawn`/`on_bind` observers to the parent plan token. Persist one exact `OperationWorkerBinding`; remove it with `delete_if_unchanged` in stage-finally paths. Reuse generic `OperationCancellationRequester` fallback.

**Verify:** focused plan execution and cancellation tests.

## Task 6 — Execution-worker durable progress heartbeat

**Files:**
- Modify: `src/repoforge/domain/execution_worker.py`
- Modify: `src/repoforge/ports/execution_worker_store.py`
- Modify: `src/repoforge/adapters/persistence/json_execution_worker_binding_store.py`
- Modify: `src/repoforge/application/operations/work_loop.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_execution_worker_binding.py`
- Modify: `tests/test_operation_execution_worker.py`

**RED:** Old payload compatibility, heartbeat CAS update, state/current-operation transitions, and bounded fields.

**GREEN:** Add optional heartbeat fields and an atomic store heartbeat method. Pass a heartbeat callback into `OperationWorkLoop`; emit starting/idle/recovering/claiming/executing/stopping observations without sensitive payloads.

**Verify:** focused binding/store/work-loop tests.

## Task 7 — Supervisor progress-liveness health

**Files:**
- Modify: `src/repoforge/ports/execution_worker.py`
- Modify: `src/repoforge/adapters/runtime/execution_worker.py`
- Modify: `src/repoforge/application/runtime/supervisor.py`
- Modify: `tests/test_phase4_runtime_control.py`
- Modify: runtime health/runbook documentation.

**RED:** Fresh heartbeat healthy; stale heartbeat unhealthy while PID is alive; startup grace; stale worker terminated before replacement; existing generation/death restart remains unchanged.

**GREEN:** Add typed health observation to the worker client. Supervisor records separate process/progress checks and never starts a replacement until stale-worker termination is confirmed by existing lifecycle machinery.

**Verify:** focused runtime-control and execution-worker tests.

## Task 8 — Evidence and documentation consistency

**Files:**
- Modify: operation DTO/tests only where additive repair status is exposed.
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: relevant operations/runtime runbooks
- Modify: `CHANGELOG.md`

**RED:** Contract/document drift tests for the new CLI and fields.

**GREEN:** Document exact meanings of blocked, repaired, evidence-complete, heartbeat stale, and safe operator actions. Never describe an incomplete durable record as a passed command.

**Verify:** documentation drift tests.

## Task 9 — Verification and publication

1. Format only changed files with the reviewed formatter.
2. Run focused tests for each task.
3. Run the affected-test selector (`make test` / RepoForge `test-affected`).
4. Run lint, strict mypy, build, and the repository `verify` profile.
5. Because this is runtime/state/cancellation safety work, run the authoritative full gate once before publication.
6. Inspect the complete diff and ensure no file exceeds repository policy without an approved exception.
7. Commit the exact verified tree.
8. Push the new `ai/*` branch and create a draft PR with failure-mode, invariants, tests, and rollout notes.

If the managed execution worker prevents verification from running, do not fabricate evidence or commit as verified. Record the durable blocker and leave the branch/worktree intact for operator runtime recovery.
