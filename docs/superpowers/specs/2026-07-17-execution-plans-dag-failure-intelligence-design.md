# Execution Plans, Verification DAG, Cache, and Failure Intelligence Design

## Scope

This design implements GitHub issues #20, #21, #45, and #46 on the current RepoForge v1 architecture. It deliberately does not modify Forge v2 contracts, schemas, tool families, or the Forge v2 implementation worktree.

## Goals

1. Freeze one current workspace assessment and its verification recommendation into an immutable, content-addressed execution plan.
2. Execute an accepted plan through iteration or the final gate while preserving RepoForge's existing authoritative verification receipt.
3. Compile plan stages into a deterministic DAG and safely reuse compatible read-only iteration results.
4. Normalize execution failures into bounded, secret-safe, durable evidence with typed recovery actions.

## Non-goals

- No arbitrary command input, shell execution, force push, merge, or policy bypass.
- No replacement of `workspace_run_profile` or `workspace_run_diagnostic`.
- No cached final verification and no cache-backed commit eligibility.
- No new public plan-management tools. The only new public operation is the issue-specified `workspace_execute_plan`.
- No Forge v2 tool/schema changes.

## Architecture

### Domain

`execution_plan.py` owns immutable plan, stage, acceptance, validation, hashing, and stale-reason models. Plans bind exact HEAD, workspace fingerprint, configuration digest, policy hash, assessment snapshot identity, deterministic risk hash, deterministic recommendation hash, and stage-definition hash.

`verification_dag.py` validates stage dependencies and returns a stable topological order. It also owns deterministic iteration-cache keys and cache compatibility results.

`execution_receipt.py` owns durable stage receipts and final plan-execution summaries. Receipts contain IDs, hashes, status, bounded result references, artifact digests, timing, and exact pre/post identities; they never contain source or raw unbounded output.

`failure_intelligence.py` owns normalized failure classes, typed safe recovery actions, bounded evidence, deterministic evidence IDs, and conservative structured-first classification.

### Persistence

One adapter module uses the existing `JsonStateRepository` substrate for five private collections:

- `execution-plans`
- `execution-plan-acceptances`
- `execution-stage-receipts`
- `iteration-cache`
- `failure-evidence`

This preserves atomic writes, private permissions, schema-version fail-closed behavior, bounded records, deterministic serialization, and revision CAS. Plan and receipt records are immutable; cache records may be deleted only through bounded retention APIs.

### Application services

`ExecutionPlanService` builds a plan from `WorkspaceAssessmentReader`, resolves every recommendation stage against reviewed repository profiles/diagnostics, persists the immutable plan, and records a separate acceptance. Plan creation/acceptance remain internal service methods in v1.

`WorkspacePlanExecutor` creates one durable `OperationTask`, runs in the existing background-task adapter, validates the plan before each stage, calls the existing profile/diagnostic runners, records progress and stage receipts, consults the iteration cache only for compatible read-only non-final stages, and stores a bounded final operation result.

The final profile is always executed through `WorkspaceProfileRunner`; only its existing exact-tree verification receipt can satisfy the commit gate.

### Locking and cancellation

Admission acquires the workspace lock for the full background plan execution, matching background profile behavior. Internal stage runners receive an execution path that reuses the already-held lock rather than acquiring a second process lock. A shared cancellation token is checked before each stage and passed to profile command execution. Diagnostic cancellation remains cooperative at stage boundaries because the existing diagnostic runner has no subprocess cancellation port; this limitation is explicit in receipts.

### Cache policy

A cache entry is eligible only when all of the following are true:

- stage boundary is iteration;
- stage is read-only;
- stage is not the final profile;
- environment identity is complete and cache-eligible;
- exact workspace/config/policy/provider/stage/dependency bindings match;
- referenced artifacts still exist and match their digests.

Mutating stages and final verification are always misses with stable reasons. A hit emits a new current stage receipt referencing the reused cache record; it never writes a verification receipt.

### Failure intelligence

Classification prefers structured error code, failed-step kind, diagnostic failure class, cancellation and mutation evidence. Bounded text heuristics are fallback only. Every failure is redacted before persistence. Recovery actions reference existing typed operations such as `workspace_status`, `workspace_run_diagnostic`, `workspace_run_profile`, and `workspace_refresh_preview`; arbitrary argv is never emitted.

## Public contract

Add one v1 MCP tool:

```text
workspace_execute_plan(workspace_id, plan_id, through = "full")
```

It returns a durable operation reference. Tool annotations are local mutating, non-destructive, and non-open-world. The release contract and tool reference are updated. Forge v2 schema goldens remain untouched.

## Verification strategy

Each issue lands as a separate commit:

1. #20 plan domain/store/service.
2. #21 durable execution and one MCP operation.
3. #45 DAG/cache.
4. #46 failure evidence.

Every slice uses RED/GREEN focused tests. The final exact tree runs formatter, quick checks, the authoritative full production gate, package/wheel lifecycle, and exact-SHA CI after publication.
