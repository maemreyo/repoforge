# Phase 6 Operational Hardening

Phase 6 implements the structured UX, observability, bounded logging, diagnostics, capability
discovery, idempotency, and retry requirements from
`docs/plans/repoforge-production-architecture-tunnel-plan.md`. It is based on upstream
`dev@9c98ceb350b7d8dc6cad033d7d0bf9d9059be4a1`.

## Dependency boundaries

The implementation preserves the existing dependency direction:

- `domain/operations.py` owns pure idempotency, retry, and unchanged-state policy.
- `ports/idempotency.py` and `ports/metrics.py` define persistence and observability boundaries.
- `application/idempotency.py` coordinates a keyed operation without importing concrete adapters.
- `application/diagnostics/` builds metadata-only bundles.
- `adapters/persistence/json_idempotency_store.py` and
  `adapters/observability/json_metrics.py` provide local crash-safe implementations.
- `interfaces/cli/` and `interfaces/mcp/` render stable user-facing envelopes.
- `bootstrap.py` remains the only production composition root.

## Stable failure envelope

CLI and MCP failures expose:

- `status` and stable `error_code`;
- redacted `what_happened` and categorized `why`;
- `correlation_id`;
- `unchanged_state`;
- `safe_next_action`;
- `retryable`;
- `automatic_retry_allowed`.

`automatic_retry_allowed` is true only when all conditions hold:

1. the workflow is one of workspace create, push, create draft PR, or update draft PR;
2. the caller supplied an idempotency key;
3. the failure category is transient and reviewed, including timeout, lock contention, runtime
   reload/unavailability, or durable operational-state persistence failure.

Security, approval, input, stale semantic state, and arbitrary write operations are never retried
automatically.

## Cross-process idempotency

Idempotency keys are accepted by the four reconciliable write workflows. RepoForge:

1. validates the key and stores only its SHA-256 hash;
2. hashes canonical JSON input, preventing one key from authorizing different input;
3. acquires a cross-process lock scoped to action and key hash;
4. writes a private `in_progress` receipt with correlation ID;
5. executes the operation and its workflow-specific reconciliation logic;
6. writes a private, fsynced `completed` receipt;
7. returns the same sanitized result on the first call and every replay.

Receipts never persist raw keys, PR bodies, patches, file content, environment data, stdout, stderr,
or credential-shaped fields. High-risk fields are replaced by omission markers and hashes. A corrupt
receipt fails with `STATE_PERSISTENCE_FAILED`; it is never silently ignored.

Workspace creation derives deterministic workspace and branch suffixes from the key hash. Push
reconciles the upstream SHA. Draft PR creation discovers an already-created PR. Draft PR update is
safe to replay with identical validated title/body input.

## Audit and metrics

Every application operation is assigned a correlation ID and duration. The private JSONL audit log:

- recursively redacts secrets before serialization;
- uses `0700` directories and `0600` files;
- fsyncs durable appends and directory changes;
- rotates to a configured bounded number of backups;
- replaces oversized events with a SHA-256 summary rather than retaining a content preview;
- preserves the primary operation error when a failure audit append also fails.

Aggregate operation metrics record count, success/failure count, total/max duration, and stable
failure categories. Metrics are written atomically under the existing cross-process lock manager.
Metrics failures are best-effort and never replace the primary operation result.

## Live tunnel logs

The tunnel child no longer writes stdout directly to a file. A supervisor-owned pump reads bounded
chunks, redacts credential-shaped text and the exact control-plane key before persistence, and rotates
logs while the process is running. The current log and each retained backup remain private and within
`runtime_log_max_bytes`. A pathological no-newline stream is bounded so it cannot grow memory or
bypass the retention policy.

## Runtime truth and bounded recovery

The managed runtime no longer equates process liveness with service health. The domain record retains
both its persisted phase and timestamped component observations. The supervisor composes tunnel-child
identity, loopback admin readiness, MCP generation, repository self-check, and control-plane response
signals behind the existing `TunnelClient` and runtime-control ports. Startup and steady-state monitoring
use the same observation path.

One transient failure records evidence only. Consecutive failures transition `healthy` to `degraded`; a
reviewed threshold terminates the child and reuses the existing bounded restart path. A sustained healthy
window resets restart pressure. Exhausting the restart budget writes a terminal failed record, preventing
an unbounded crash loop. The CLI actively probes the supervisor and reports `stale` when a persisted
healthy record cannot be confirmed.

The existing runtime log reader merges retained numeric rotations and the active file under one global
byte and line budget. Agent-facing results expose only relative labels, while the local files remain
private and redacted.

## Commit and verification failure evidence

The existing profile runner timestamps each reviewed command and returns stage and cumulative durations
on success or the first failing stage on error. The existing commit path annotates Git add, staged-diff,
commit-hook, and summary failures with bounded redacted subprocess evidence. If a failed hook changes the
workspace fingerprint, the prior verification receipt is invalidated before the failure is returned.
No readiness or incident MCP tool is introduced; the enhanced evidence travels through the existing
`workspace_run_profile`, `workspace_commit`, runtime status, and runtime-log surfaces.

## Diagnostics bundle

`rf diagnostics bundle` writes a private metadata-only JSON document containing:

- accepted and active generation metadata and hashes;
- redacted runtime metadata;
- startup capability and remediation summaries;
- aggregate operation metrics;
- an explicit exclusion manifest.

The bundle excludes configuration bodies, repository file content, patches, pull-request bodies,
process environment, credentials, and runtime log content. Recursive redaction is applied again at
the final bundle boundary.

## Capability discovery

`rf doctor` and diagnostics report:

- Git and GitHub CLI availability and versions;
- GitHub authentication;
- tunnel-client availability and version;
- configured repository validity, branch, clean state, remote, and base reachability;
- declared package manager/runtime and verification-profile executables;
- workspace and state-root writability;
- actionable remediation for failed checks.

Discovery inspects metadata only. It never executes a detected repository verification command.

## Configuration

The following optional `[server]` settings are positive integers:

```toml
[server]
audit_max_bytes = 5000000
audit_backup_count = 3
runtime_log_max_bytes = 5000000
runtime_log_backup_count = 3
idempotency_stale_seconds = 900
idempotency_lock_timeout_seconds = 2
```

Existing configuration remains valid because every setting has a production default.

## Verification contract

Phase 6 is complete only when all of the following pass from a clean checkout:

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy --strict src/repoforge
uv run pytest --cov=repoforge --cov-branch --cov-report=term-missing
uv build
```

The regression suite includes recursive redaction, bounded audit/log retention, stable error
rendering, corruption handling, first-call/replay equivalence, real cross-process idempotency, CLI
diagnostics, MCP compatibility, and complete workspace create/push/draft-PR lifecycle coverage.
