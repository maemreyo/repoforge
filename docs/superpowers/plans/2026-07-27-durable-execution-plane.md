# Durable Execution Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make every workspace verification a durable, restart-safe operation that never executes repository commands on an MCP request thread.

**Architecture:** Persist a schema-versioned work envelope beside the existing operation record, claim it through a compare-and-swap queue, and execute it in a dedicated managed-runtime worker process. Existing profile/ad-hoc runners become internal synchronous handlers; the public verifier always admits durable work and optionally performs a bounded long-poll.

**Tech Stack:** Python 3.12, immutable dataclasses, JSON/fsync/Fcntl persistence, existing `OperationManager`, `ExecutionCoordinator`, pytest, Ruff, strict Mypy.

## Global Constraints

- Keep the existing static 28-tool surface; all contract changes are additive.
- Do not add a database, external queue, shell execution, credentials, mounts, or network capability.
- Never auto-retry mutating work after an ambiguous child-process outcome.
- Persist no source bodies, secrets, raw environment values, or unbounded command output.
- Bind every work item to exact head SHA, workspace fingerprint, and active configuration generation.
- A terminal operation must have a terminal phase and either a result reference or explicit evidence-incomplete status.
- Preserve legacy direct runner calls for internal tests while routing the public `workspace_verify` surface through durable admission.

---

## File Structure

**Create**

- `src/repoforge/domain/operation_work.py` — immutable work envelope, validation, serialization and claim/lease transitions.
- `src/repoforge/ports/operation_work_queue.py` — persistence boundary and bounded page/claim result types.
- `src/repoforge/adapters/operation_work_queue.py` — atomic JSON/Fcntl implementation.
- `src/repoforge/application/operations/work_admission.py` — recoverable operation/work-item admission protocol.
- `src/repoforge/application/operations/work_executor.py` — typed handler registry and one-attempt execution lifecycle.
- `src/repoforge/application/operations/work_loop.py` — polling, claim, heartbeat and shutdown loop.
- `src/repoforge/interfaces/runtime/execution_worker.py` — dedicated worker CLI entry point.
- `tests/test_operation_work_queue.py` — domain and adapter tests.
- `tests/test_durable_verification_dispatch.py` — public admission/long-poll tests.
- `tests/test_operation_execution_worker.py` — claim, restart, cancellation and duplicate-worker tests.
- `tests/test_control_plane_responsiveness.py` — blocked-command/control-plane isolation regression.

**Modify**

- `src/repoforge/domain/operation_task.py` — terminal phase validation and queued/cancelling phase helpers without expanding the public state enum.
- `src/repoforge/ports/__init__.py` — export the new queue port.
- `src/repoforge/adapters/__init__.py` — export the JSON queue adapter.
- `src/repoforge/application/context.py` — add `operation_work_queue` to application context.
- `src/repoforge/application/operations/manager.py` — phase-aware pending cancellation and terminalization.
- `src/repoforge/application/operations/recovery.py` — reconcile orphan work records and safely requeue never-started claims.
- `src/repoforge/application/workspace/run_profile.py` — expose an internal synchronous handler and remove daemon-thread ownership.
- `src/repoforge/application/workspace/run_adhoc.py` — expose an internal synchronous handler and remove daemon-thread ownership.
- `src/repoforge/application/workspace/verify.py` — durable admission for all executable modes plus bounded wait compatibility.
- `src/repoforge/application/service.py` — construct work handlers and route cancellation through the durable queue/worker binding.
- `src/repoforge/bootstrap.py` — construct stores, recover work, and wire the worker entry point.
- `src/repoforge/adapters/runtime/supervisor.py` — supervise the execution worker separately from the MCP/tunnel child.
- `src/repoforge/interfaces/runtime/worker.py` — pass the execution-worker command to the supervisor.
- `src/repoforge/contracts/common.py` — expose attempt, heartbeat and evidence-completeness fields additively.
- `tests/test_background_run_profile.py` — replace daemon-closure assumptions with durable queue assertions.
- `tests/test_operation_tasks.py` — enforce truthful terminal phases.
- generated contract artifacts via `make schemas`.

