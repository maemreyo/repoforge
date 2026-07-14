# Durable OperationTask Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a protocol-independent durable `OperationTask` state machine, crash-safe optimistic JSON store, internal coordinator, and bounded status/list/cancel interfaces.

**Architecture:** Put all transition and validation policy in a typed domain module. Persist one schema-versioned operation per private JSON file through a typed port and cross-process CAS adapter. Build internal creation/progress/recovery APIs separately from three public read/cancel use cases, then expose thin CLI and MCP adapters.

**Tech Stack:** Python 3.10+, frozen dataclasses, enums, standard-library JSON/filesystem primitives, existing lock/audit/clock/ID ports, FastMCP, argparse, pytest.

## Global Constraints

- No arbitrary command execution, worker queue, remote scheduler, or source-changing retry.
- No raw logs, source, patches, secrets, environment bodies, or unbounded messages in operation state.
- Public creation and progress-update operations are forbidden in this ticket.
- Terminal states never return to running.
- Every write uses compare-and-swap and private atomic persistence.
- MCP Tasks may adapt later but does not define this domain contract.
- Existing repository/workspace behavior remains compatible until a consumer adopts the abstraction.

---

### Task 1: Specify domain transitions with failing tests

**Files:**
- Create: `tests/test_operation_tasks.py`
- Create: `src/repoforge/domain/operation_task.py`
- Modify: `src/repoforge/domain/errors.py`

**Interfaces:**
- Produces `OperationState`, `OperationRetryability`, `OperationSnapshotBinding`, `OperationTask`, `new_operation_task`, `transition_operation`, `update_operation_progress`, `request_operation_cancellation`, `next_operation_timestamp`, and stable operation error codes.

- [ ] Write failing tests that construct a bounded pending operation and assert all required fields and schema version.
- [ ] Write table-driven failing tests for every permitted transition: pending to running/failed/cancelled/expired and running to succeeded/failed/cancelled/expired/orphaned.
- [ ] Write failing tests proving terminal immutability, same-state idempotency, monotonic same-phase progress, allowed phase reset, total bounds, cancellation request idempotency, unsupported cancellation, and strictly increasing timestamps with a fixed clock value.
- [ ] Run `uv run pytest tests/test_operation_tasks.py -q`; expect import/undefined failures.
- [ ] Implement frozen enums/dataclasses, bounded validators, transition table, progress/cancellation helpers, and redacted bounded error text.
- [ ] Add stable errors `OPERATION_INVALID`, `OPERATION_NOT_FOUND`, `OPERATION_STALE`, `OPERATION_CORRUPT`, `OPERATION_SCHEMA_UNSUPPORTED`, and `OPERATION_TRANSITION_INVALID` with safe explanations.
- [ ] Re-run the focused domain tests until green.

### Task 2: Add optimistic private JSON persistence

