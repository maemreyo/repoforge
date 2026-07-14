# Durable PR Check Watch Design

## Context

Issue #10 requires a public operation that watches one workspace pull request until either every selected check completes or the first failure appears. The watch must be durable, cancellable, resumable after process restart, exact-SHA-bound, bounded, and safe under GitHub outages.

RepoForge already has the required foundations:

- `OperationTask` and `OperationManager` provide durable lifecycle, progress, cancellation requests, retention, and optimistic compare-and-swap.
- `workspace_pr_checks` returns compact check buckets and exact Check Run selectors.
- `workspace_pr_failure_evidence` returns bounded redacted failure evidence for one exact pushed SHA.
- workspace metadata records `last_pushed_sha`.

The missing piece is a typed durable watch definition plus a coordinator that polls GitHub in the background and drives an `OperationTask` to a terminal state.

## Decision

Add one new MCP tool:

```text
workspace_pr_watch(
  workspace_id,
  until = "all_completed" | "first_failure",
  timeout_seconds = 900,
  include_failure_evidence = true,
) -> operation reference
```

The call validates and captures the exact pushed workspace identity, creates an `OperationTask`, persists a `PrCheckWatch` record, schedules a bounded worker, and returns immediately. Callers use existing `operation_status`, `operation_list`, and `operation_cancel` tools.

## Domain model

`PrCheckWatch` is a separate schema-versioned record keyed by `operation_id`. It stores only safe bounded state:

- operation/workspace IDs;
- branch and PR number;
- exact pushed SHA and workspace fingerprint;
- completion mode;
- deadline and retry/backoff state;
- compact check counters and opaque Check Run selectors;
- failure-evidence selectors/references, never raw logs;
- result status and timestamps.

It does not duplicate `OperationTask` lifecycle fields or persist GitHub payloads, patches, source, logs, credentials, or arbitrary text.

## Execution model

`PrCheckWatchCoordinator` owns watch creation, one polling iteration, background scheduling, and restart recovery.

1. Under the workspace lock, validate a pushed commit exists and current HEAD equals `last_pushed_sha`.
2. Read PR status and capture exact PR number/head SHA.
3. Create the operation and watch record atomically in a deterministic order. If watch persistence fails, transition the operation to failed rather than leaving an unexplained active task.
4. Schedule a daemon worker through a narrow `BackgroundTaskRunner` port.
5. Each iteration:
   - reload operation/watch state;
   - honor a cancellation request before external reads;
   - fail on deadline;
   - revalidate workspace HEAD, pushed SHA, PR number, and PR head SHA;
   - read checks and calculate deterministic buckets/selectors;
   - persist watch progress and update `OperationTask` progress;
   - terminate on the selected completion condition;
   - optionally collect bounded failure evidence through the existing reader;
   - otherwise sleep using bounded exponential backoff with deterministic jitter.
6. Transient GitHub failures leave the operation running in a retrying phase until the deadline. Stable error codes and sanitized progress are persisted.

## Restart semantics

Startup recovery must not orphan resumable `pr_check_watch` operations. `recover_operations` receives a set of resumable kinds and skips orphaning those records. After construction, the coordinator scans persisted watch records and schedules each nonterminal operation exactly once. The production thread runner deduplicates by operation ID.

If the watch record is missing or corrupt while its operation remains active, recovery fails that operation with a stable watch-state error. Terminal operations are never restarted.

## Concurrency and cancellation

- Operation and watch writes use existing per-record CAS/locks.
- The background runner deduplicates active keys.
- Cancellation remains a request: `operation_cancel` sets `cancellation_requested_at`; the worker observes it and transitions to terminal `cancelled`.
- A worker checks cancellation before each poll and after each sleep.
- Concurrent terminal transitions use operation CAS and cannot overwrite a newer terminal state.

## Exact identity and staleness

The watch is bound to:

- workspace ID and branch;
- PR number;
- exact pushed commit SHA;
- workspace fingerprint at creation.

Any change to current workspace HEAD, `last_pushed_sha`, PR number, or PR head SHA fails closed with `PR_CHECK_WATCH_STALE`. A changed working-tree fingerprint is reported as stale because the original watch no longer describes the reviewed workspace snapshot.

## Bounded behavior

- Timeout: 5–7,200 seconds.
- Poll interval: starts at 1 second, doubles to at most 30 seconds.
- Deterministic jitter: derived from operation ID, never random global state.
- Check selectors: at most 200, sorted and deduplicated.
- Failure evidence: at most 20 failed selectors; only opaque references are persisted.
- Progress messages are sanitized by `OperationTask` validation.
- Audit contains IDs, counts, phases, states, and error codes only.

## Error taxonomy

Add stable codes:

- `PR_CHECK_WATCH_INVALID`
- `PR_CHECK_WATCH_STALE`
- `PR_CHECK_WATCH_TIMEOUT`
- `PR_CHECK_WATCH_STATE_CORRUPT`
- `PR_CHECK_WATCH_UNAVAILABLE`

GitHub outages are retryable and remain nonterminal until timeout. Identity mismatch and corrupt durable state fail immediately.

## Public contract

The MCP handler is thin and marked as a local non-destructive create operation. It returns the `OperationSummary` plus the watch mode/deadline. No new cancel/status tools are added.

## Testing strategy

Tests cover:

- valid/invalid start requests and exact identity capture;
- pending-to-success for both completion modes;
- first failure and optional failure-evidence references;
- cancellation before and during polling;
- timeout and transient outage/backoff;
- stale workspace, pushed SHA, PR number, and PR head;
- restart recovery and runner deduplication;
- corrupt/missing watch state;
- deterministic private persistence, schema rejection, permissions, bounds, and CAS;
- audit safety;
- actual MCP annotations and invocation;
- full production and exact-tree verification.

## Non-goals

No merge, rerun, retry, check mutation, workflow dispatch, arbitrary polling expression, raw log persistence, generic scheduler, distributed worker queue, or GitHub webhook support is introduced.