---

## Deviations from this plan as written

Recorded during implementation; each is a deliberate choice, not an omission.

- **Queue adapter path.** The JSON queue lives at
  `src/repoforge/adapters/persistence/json_operation_work_queue.py` and is exported from
  `adapters/persistence/__init__.py`, matching every other store in this repository, rather
  than the flat `src/repoforge/adapters/operation_work_queue.py` this plan named.
- **Supervisor path.** The execution worker is supervised from
  `src/repoforge/application/runtime/supervisor.py` (where `RuntimeSupervisor` actually lives)
  and launched by `adapters/runtime/execution_worker.py` behind the new
  `ports/execution_worker.py` boundary. `interfaces/runtime/worker.py` was not touched; the
  worker command is wired through `bootstrap.run_runtime_worker`.
- **Progress and supervisor test placement.** Task 9's progress-truth assertions live in
  `tests/test_operation_observability.py` (attempt, heartbeat, evidence completeness) and
  `tests/test_background_run_profile.py` (current step name and heartbeat re-emission);
  Task 7's supervisor assertions live in `tests/test_phase4_runtime_control.py` beside the
  existing runtime-control coverage. No `tests/test_runtime_supervisor.py` was added.
- **Legacy in-process background execution is retained.** The public `workspace_verify`
  surface is fully durable, and no MCP tool reaches the daemon-thread path any more. The
  direct `WorkspaceProfileRunner`/`WorkspaceAdhocRunner` background entry points are kept
  for internal callers and tests, as the Global Constraints above require. Removing them is
  a separate change; recovery no longer assumes every execution has a work sidecar.
- **`make format` and `make check-generated` do not exist.** The equivalent real gates are
  `make lint`, `make typecheck`, `make schemas` (which reports `changed_paths` and the tool
  count) and `make test-groups-check`.
- **A foreground durable failure is re-raised, not flattened into a verdict.** Moving the
  command off the request thread moved validation refusals into the worker, and the first
  cut reported them as `outcome="failed"` with the typed code and message reachable only
  through a second `operation` call. `WorkspaceVerifier._raise_terminal_failure` now
  re-raises a terminal FAILED/ORPHANED/CANCELLED operation with its code, message,
  retryability and operation id, preserving the pre-durable contract exactly: a command
  that fails still returns a result, a refusal still raises. `background=true` never raises.
- **Existing `workspace_verify` tests were reconciled, not weakened.** Tests that
  monkeypatched a runner and asserted the forwarded command now assert the admitted
  `OperationWorkRequest` -- the reviewed boundary durable admission actually persists --
  and tests that need real command output run against a live worker through the new
  `conftest.durable_worker` helper.
- **Shutdown reaping is decided by ownership, not by kind.** Exempting every verification
  kind from the graceful-shutdown sweep also exempted legacy in-process runs whose children
  die with the MCP process, which is the leak that sweep exists to prevent. Work with a
  durable sidecar is left to its execution worker; everything else is still reaped.
- **Read-only retry-once after a known-dead child is not implemented.** The spec words it as
  "may", and no acceptance criterion depends on it. Recovery requeues only work that never
  started a child and orphans everything ambiguous, which satisfies "a lost lease before
  child spawn requeues automatically" and "ambiguous mutating work never auto-retries".
- **`workspace_status` on a workspace with a running command was still serialized** behind
  that workspace's lock (~61s, measured). It was deferred out of this plan and fixed
  separately, because it needed its own decision about what a status read means mid-mutation
  plus an additive contract field: a status read now waits at most
  `server.status_read_lock_timeout_seconds` and reports `read_consistency` as `locked` or
  `concurrent_write`, and an unsynchronized read never writes its fingerprint back to the
  shared cache. `tests/test_control_plane_responsiveness.py` covers both the idle-bystander
  and busy-workspace reads.

---

### Task 1: Typed durable work envelope

**Files:**
- Create: `src/repoforge/domain/operation_work.py`
- Test: `tests/test_operation_work_queue.py`

