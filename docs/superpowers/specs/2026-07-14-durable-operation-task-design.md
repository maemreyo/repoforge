# Durable OperationTask Foundation Design

## Context

Issue #9 requires a protocol-independent durable operation model that later consumers such as PR-check watching, verification, indexing, and plan execution can create, update, resume, cancel, expire, and inspect. The foundation must not execute work itself and must not let MCP Tasks define core semantics.

## Considered approaches

### 1. Typed domain model with per-record atomic JSON persistence — recommended

Store one schema-versioned JSON document per operation under RepoForge's private state root. Domain functions validate transitions and progress. The adapter owns atomic replace, `0600` files, `0700` directories, fsync, identity validation, compare-and-swap, corruption handling, and bounded listing.

This matches existing onboarding and idempotency persistence, keeps dependencies unchanged, and makes records inspectable and recoverable without introducing a database service.

### 2. Append-only event log

An event log would preserve transition history naturally, but it adds compaction, replay, partial-write recovery, and migration complexity before any durable consumer exists. Audit already records safe transition metadata, so a second event stream is unnecessary for this ticket.

### 3. SQLite operation database

SQLite would make pagination and concurrent updates straightforward, but adds a new persistence technology and migration surface to a personal local tool. Per-record JSON plus existing cross-process locks is sufficient for the bounded foundation.

## Domain model

`OperationTask` is an immutable dataclass with schema version `1`:

```text
operation_id
kind
state: pending | running | succeeded | failed | cancelled | expired | orphaned
phase
progress_current
progress_total?
progress_unit?
progress_message?
task_id?
workspace_id?
snapshot_binding?
result_reference?
error_code?
error_message?
retryability: none | manual | automatic
cancel_supported
cancellation_requested_at?
created_at
updated_at
expires_at?
schema_version
```

`OperationSnapshotBinding` contains only bounded identity fields: `head_sha`, `workspace_fingerprint`, `config_generation`, and `evidence_snapshot_id`. It never stores source, patches, logs, or environment data.

All IDs, kind, phase, progress text, result references, and error fields are length- and character-bounded. Stored error text is redacted before persistence.

## Transition rules

- `pending -> running | failed | cancelled | expired`.
- `running -> succeeded | failed | cancelled | expired | orphaned`.
- Terminal states are `succeeded`, `failed`, `cancelled`, `expired`, and `orphaned`.
- Repeating the exact current state is idempotent; a terminal state cannot transition to a different state.
- Progress cannot decrease within the same phase.
- A phase change may reset progress.
- `progress_current` is non-negative and cannot exceed `progress_total` when total is present.
- Cancellation request is distinct from terminal cancellation.
- Repeated cancellation requests return the unchanged task.
- Unsupported cancellation and already-terminal cancellation are explicit non-mutating results.
- Every mutation produces a strictly increasing ISO `updated_at`, even when a deterministic clock returns the same timestamp.

## Persistence contract

`OperationStore` exposes:

```text
create(task)
read(operation_id)
save(task, expected_updated_at)
list_records(max_records)
delete(operation_id)
```

`save` is compare-and-swap. The JSON adapter takes a per-operation cross-process lock, re-reads the current record under the lock, compares `updated_at`, and atomically replaces the file. It rejects unsupported future schemas and corrupt records with stable errors. The root directory is `state_root/operations`, mode `0700`; records are mode `0600`.

The adapter scans at most 2,000 records for one public list call and reports whether the scan was truncated. Application output is limited to 100 records per page.

## Internal application coordinator

`OperationManager` is the internal service for approved consumers. It provides typed methods to:

- create pending operations;
- mark running;
- update phase/progress;
- succeed with a bounded result reference;
- fail with a safe code/message and retryability;
- mark terminal cancellation;
- expire;
- orphan unrecoverable running operations.

It is constructed by the composition root and exposed on the internal `Application` container. Public MCP/CLI callers cannot invoke creation or progress methods.

## Startup recovery, expiry, and retention

On application construction:

- every persisted `running` operation is atomically changed to `orphaned` with error code `OPERATION_WORKER_LOST`, because this ticket has no recoverable worker registry;
- every non-terminal operation whose `expires_at` is at or before the current time becomes `expired`;
- terminal records whose `updated_at` is older than seven days are deleted;
- malformed records are not silently deleted and do not cause a false success transition.

Recovery uses CAS and tolerates a concurrent winner by re-reading rather than overwriting it.

## Public operations

```text
operation_status(operation_id)
operation_cancel(operation_id, expected_updated_at?)
operation_list(scope?, state?, limit?, cursor?)
```

`scope` accepts only `task:<id>` or `workspace:<id>`. `state` is one of the seven domain states. `limit` is clamped to `1..100`. Results sort by `updated_at` descending then `operation_id` descending. `next_cursor` is the final returned operation ID; a stale or unknown cursor fails explicitly.

`operation_cancel` returns the current compact task plus:

- `cancellation_requested`;
- `already_requested`;
- `already_terminal`;
- `cancel_supported`.

It never marks terminal cancellation itself; the owned worker remains responsible for acknowledging cancellation.

## Interfaces

MCP adds three closed-world tools:

- `operation_status` — read-only;
- `operation_list` — read-only;
- `operation_cancel` — local mutation, non-destructive and idempotent.

CLI adds `rf operation status ID`, `rf operation list`, and `rf operation cancel ID`. CLI and MCP delegate to the same `CodingService` methods and do not own transition policy.

## Errors and audit safety

Stable errors cover invalid identity/scope/state/cursor, missing operation, stale CAS, corrupt/future schema, invalid transition/progress, and persistence failure.

Audit records contain only operation ID, kind, previous/new state, phase, timestamps, safe error code, and count/filter metadata. They never contain progress messages, stored error messages, snapshot bodies, result bodies, logs, source, patches, secrets, or environment values.

## Testing

Tests use deterministic clocks, in-memory stores, temporary state roots, and in-memory MCP sessions. Coverage includes:

- every valid and invalid state transition;
- same-state idempotency and terminal immutability;
- monotonic progress and phase resets;
- cancellation unsupported/already requested/already terminal;
- CAS races and stale expected timestamps;
- atomic persistence, permissions, corruption, future schema, and identity mismatch;
- restart orphaning, expiry, retention, and concurrent recovery;
- deterministic filtering, pagination, cursor errors, and bounds;
- service, CLI, MCP metadata/invocation, release contracts, and audit redaction.

Final verification is `scripts/verify-production.sh --allow-dirty` through RepoForge `full` exact-tree verification before commit.
