# State Concurrency Hardening Design

## Goal

Remove the confirmed persistence races and crash-safety defects from the investigation while preserving the intentional saga and configuration-authority designs that are not bugs.

## Scope

The implementation covers eight concrete hardening slices:

1. unify state migration, cleanup, recovery, and ordinary record mutation under one collection lock hierarchy;
2. add revision-aware atomic updates to the workspace registry and migrate unsafe metadata RMW call sites;
3. make runtime reads pure and move durable repair behind a locked compare-before-mutate reconcile operation;
4. serialize audit sequence allocation, rotation, append, and prune across processes, with atomic prune replacement;
5. make admission-epoch mutations atomic inside the store while retaining the existing outer admission transaction lock;
6. replace quarantine copy/unlink and local runtime ad-hoc writers with crash-safe same-filesystem rename and the shared atomic writer;
7. encapsulate idempotency RMW and make accepted-versus-active configuration reads explicit;
8. update architecture documentation and add targeted invariant/audit tests for the findings that are not confirmed bugs.

H4 remains a write-ahead durability invariant, not a production defect. H8 remains an expected after-effect saga outcome; implementation changes are limited to proving or improving an existing reconcile path. H5 and H6 receive lock-order and API-contract hardening only where call-site evidence supports it.

## Architecture

### Collection maintenance coordination

`LockManager` gains a shared-lock operation. Ordinary `JsonStateRepository` mutations acquire the shared collection maintenance lock and then the exclusive record lock. Migration, cleanup, and recovery acquire the exclusive collection maintenance lock. This preserves parallel writes to different records while making lifecycle operations mutually exclusive with every ordinary writer. The canonical namespace is `state-lifecycle-{collection}`.

Lock order is fixed as collection maintenance before record. No lifecycle operation acquires record locks, so the hierarchy cannot invert.

### Workspace registry atomic updates

Workspace files gain a private monotonic `revision` envelope field with backward-compatible decoding for legacy records. `WorkspaceStore` exposes an atomic `update(workspace_id, callback)` operation implemented under a per-workspace interprocess lock. The callback receives the latest record and its result is saved with the next revision in the same critical section. Existing create/replace paths may continue to use `save`; confirmed unsafe metadata RMW paths use `update`.

### Runtime observation and reconciliation

Runtime parsing and identity observation become side-effect free. `read()` may project a stale supervisor as absent and a stale child as degraded, but it never unlinks or rewrites state. `reconcile()` acquires a sibling OS lock, rereads the durable record, verifies that the observed identity/digest still matches, then applies the clear or degrade mutation. A replacement written after observation is preserved.

### Audit integrity

All audit writers and prune operations use one sibling `fcntl` lock. While holding it, a writer rereads the durable tail, allocates the next global sequence, rotates if needed, appends, fsyncs, and updates its local cache. Prune rereads under the same lock and replaces the file through the shared secure atomic writer. Process-local compaction remains protected by the existing thread lock.

### Atomic file mechanics

Quarantine uses `os.replace(source, target)` under the record lock after collision validation, then fsyncs both directories. Local runtime writers use `atomic_write_text`. Admission epoch JSON also uses the shared atomic writer and a dedicated store lock; the existing outer `WORKER_ADMISSION_LOCK` still protects the larger read/claim/create-intent transaction.

### Authority and abstraction contracts

Idempotency raw persistence is accessed through a keyed transaction boundary for compound state transitions. Configuration generation reads require an explicit generation or explicit accepted/active selector. Fingerprint persistence remains caller-locked; targeted tests enforce every current `persist=True` call is inside a workspace lock, avoiding an unsupported broad redesign.

## Error handling

Lock acquisition remains bounded and fail-closed with `LOCK_TIMEOUT`. CAS conflicts surface as retryable stale-state errors. Reconcile operations re-read under lock and no-op when the durable identity moved. Atomic replacement failures preserve the previous durable file. Quarantine target collisions fail without deleting either copy.

## Verification

Each slice starts with a deterministic failing regression test. Concurrency tests use barriers and independent store instances. Crash-safety tests inject failure before replacement and assert the original durable state survives. After targeted tests pass, run affected-test selection, strict lint/type checks, and the reviewed verification profile before commit.