**Interfaces:**
- Produces: `OperationWorkKind`, `OperationWorkState`, `OperationWorkRequest`, `OperationWorkItem`, `new_work_item()`, `claim_work_item()`, `renew_work_lease()`, `requeue_unstarted_work()`, `complete_work_item()`, `work_item_payload()`, and `work_item_from_payload()`.
- Consumes: `validate_operation_id()` and `RepoForgeError` from the existing operation domain.

- [x] **Step 1: Write failing domain tests**

```python
def test_new_work_item_is_queued_and_exact_state_bound():
    item = new_work_item(
        operation_id="op-" + "a" * 24,
        request=OperationWorkRequest.profile(
            workspace_id="ws-1",
            profile_name="full",
            expected_head_sha="b" * 40,
            expected_fingerprint="c" * 64,
            config_generation=12,
        ),
        now="2026-07-27T00:00:00+00:00",
    )
    assert item.state is OperationWorkState.QUEUED
    assert item.attempt == 0
    assert item.owner_id is None


def test_claim_requires_cas_and_requeue_rejects_started_child():
    claimed = claim_work_item(item, owner_id="worker-1", lease_expires_at=LEASE, now=NOW)
    with pytest.raises(RepoForgeError, match="started work cannot be requeued"):
        requeue_unstarted_work(replace(claimed, child_started=True), now=LATER)
```

- [x] **Step 2: Run the tests and confirm RED**

Run: `uv run pytest -q tests/test_operation_work_queue.py`

Expected: collection fails because `repoforge.domain.operation_work` does not exist.

- [x] **Step 3: Implement the immutable schema and transitions**

```python
class OperationWorkState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"

@dataclass(frozen=True, slots=True)
class OperationWorkRequest:
    kind: Literal["profile", "adhoc"]
    workspace_id: str
    expected_head_sha: str
    expected_fingerprint: str
    config_generation: int
    profile_name: str | None = None
    argv: tuple[str, ...] = ()
    working_directory: str | None = None
    mutability: Literal["read_only", "may_write"] = "read_only"

@dataclass(frozen=True, slots=True)
class OperationWorkItem:
    operation_id: str
    request: OperationWorkRequest
    state: OperationWorkState
    attempt: int
    max_attempts: int
    owner_id: str | None
    lease_expires_at: str | None
    child_started: bool
    created_at: str
    updated_at: str
    available_at: str
    result_reference: str | None = None
    schema_version: int = 1
```

Validation rejects unknown fields, non-exact SHA/fingerprint lengths, unsupported kinds, unbounded argv and control characters. Serialization round-trips every field and rejects future schema versions.

- [x] **Step 4: Run domain tests and confirm GREEN**

Run: `uv run pytest -q tests/test_operation_work_queue.py -k 'new_work_item or claim or payload'`

Expected: all selected tests pass.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/domain/operation_work.py tests/test_operation_work_queue.py
git commit -m "feat(operations): define durable work envelopes"
```

### Task 2: Atomic JSON work queue

**Files:**
- Create: `src/repoforge/ports/operation_work_queue.py`
- Create: `src/repoforge/adapters/operation_work_queue.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/__init__.py`
- Test: `tests/test_operation_work_queue.py`

**Interfaces:**
- Produces: `OperationWorkQueue.create`, `read`, `save`, `claim_next`, `list_records`, and `delete`.
- `claim_next(*, owner_id: str, now: str, lease_expires_at: str, compatible_kinds: frozenset[str]) -> OperationWorkItem | None` performs claim and persistence under one cross-process lock.

- [x] **Step 1: Add failing persistence/race tests**

```python
def test_two_queue_instances_have_one_claim_winner(tmp_path):
    first = JsonOperationWorkQueue(tmp_path, FcntlLockManager(tmp_path / "locks"))
    second = JsonOperationWorkQueue(tmp_path, FcntlLockManager(tmp_path / "locks"))
    first.create(item)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda q: q.claim_next(**CLAIM), (first, second)))
    assert sum(result is not None for result in results) == 1


