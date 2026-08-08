# State Concurrency Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the confirmed state races and crash-safety defects while hardening, rather than misclassifying, the investigation's contract hazards and intended saga invariants.

**Architecture:** Add a shared/exclusive collection lock hierarchy, revision-aware workspace updates, pure runtime observation with locked reconciliation, and one interprocess audit transaction. Reuse the repository's secure atomic writer and preserve existing public behavior unless the current behavior is itself destructive.

**Tech Stack:** Python 3.10+, `fcntl`, dataclasses, pathlib, pytest, uv, Ruff, mypy.

## Global Constraints

- Work only in `state-concurrency-harden-2542636120` on `ai/state-concurrency-hardening-2542636120`.
- Write a failing regression test before each production behavior change.
- Keep lock acquisition bounded and preserve the order collection-maintenance → record.
- Reuse `adapters/filesystem/atomic.py`; do not add another private atomic-write variant.
- Do not convert H4 or H8 into bugs without new evidence.
- Do not exceed the repository's 400-line rule for newly created Python files.

---

### Task 1: Collection maintenance lock hierarchy

**Files:**
- Modify: `src/repoforge/ports/locking.py`
- Modify: `src/repoforge/adapters/locking/fcntl.py`
- Modify: `src/repoforge/testing/fakes.py`
- Modify: `src/repoforge/adapters/persistence/json_state_file_store.py`
- Modify: `src/repoforge/adapters/persistence/json_state_lifecycle.py`
- Test: `tests/test_durable_state.py`

**Interfaces:**
- Produces: `LockManager.shared_lock(name, timeout_seconds, metadata)`.
- Produces: canonical collection lock `state-lifecycle-{collection}` used shared by record mutations and exclusively by lifecycle operations.

- [ ] Add a test proving a record save cannot enter while the collection lifecycle lock is held exclusively.
- [ ] Run the focused test and verify it fails because ordinary mutations ignore the collection lock.
- [ ] Add shared locking to production and in-memory lock managers and nest it before record locks.
- [ ] Change recovery to lock by collection, not plan id; verify migration, cleanup, and recovery use the same namespace.
- [ ] Run `tests/test_durable_state.py` and verify it passes.

### Task 2: Revision-aware workspace metadata updates

**Files:**
- Modify: `src/repoforge/ports/workspace_store.py`
- Modify: `src/repoforge/adapters/persistence/json_workspace_store.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/application/workspace/failure_intelligence.py`
- Modify: `src/repoforge/application/workspace/pr.py`
- Test: `tests/test_workspace_kind.py`

**Interfaces:**
- Produces: `WorkspaceStore.update(workspace_id, updater) -> WorkspaceRecord`.
- Persists: private integer `revision`, defaulting legacy records to revision 1.

- [ ] Add a barrier-based test where two independent updates preserve both metadata changes.
- [ ] Run it and verify the current load/mutate/save implementation loses one update.
- [ ] Implement per-workspace locked update and revision persistence with legacy decoding.
- [ ] Migrate confirmed unguarded failure-intelligence and PR-intent RMW paths to `update`.
- [ ] Run focused workspace and PR tests.

### Task 3: Pure runtime reads and locked reconciliation

**Files:**
- Modify: `src/repoforge/adapters/runtime/state_store.py`
- Modify: `src/repoforge/adapters/runtime/local_runtime.py`
- Test: `tests/test_runtime_adapters_and_serve.py`
- Test: `tests/test_mcp_runtime_coverage.py`

**Interfaces:**
- Produces: side-effect-free `read()`/local read helpers.
- Produces: `JsonRuntimeStore.reconcile()` that rereads under an OS lock before clearing or degrading.

- [ ] Change tests to require stale reads to leave durable bytes untouched.
- [ ] Add a replacement-race test showing reconcile never deletes a newer record.
- [ ] Run the tests and verify destructive-read assertions fail.
- [ ] Split parsing/observation from mutation and add locked compare-before-mutate reconciliation.
- [ ] Update explicit cleanup callers to invoke reconciliation where durable repair is desired.
- [ ] Run runtime adapter and control-plane focused tests.

### Task 4: Audit interprocess transaction