**Files:**
- Create: `src/repoforge/ports/operation_store.py`
- Modify: `src/repoforge/ports/__init__.py`
- Create: `src/repoforge/adapters/persistence/json_operation_store.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Modify: `src/repoforge/testing/fakes.py`
- Test: `tests/test_operation_tasks.py`

**Interfaces:**
- Produces `OperationStore.create`, `read`, `save(expected_updated_at)`, `list_records(max_records)`, and `delete`.
- Produces `OperationRecordPage(records, scan_truncated)` and `InMemoryOperationStore`.

- [ ] Add failing tests for create/read, duplicate create, stale CAS, successful CAS, missing record, deterministic bounded list, delete, identity mismatch, corrupt JSON, future schema, forbidden persisted keys, `0700` directory, `0600` file, and no temporary-file leak.
- [ ] Implement strict encode/decode functions that serialize enums and snapshot bindings without generic model-facing dictionaries.
- [ ] Reject unsupported schema versions, filename/record identity mismatches, forbidden payload keys, and invalid operation IDs.
- [ ] Implement per-operation locks, atomic temporary write, file/directory fsync, chmod, and compare-and-swap under one lock.
- [ ] Implement bounded sorted scanning and explicit `scan_truncated` metadata.
- [ ] Add the deterministic in-memory adapter with the same CAS behavior for application tests.
- [ ] Run `uv run pytest tests/test_operation_tasks.py -q` until persistence tests pass.

### Task 3: Build the internal coordinator and startup maintenance

**Files:**
- Create: `src/repoforge/application/operations/__init__.py`
- Create: `src/repoforge/application/operations/manager.py`
- Create: `src/repoforge/application/operations/recovery.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_bootstrap_factories.py`
- Test: `tests/test_operation_tasks.py`

**Interfaces:**
- Produces `OperationManager.create`, `start`, `progress`, `succeed`, `fail`, `cancelled`, `expire`, and `orphan`.
- Produces `recover_operations(manager, now, retention_seconds=604800)`.
- Adds `Application.operations` and `ApplicationContext.operation_store`.

- [ ] Write failing manager tests for creation, each mutation method, stale CAS propagation, safe error redaction, and audit metadata that excludes progress/error bodies.
- [ ] Write failing startup tests: running becomes orphaned with `OPERATION_WORKER_LOST`; expired pending/running becomes expired; terminal records older than seven days are deleted; concurrent CAS winner is preserved; corrupt records fail closed rather than being deleted.
- [ ] Implement manager methods using only domain helpers and the typed store.
- [ ] Add a compact private audit helper that records operation ID, kind, old/new state, phase, timestamps, and safe error code only.
- [ ] Wire `JsonOperationStore` and overrides in the composition root, construct `OperationManager`, run startup recovery once, and expose it on `Application`.
- [ ] Update bootstrap factory tests and run `uv run pytest tests/test_operation_tasks.py tests/test_bootstrap_factories.py -q`.

### Task 4: Add bounded public application use cases

**Files:**
- Create: `src/repoforge/application/operations/status.py`
- Create: `src/repoforge/application/operations/list.py`
- Create: `src/repoforge/application/operations/cancel.py`
- Modify: `src/repoforge/application/service.py`
- Test: `tests/test_operation_tasks.py`
- Modify: `tests/test_service_tools.py`

**Interfaces:**
- Produces `operation_status(operation_id)`.
- Produces `operation_list(scope=None, state=None, limit=50, cursor=None)`.
- Produces `operation_cancel(operation_id, expected_updated_at=None)`.

- [ ] Write failing tests for compact deterministic status serialization and missing/invalid IDs.
- [ ] Write failing list tests for state filters, `task:<id>` and `workspace:<id>` scopes, `1..100` limits, descending ordering, next cursor, cursor resume, invalid/stale cursor, and scan truncation.
- [ ] Write failing cancel tests for a fresh request, repeated request, unsupported cancellation, already-terminal state, caller-provided stale `expected_updated_at`, and concurrent store CAS failure.
- [ ] Implement focused command/result dataclasses and readers/writer that delegate to `OperationManager` and never expose internal creation/progress methods.
- [ ] Add `CodingService` methods and an internal `operations` attribute for future approved consumers.
- [ ] Run focused operation and service tests until green.

### Task 5: Wire CLI, MCP, golden contracts, and docs

**Files:**
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Modify: `src/repoforge/interfaces/cli/contract.py`
- Modify: `tests/test_mcp_contract.py`
- Modify: `tests/test_phase5_mcp_contract.py`
- Modify: `tests/test_cli_surface_coverage.py`
- Modify: `docs/contracts/release-contract-v1.json`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/testing/TESTING.md`
- Modify: `docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md`

**Interfaces:**
- Adds MCP tools `operation_status`, `operation_list`, and `operation_cancel`.
- Adds CLI commands `rf operation status ID`, `rf operation list`, and `rf operation cancel ID`.

- [ ] Extend MCP inventory and actual-client invocation tests; require read-only annotations for status/list and local non-destructive mutation for cancel.
- [ ] Extend CLI parser/dispatch tests for status/list/cancel, state choices, scope, cursor, limit, and expected-updated-at.
- [ ] Add three thin MCP handlers and one thin CLI operation command dispatcher.
- [ ] Update the MCP count from 33 to 36 and intentionally regenerate/review the release contract.
- [ ] Extend the CLI release contract with the `operation` command family.
- [ ] Document state semantics, cancellation request versus terminal cancellation, startup orphaning, retention, pagination, safe fields, and non-goals.
- [ ] Mark issue #9 foundation complete in the roadmap without claiming PR-watch or execution-plan adoption.
- [ ] Run operation, CLI, MCP, contract, and documentation-focused tests.

### Task 6: Review, exact-tree verification, and publication

**Files:**
- Review every changed file; no unrelated refactor.

**Interfaces:**
- Produces one verified commit and one draft PR closing #9.

- [ ] Review `workspace_diff` for generic dictionaries, raw persisted content, unsafe audit fields, unbounded scans, missing CAS checks, unrelated cleanup, or public creation/progress tools.
- [ ] Run the RepoForge `full` profile and require release contract validation, Ruff formatting/lint, strict mypy, all tests, coverage, source/wheel builds, and installed-wheel smoke.
- [ ] Confirm the final verification fingerprint matches the exact uncommitted tree.
- [ ] Commit with `feat(operations): add durable task foundation`.
- [ ] Push without force.
- [ ] Create a draft PR describing scope, state/CAS invariants, persistence/recovery, compatibility, verification evidence, deferred consumers, and `Closes #9`.
- [ ] Read PR status and report queued, pending, or failed CI without merging or marking ready.