def test_save_rejects_stale_updated_at(queue):
    current = queue.create(item)
    queue.save(claim_work_item(current, **CLAIM), expected_updated_at=current.updated_at)
    with pytest.raises(RepoForgeError, match="stale"):
        queue.save(current, expected_updated_at=current.updated_at)
```

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_operation_work_queue.py -k 'queue or claim_winner or stale'`

Expected: import or attribute failure for the queue adapter.

- [x] **Step 3: Implement the queue using existing store conventions**

Use one JSON file per operation beneath `<state_root>/operation-work-v1/`, `0600` file permissions, `0700` directory permissions, temp-file fsync plus `os.replace`, directory fsync, and the provided Fcntl lock manager. `claim_next` scans at most 2,000 records, sorts by `(available_at, created_at, operation_id)`, validates every record, then re-reads and claims the winner under its operation lock.

- [x] **Step 4: Run adapter tests and confirm GREEN**

Run: `uv run pytest -q tests/test_operation_work_queue.py`

Expected: pass, including corruption, future-schema, permissions and race cases.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/ports src/repoforge/adapters tests/test_operation_work_queue.py
git commit -m "feat(operations): persist and claim durable work"
```

### Task 3: Recoverable admission and truthful operation phases

**Files:**
- Create: `src/repoforge/application/operations/work_admission.py`
- Modify: `src/repoforge/domain/operation_task.py`
- Modify: `src/repoforge/application/operations/manager.py`
- Modify: `src/repoforge/application/context.py`
- Test: `tests/test_operation_tasks.py`
- Test: `tests/test_durable_verification_dispatch.py`

**Interfaces:**
- Produces: `DurableWorkAdmission.admit(request, *, operation_kind, expires_at=None) -> OperationTask`.
- Produces: `OperationManager.cancel_pending(operation_id, now=None) -> OperationTask` and terminal phase normalization.
- Consumes: `OperationWorkQueue` from Task 2.

- [x] **Step 1: Write failing admission consistency tests**

```python
def test_admission_persists_queued_phase_without_marking_running(env):
    operation = admission.admit(request, operation_kind="workspace_run_profile")
    assert operation.state is OperationState.PENDING
    assert operation.phase == "queued"
    assert queue.read(operation.operation_id) is not None


def test_pending_cancel_terminalizes_and_deletes_work(env):
    operation = admission.admit(request, operation_kind="workspace_run_profile")
    cancelled = admission.cancel(operation.operation_id)
    assert cancelled.state is OperationState.CANCELLED
    assert cancelled.phase == "cancelled"
    assert queue.read(operation.operation_id) is None
```

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_operation_tasks.py tests/test_durable_verification_dispatch.py -k 'admission or pending_cancel or terminal_phase'`

Expected: failures because admission and pending cancellation are absent.

- [x] **Step 3: Implement recoverable two-record admission**

Create the work item first, then create the public operation. If operation creation fails, delete the work item. Startup recovery deletes work without an operation and fails a pending `phase=queued` operation that has no work item with `OPERATION_WORK_MISSING`. This ordering makes every crash state deterministic without requiring a database transaction.

Terminal transitions set phase to the terminal state value unless a more specific terminal phase is supplied. `request_cancel` on pending queued work deletes the work item and transitions directly to cancelled.

- [x] **Step 4: Run and confirm GREEN**