**Files:**
- Create: `src/repoforge/adapters/audit/locking.py`
- Modify: `src/repoforge/adapters/audit/jsonl.py`
- Modify: `src/repoforge/adapters/audit/query.py`
- Test: `tests/test_audit_cursor.py`

**Interfaces:**
- Produces: `locked_audit_log(path)` shared by append/rotate/sequence/prune.

- [ ] Add a multi-instance concurrent append test requiring unique contiguous global sequences.
- [ ] Add a prune-write failure test requiring the original log to remain byte-identical.
- [ ] Run both tests and observe duplicate sequence or destructive-prune failure.
- [ ] Lock sequence recovery/allocation, rotation, append, and prune in one interprocess critical section.
- [ ] Replace prune via `atomic_write_text` and run the complete audit cursor tests.

### Task 5: Admission epoch store atomicity

**Files:**
- Modify: `src/repoforge/adapters/persistence/json_admission_epoch.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_lease_registry.py`

**Interfaces:**
- Store mutators serialize through a dedicated epoch-store lock while the existing `WORKER_ADMISSION_LOCK` remains the outer transaction boundary.

- [ ] Add a concurrent two-store permit-claim test requiring exactly one success.
- [ ] Run it and verify both callers can currently claim.
- [ ] Inject/use `LockManager`, lock every epoch RMW mutation, and use the shared atomic writer.
- [ ] Verify registrar/restarter still share the outer admission lock and run lease/activation tests.

### Task 6: Quarantine and local runtime crash safety

**Files:**
- Modify: `src/repoforge/adapters/persistence/json_state_repository.py`
- Modify: `src/repoforge/adapters/runtime/local_runtime.py`
- Test: `tests/test_execution_worker_binding.py`
- Test: `tests/test_mcp_runtime_coverage.py`

**Interfaces:**
- Quarantine performs validated same-filesystem rename and directory fsync.
- Local runtime writers delegate to `atomic_write_text`.

- [ ] Add failure-injection tests proving a failed rename preserves the source and local writers use random exclusive atomic replacement.
- [ ] Run and verify the current copy/unlink and PID-only temp writers fail the tests.
- [ ] Implement `os.replace` quarantine with collision checks and directory fsync.
- [ ] Replace both local runtime writers with the shared atomic helper.
- [ ] Run focused worker-binding and runtime tests.

### Task 7: Idempotency and state-authority API hardening

**Files:**
- Modify: `src/repoforge/ports/idempotency.py`
- Modify: `src/repoforge/adapters/persistence/json_idempotency_store.py`
- Modify: `src/repoforge/application/idempotency.py`
- Modify: `src/repoforge/ports/configuration.py`
- Modify: `src/repoforge/adapters/configuration/generation_store.py`
- Modify: affected configuration call sites and tests.

**Interfaces:**
- Compound idempotency transitions execute through a keyed transaction API.
- Resolved configuration reads select accepted or active authority explicitly.

- [ ] Add contract tests for concurrent idempotency transition serialization and explicit config authority.
- [ ] Run and verify the raw-store/default-authority APIs fail those tests.
- [ ] Add the transaction boundary without nesting the existing keyed lock.
- [ ] Remove the implicit accepted-generation default and update every call site explicitly.
- [ ] Run idempotency, configuration, and strict mypy tests.

### Task 8: Non-bug invariants, call-site audits, and architecture map

**Files:**
- Modify: `AGENTS.md`
- Modify: targeted tests for configuration activation, lock order, fingerprint persistence, and commit reconciliation.

**Interfaces:**
- Documents current domain/ports/adapters/application/interfaces layout.
- Tests H4 write-ahead restart recovery and H8 after-effect reconciliation without changing their classification.

- [ ] Add or strengthen the active-pointer restart invariant test.
- [ ] Audit every `read_fingerprint(..., persist=True)` call and add a guard test that each is under a workspace lock.
- [ ] Add a lock-order test for known nested lock paths; do not claim a deadlock without a cycle.
- [ ] Confirm the commit-after-effect reconcile path; add recovery UX coverage only if missing.
- [ ] Replace the stale `AGENTS.md` repository map with current architecture.
- [ ] Run affected tests, `quick`, `test-affected`, and final reviewed verification.
