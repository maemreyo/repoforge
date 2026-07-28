# Durable Execution and Operator Ergonomics Design

**Status:** Slice A implemented and verified (2026-07-28); Slices B and C pending  
**Date:** 2026-07-27  
**Scope:** Existing Forge v2 28-tool surface

## Decision

Every verification execution is admitted as a durable, serializable work item. No repository command runs on an MCP request thread. `workspace_verify(background=false)` remains a convenience API, but it only performs a bounded wait on the durable operation; a client timeout or disconnect never owns or destroys execution state.

This is the selected all-durable design. A duration-threshold hybrid is rejected because command duration cannot be predicted reliably and would preserve the exact foreground starvation failure this work is intended to remove.

The program is delivered in three independently testable slices:

1. durable execution plane and truthful operation lifecycle;
2. typed refresh resolution and generated-artifact regeneration;
3. bounded evidence transport and remaining operator actions.

The slices share operation and artifact contracts, but each has its own acceptance tests and commit boundary.

## Observed failures

The design is based on reproduced runtime behavior, not hypothetical risk:

- a foreground `workspace_verify` exceeded the HTTP deadline and temporarily prevented status and operation calls from completing;
- a background operation reported `state=running` while its phase remained `queued`, then later became `orphaned` with `OPERATION_WORKER_LOST`;
- another full profile held the workspace for more than fifteen minutes while reporting only `0/1, running unknown`; cancellation acknowledged quickly, but the terminal record retained `phase=running` and no result reference;
- current background submission persists an operation but schedules an in-process Python closure on a daemon thread, so the command body cannot survive process restart;
- refresh conflict application requires complete replacement content, including generated files, and formatter changes can invalidate the reviewed transaction;
- large preview/read responses are truncated or redacted inline, which can make code evidence non-round-trippable;
- activation identity, receipt and build fingerprint cannot be read back independently after activation through the current status surface.

## Goals

- Keep control-plane reads and cancellation responsive while long commands execute.
- Preserve an authoritative terminal verdict across client disconnect and runtime restart.
- Represent queued, claimed, running, cancelling and terminal phases truthfully.
- Prevent duplicate execution under concurrent workers.
- Requeue work that was never started; fail closed when retry safety is unknown.
- Make step identity, heartbeat age and result artifacts observable.
- Let refresh callers choose typed conflict strategies without reconstructing entire files.
- Regenerate generated conflicts atomically from authoritative sources.
- Keep large/raw evidence retrievable through immutable bounded references.
- Preserve the static 28-tool roster and existing callers through additive contracts.

## Non-goals

- A distributed queue service or new database dependency.
- Arbitrary shell, environment, credential, network or mount controls.
- Automatic retry of mutating work after an ambiguous child-process outcome.
- Increasing HTTP or inline output budgets as the primary fix.
- Adding a twenty-ninth MCP tool.

## Slice A: durable execution plane

### Serializable work envelope

Introduce a schema-versioned `OperationWorkItem`, stored separately from the public operation record and keyed by `operation_id`. It contains only execution data required to reconstruct reviewed work:

- operation kind and workspace ID;
- exact head SHA and workspace fingerprint;
- active configuration generation;
- reviewed profile, diagnostic or ad-hoc request shape;
- normalized argv and working directory after admission;
- requested execution policy and mutability classification;
- attempt count and maximum safe attempts;
- created, available, claimed and heartbeat timestamps;
- worker ownership token and lease deadline;
- result artifact reference after completion.

Secrets, environment values, source bodies and inline output are excluded. Persisted request fields are sanitized and bounded.

### Durable store and atomic claim

Extend the operation persistence adapter with a work-item store using the existing atomic JSON, permissions, fsync and cross-process lock conventions. Required operations are:

- create operation and work item atomically from the caller's perspective;
- claim the oldest available compatible item with compare-and-swap;
- renew a worker lease;
- publish progress and result references;
- release or requeue an unstarted item;
- terminalize and remove or tombstone completed work.

Two workers racing for the same item must produce one claim winner. The execution idempotency key is the operation ID plus attempt number.

### State truth

Operation creation produces `queued`, not `running`. The worker transitions the record only after a successful claim:

```text
queued -> claimed -> running -> succeeded | failed | cancelled
                    -> cancelling -> cancelled
queued -> cancelled
claimed -> queued        only when no child was started and the lease was lost
running -> orphaned      when child outcome is ambiguous
```

If the public domain model keeps a smaller state enum, `claimed` and `cancelling` are phases under `running`; the externally visible state/phase pair must still be truthful. A terminal operation may never retain `phase=running`.

### Dedicated worker process

The managed runtime starts a dedicated execution worker loop separately from MCP dispatch. It claims persisted work, acquires the workspace lock, revalidates exact state, starts the coordinated execution session, binds child identity, renews leases and records terminal evidence.

The MCP process performs validation and persistence only. It never owns a command thread or an unpersisted callable. A slow command therefore cannot starve tool dispatch.

### Foreground compatibility

`background=true` returns after durable admission.

`background=false` admits the same work item and long-polls its operation for a bounded server-side interval. If it becomes terminal, the normal verification result is returned. Otherwise the response returns `outcome=running`, the operation ID and polling guidance. Execution continues independently.

A client timeout or disconnect affects only the wait, not the job.

### Recovery policy

At startup and periodically:

- expired queued leases are made available again;
- claimed work with no child binding is requeued;
- read-only verification with a known-dead child may retry once only when the exact fingerprint and configuration generation still match;
- mutating or ambiguous running work becomes `orphaned` with bounded failure evidence;
- stale ownership cannot renew or terminalize a newer attempt;
- terminal records without an artifact reference are marked evidence-incomplete rather than reported as fully successful.