Run: `uv run pytest -q tests/test_operation_tasks.py tests/test_durable_verification_dispatch.py -k 'admission or pending_cancel or terminal_phase'`

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/application/operations/work_admission.py src/repoforge/domain/operation_task.py src/repoforge/application/operations/manager.py src/repoforge/application/context.py tests/test_operation_tasks.py tests/test_durable_verification_dispatch.py
git commit -m "feat(operations): admit queued work truthfully"
```

### Task 4: Typed verification work handlers

**Files:**
- Create: `src/repoforge/application/operations/work_executor.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/application/workspace/run_adhoc.py`
- Test: `tests/test_operation_execution_worker.py`
- Test: `tests/test_background_run_profile.py`

**Interfaces:**
- Produces: `VerificationWorkHandlers.execute(item, *, cancellation_token, progress) -> WorkspaceRunProfileResult | WorkspaceRunAdhocResult`.
- Produces internal runner methods `execute_claimed(command, *, cancellation_token, progress)` that never enqueue recursively.
- Consumes exact serialized request fields from `OperationWorkRequest`.

- [x] **Step 1: Write failing handler tests**

```python
def test_profile_handler_reconstructs_exact_command_without_recursive_enqueue(env):
    result = handlers.execute(profile_item, cancellation_token=token, progress=progress.append)
    assert result.profile == "quick"
    assert queue.list_records(max_records=100).records == (profile_item,)


def test_handler_rejects_fingerprint_drift_before_process_start(env):
    mutate_workspace(env)
    with pytest.raises(RepoForgeError, match="fingerprint"):
        handlers.execute(profile_item, cancellation_token=token, progress=lambda event: None)
    assert env.command_executor.calls == []
```

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_operation_execution_worker.py tests/test_background_run_profile.py -k 'handler or recursive or fingerprint_drift'`

Expected: handler import/attribute failures.

- [x] **Step 3: Extract synchronous execution from scheduling**

Move no command logic into the queue layer. `execute_claimed` retains existing locking, exact-state validation, `ExecutionCoordinator`, result construction and verification receipt behavior. The public runner method delegates either to this internal handler for a claimed item or to durable admission; the worker path is explicitly marked to prevent recursive admission.

- [x] **Step 4: Run and confirm GREEN**

Run: `uv run pytest -q tests/test_operation_execution_worker.py tests/test_background_run_profile.py -k 'handler or recursive or fingerprint_drift'`

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/application/operations/work_executor.py src/repoforge/application/workspace/run_profile.py src/repoforge/application/workspace/run_adhoc.py tests/test_operation_execution_worker.py tests/test_background_run_profile.py
git commit -m "refactor(verify): expose claimed work handlers"
```

### Task 5: Lease-owning worker loop

**Files:**
- Create: `src/repoforge/application/operations/work_loop.py`
- Modify: `src/repoforge/application/operations/work_executor.py`
- Test: `tests/test_operation_execution_worker.py`

**Interfaces:**
- Produces: `OperationWorkLoop.run_once() -> bool`, `run_until_stopped(stop_event) -> None`, and `request_stop() -> None`.
- Uses a unique `owner_id`, 90-second lease, 30-second renew interval, and bounded idle polling.

- [x] **Step 1: Write failing worker lifecycle tests**

```python
def test_worker_claims_once_publishes_progress_and_terminal_result(worker, stores):
    assert worker.run_once() is True
    task = stores.operations.read(OP_ID)
    assert task.state is OperationState.SUCCEEDED
    assert task.phase == "succeeded"
    assert task.result_reference == f"operation-result:{OP_ID}"
    assert stores.queue.read(OP_ID).state is OperationWorkState.COMPLETED


def test_two_workers_execute_one_attempt(stores, counting_handler):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda worker: worker.run_once(), make_two_workers(stores)))
    assert counting_handler.calls == [OP_ID]
```

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_operation_execution_worker.py -k 'worker or two_workers or terminal_result'`

Expected: missing `OperationWorkLoop`.

- [x] **Step 3: Implement claim-execute-heartbeat-terminalize**

The loop claims one item, transitions its operation from pending/queued to running/claimed, starts a lease-renew thread, executes the typed handler, persists the bounded result before success, then completes the work item. Failure persists a sanitized code/message. Stale owners cannot renew progress or terminalize. Cleanup joins the heartbeat thread and removes worker bindings exactly once.

- [x] **Step 4: Run and confirm GREEN**

Run: `uv run pytest -q tests/test_operation_execution_worker.py`

Expected: all worker lifecycle and duplicate-claim tests pass.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/application/operations/work_loop.py src/repoforge/application/operations/work_executor.py tests/test_operation_execution_worker.py
git commit -m "feat(operations): execute claimed work with leases"
```

### Task 6: Public all-durable dispatch and bounded foreground wait

**Files:**
- Modify: `src/repoforge/application/workspace/verify.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/application/workspace/run_adhoc.py`
- Test: `tests/test_durable_verification_dispatch.py`
- Test: `tests/test_background_run_profile.py`

**Interfaces:**
- `WorkspaceVerifier` receives `DurableWorkAdmission` and an operation waiter.
- `background=true` returns immediately after admission.
- `background=false` waits at most 25 seconds and returns the terminal result or a running result with operation ID.

- [x] **Step 1: Write failing public behavior tests**

```python
def test_foreground_verify_only_waits_on_durable_operation(service, blocked_worker):
    started = monotonic()
    result = service.workspace_verify(workspace_id=WS, mode="profile", profile_name="slow")
    assert monotonic() - started < 26
    assert result.outcome == "running"
    assert result.operation["operation_id"]
    assert blocked_worker.command_runs_on_request_thread is False


def test_client_wait_timeout_does_not_cancel_work(service, worker):
    running = service.workspace_verify(workspace_id=WS, mode="profile", profile_name="slow")
    worker.release()
    assert wait_terminal(running.operation["operation_id"]).state is OperationState.SUCCEEDED
```

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_durable_verification_dispatch.py`

Expected: current foreground path executes inline or lacks an operation ID.

- [x] **Step 3: Route every executable verify mode through admission**

Keep `mode=plan` synchronous and read-only. For profile and ad-hoc modes, compile the same reviewed command request, admit it, and long-poll only when `background` is false. Diagnostic execution remains synchronous temporarily only inside a claimed work handler; public diagnostic mode is represented by a serialized diagnostic request before this task is complete.

Return `phase=queued` before claim and preserve the current result shape after terminal result retrieval.

- [x] **Step 4: Run and confirm GREEN**

Run: `uv run pytest -q tests/test_durable_verification_dispatch.py tests/test_background_run_profile.py`

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/application/workspace src/repoforge/application/service.py tests/test_durable_verification_dispatch.py tests/test_background_run_profile.py
git commit -m "feat(verify): route execution through durable admission"
```

### Task 7: Dedicated managed-runtime execution worker

**Files:**
- Create: `src/repoforge/interfaces/runtime/execution_worker.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/adapters/runtime/supervisor.py`
- Modify: `src/repoforge/interfaces/runtime/worker.py`
- Test: `tests/test_control_plane_responsiveness.py`
- Test: `tests/test_runtime_supervisor.py`

**Interfaces:**
- Produces CLI `python -m repoforge.interfaces.runtime.execution_worker --config PATH --generation N`.
- Supervisor owns separate MCP/tunnel and execution-worker process identities and stops both during generation handoff.

- [x] **Step 1: Write failing process-isolation regression**

```python
def test_blocked_verification_does_not_block_control_plane(runtime_harness):
    operation_id = runtime_harness.submit_blocked_profile()
    assert runtime_harness.wait_until_child_started(operation_id)
    started = monotonic()
    assert runtime_harness.workspace_status()["status"] == "ok"
    assert runtime_harness.operation_get(operation_id)["state"] == "running"
    assert monotonic() - started < 2.0
```

Also assert the MCP server PID differs from the execution-worker PID and both are terminated during handoff.

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_control_plane_responsiveness.py tests/test_runtime_supervisor.py -k 'execution_worker or blocked_verification'`

Expected: no execution worker process exists and the blocked request owns MCP execution.

- [x] **Step 3: Add and supervise the worker process**

The execution-worker CLI builds the application for the accepted generation, creates handlers and runs `OperationWorkLoop`. The supervisor starts it before opening the MCP gate, records its PID/start token, and drains it by stopping new claims then waiting for the current bounded handoff policy. The new generation may claim only work whose config generation matches; old work is reconciled before reopening.

- [x] **Step 4: Run and confirm GREEN**