### Cancellation

- queued: atomically terminalize as cancelled and remove the work item;
- claimed before spawn: cancel before process creation;
- running: mark cancelling, signal the bound process group, reap it, then terminalize;
- worker loss during cancelling: recovery completes cancellation when child identity is known dead, otherwise reports orphaned ambiguity.

Target latency for queued cancellation is under one second under normal local load. Repeated cancellation is idempotent.

### Progress and receipts

Profile execution publishes one durable step record per reviewed command. Operation output includes:

- current step ID and safe display name;
- completed and total steps;
- step and cumulative elapsed time;
- heartbeat timestamp and computed heartbeat age;
- current attempt;
- terminal result artifact reference.

`running unknown` is allowed only before the first step is resolved and must not persist once the runner has selected a profile command.

## Slice B: typed refresh resolution

### Resolution contract

Extend each conflict resolution additively:

```text
path
strategy: ours | theirs | content | patch | regenerate
content?            required only for content
patch?              required only for patch
content_reference?  optional immutable raw artifact reference
```

Existing `{path, content}` callers normalize to `strategy=content`.

`ours` and `theirs` use the exact reviewed merge stages. `patch` is applied against an exact conflict-stage digest. `regenerate` is accepted only for paths classified by a registered deterministic generator.

### Generated artifacts

Preview groups generated conflicts by generator and authoritative inputs. Apply resolves source conflicts first, invokes each generator once in a temporary transaction tree, verifies declared outputs and atomically installs the generated set.

Generated output supplied manually is rejected by default when a registered generator is available. The result includes generator identity, input digests, output paths and a bounded regeneration receipt.

### Preflight canonicalization

Before opening the final apply transaction, candidate resolutions run the configured formatter/linter in a temporary tree. Deterministic formatter changes become part of the reviewed candidate and plan hash. Non-deterministic or scope-expanding changes fail preview with actionable evidence.

This prevents a formatter from changing content after the exact-state transaction has begun.

### Bounded preview

The default preview returns conflict metadata, path status, line counts, generator classification and per-conflict evidence references. Full conflict bodies and generated JSON are opt-in by path and byte budget.

## Slice C: evidence transport and operator actions

### Immutable raw artifacts

Large command output, patches, conflict bodies and exact file content can be stored as immutable local artifacts. Public results carry a bounded reference with digest, byte count, media type, redaction status and expiry/retention class.

Inline fields remain redacted and bounded. A raw artifact is returned only through an existing authorized read/evidence action, with range/cursor support and exact digest binding. Raw code retrieval must preserve bytes and be round-trippable; UI display redaction must never be substituted into mutation input.

### Read ergonomics

- allow multiple ranges from the same path in one batch;
- return successful items plus typed per-item failures instead of discarding the whole batch;
- distinguish source truncation, response-budget truncation and display redaction;
- provide stable continuation for each item;
- keep diff summary-first and require narrow opt-in hunks.

### Activation observability

`config_inspect` exposes current runtime identity additively: active/observed release SHA, build fingerprint, tool-surface hash, package version, process-start identity, active generation and latest activation receipt reference. A receipt read mode verifies persisted activation facts independently of the activation command output.

### PR lifecycle

Within existing `workspace_pr`, add reviewed `ready` and `merge` actions only if repository policy explicitly permits them. Both require exact PR identity and fresh checks/review evidence. Merge remains non-force and protected-branch policy remains authoritative.

## Compatibility and rollout

- Tool names and count remain unchanged.
- New request fields are additive; legacy shapes normalize at the boundary.
- Operation and work-item schemas are versioned and migrated conservatively.
- Runtime rollout first enables durable admission and shadow worker metrics, then makes all verification durable.
- Existing in-process background execution is removed only after restart, cancellation and duplicate-claim tests pass.
- Feature flags are configuration-owned, not public tool inputs.

## Acceptance criteria

### Execution plane

- A ten-minute verification does not delay `operation`, `workspace_status`, logs or cancellation calls.
- Disconnecting the client does not stop execution or lose the terminal verdict.
- A queued job survives runtime restart and executes once.
- Two workers cannot execute the same attempt.
- A lost lease before child spawn requeues automatically.
- Ambiguous mutating work never auto-retries.
- Queued cancellation terminalizes within one second in the normal case.
- Terminal operations have terminal phases and a result or explicit evidence-incomplete status.
- Profile progress names the current step and publishes heartbeat age.

### Refresh

- Legacy content resolution remains valid.
- `ours`, `theirs`, `patch` and `regenerate` are exact-state bound.
- Generated conflicts regenerate atomically from authoritative inputs.
- Formatter output is included in preview and plan hash before apply.
- Preview stays bounded for repositories with large generated contracts.

### Evidence and operations

- Raw code artifacts round-trip byte-for-byte without display redaction tokens.
- Multiple ranges from one file work in one read call.
- One missing file does not erase successful batch reads.
- Activation SHA, fingerprint and receipt can be verified after activation.
- Existing diff summary-first behavior and static 28-tool contract remain intact.

## Verification strategy

Implementation follows red-green-refactor for each state transition and recovery branch. Required test layers are:

- domain transition and serialization tests;
- store CAS, atomicity, corruption and lease-race tests;
- worker restart, duplicate claim, cancellation and child-binding integration tests;
- MCP responsiveness tests with a deliberately blocked command;
- refresh strategy and generator transaction tests;
- artifact authorization, range, digest and redaction tests;
- contract snapshots and generated release schemas;
- focused verification per slice, then the repository full profile on one exact final fingerprint.