Run: `uv run pytest -q tests/test_control_plane_responsiveness.py tests/test_runtime_supervisor.py -k 'execution_worker or blocked_verification'`

Expected: pass within bounded test time.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/interfaces/runtime src/repoforge/adapters/runtime/supervisor.py src/repoforge/bootstrap.py tests/test_control_plane_responsiveness.py tests/test_runtime_supervisor.py
git commit -m "feat(runtime): isolate durable execution worker"
```

### Task 8: Recovery and cancellation semantics

**Files:**
- Modify: `src/repoforge/application/operations/recovery.py`
- Modify: `src/repoforge/application/operations/work_admission.py`
- Modify: `src/repoforge/application/operations/work_loop.py`
- Modify: `src/repoforge/application/service.py`
- Test: `tests/test_operation_execution_worker.py`
- Test: `tests/test_phase6_operational_hardening.py`

**Interfaces:**
- Produces `recover_operation_work(...) -> OperationWorkRecoveryReport` with requeued, orphaned, cancelled, missing-work and orphan-work counts.

- [x] **Step 1: Write failing restart/cancel tests**

```python
def test_expired_claim_without_child_is_requeued(recovery_fixture):
    report = recover_operation_work(now=LATER, **recovery_fixture.args)
    assert report.requeued == 1
    assert queue.read(OP_ID).state is OperationWorkState.QUEUED
    assert operations.read(OP_ID).phase == "queued"


def test_expired_running_mutation_with_child_is_orphaned(recovery_fixture):
    mark_child_started(mutating_item)
    report = recover_operation_work(now=LATER, **recovery_fixture.args)
    assert report.orphaned == 1
    assert operations.read(OP_ID).state is OperationState.ORPHANED


def test_queued_cancel_is_terminal_and_idempotent(service):
    first = service.operation_cancel(OP_ID)
    second = service.operation_cancel(OP_ID)
    assert first.operation.state is OperationState.CANCELLED
    assert second.already_terminal is True
```

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_operation_execution_worker.py tests/test_phase6_operational_hardening.py -k 'requeue or queued_cancel or orphaned'`

Expected: current recovery orphans all expired running ownership and cannot reconcile queued work.

- [x] **Step 3: Implement safe recovery matrix**

Requeue only when `child_started` is false. A read-only item with a known-dead child may retry once when fingerprint and config still match. Mutating or ambiguous work becomes orphaned. Cancellation removes queued work immediately; running cancellation signals/reaps the process group and terminalizes only after the child outcome is known.

- [x] **Step 4: Run and confirm GREEN**

Run: `uv run pytest -q tests/test_operation_execution_worker.py tests/test_phase6_operational_hardening.py`

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/application/operations src/repoforge/application/service.py tests/test_operation_execution_worker.py tests/test_phase6_operational_hardening.py
git commit -m "fix(operations): recover and cancel durable work safely"
```

### Task 9: Progress, contracts and generated schemas

**Files:**
- Modify: `src/repoforge/contracts/common.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `tests/test_operation_progress_wait.py`
- Modify: `tests/test_background_run_profile.py`
- Modify generated contract artifacts via `make schemas`

**Interfaces:**
- Public operation output adds `attempt`, `heartbeat_at`, `heartbeat_age_seconds`, and `evidence_complete`.
- Existing fields remain unchanged.

- [x] **Step 1: Write failing progress truth tests**

```python
def test_running_profile_names_current_step_and_heartbeat(service, release):
    operation = start_progress_profile(service, release)
    observed = wait_for_progress(operation.operation_id, current=1)
    assert observed.progress_message.startswith("running tests")
    assert observed.heartbeat_at is not None
    assert observed.heartbeat_age_seconds >= 0
    assert observed.attempt == 1


def test_terminal_operation_does_not_keep_running_phase(service):
    terminal = wait_terminal(start_quick_profile(service).operation_id)
    assert terminal.phase in {"succeeded", "failed", "cancelled", "orphaned"}
```

- [x] **Step 2: Run and confirm RED**

Run: `uv run pytest -q tests/test_operation_progress_wait.py tests/test_background_run_profile.py -k 'heartbeat_age or current_step or running_phase'`

Expected: new fields/phase assertions fail.

- [x] **Step 3: Publish additive evidence and regenerate schemas**

Compute heartbeat age at read time from persisted `updated_at`/heartbeat timestamp; never persist a drifting age. Derive current step name from the selected `VerificationStep` before command start. Run `make schemas` and verify the tool count remains 28.

- [x] **Step 4: Run and confirm GREEN**

Run: `uv run pytest -q tests/test_operation_progress_wait.py tests/test_background_run_profile.py`

Run: `make schemas`

Run: `make check-generated`

Expected: all pass and no unexpected tool-surface change.

- [x] **Step 5: Commit**

```bash
git add src/repoforge/contracts src/repoforge/application/workspace/run_profile.py docs/contracts tests/test_operation_progress_wait.py tests/test_background_run_profile.py
git commit -m "feat(operations): expose truthful worker progress"
```

### Task 10: Slice-A verification and runtime proof

**Files:**
- Modify: `docs/superpowers/plans/2026-07-27-durable-execution-plane.md` only to check completed boxes.

**Interfaces:**
- Consumes the exact final fingerprint from RepoForge status.
- Produces focused and full durable operation receipts.

- [x] **Step 1: Run focused tests through RepoForge**

Run these exact selectors with `workspace_verify` and the current fingerprint:

```text
tests/test_operation_work_queue.py
tests/test_operation_execution_worker.py
tests/test_durable_verification_dispatch.py
tests/test_control_plane_responsiveness.py
tests/test_background_run_profile.py
tests/test_operation_progress_wait.py
tests/test_operation_tasks.py
tests/test_operation_observability.py
tests/test_phase6_operational_hardening.py
```

Expected: all pass; while the blocked-command test runs, `operation get` and `workspace_status` respond within two seconds.

Result: 127 passed. `operation` get/list and cancellation answer in milliseconds while a
child is genuinely blocked. A status read of the *executing* workspace serialized behind
that workspace's lock at the time this ran; it was fixed in a follow-up commit and both
the idle-bystander and busy-workspace reads are now covered -- see Deviations above.

- [x] **Step 2: Run format, lint, typing and generated checks**

Run: `make lint`

Run: `make typecheck`

Run: `make schemas`

Run: `make test-groups-check`

Expected: all pass without changing files after verification begins.

Result: Ruff clean; Mypy clean across 434 source files; `make schemas` reports
`changed_paths: []` with `tool_count: 28`; the test-group manifest is complete after
mapping the new durable test files (it was failing on 11 unmapped files, including one
from earlier committed work). `make format` and `make check-generated` do not exist in
this repository -- see Deviations.

- [x] **Step 3: Run the repository full suite and release gates**

Run: `make test-fast` (serial lane, then the parallel lane)

Run: `make v2-gates`

Expected: immediate queued/running operation response; step name and heartbeat are observable; control-plane calls stay responsive; terminal result has an immutable result reference and satisfies the commit gate.

Result: full suite green across both lanes (168 test files). Control-plane fault gates
pass with `unknown_effect_outcomes=0`, `hidden_retries=0`, `duplicate_read_rate=0.000%`.
Verification ran through the repository's own Make targets rather than a self-hosted
`workspace_verify` call, because the installed runtime is a different release than this
worktree; the durable path itself is proven by `tests/test_control_plane_responsiveness.py`,
which admits real work, executes it in a worker, and asserts the terminal result reference
and `evidence_complete`.

- [x] **Step 4: Commit the exact verified tree**

```bash
git add docs/superpowers/plans/2026-07-27-durable-execution-plane.md
git commit -m "docs(operations): record durable execution verification"
```

Result: committed as `feat(operations): make verification durable, restart-safe work (#232)`
on `ai/epic-232-control-plane-truth-hardening-7d3cf72699`, then merged with the remote
branch and pushed to PR #250. The responsive-status follow-up landed on top.
