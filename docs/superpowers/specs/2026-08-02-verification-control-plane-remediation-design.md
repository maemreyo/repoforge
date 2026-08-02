# Verification Control Plane Remediation Design

## Status

Approved for implementation from the independent architecture review requested by the repository owner.

## Problem

RepoForge currently conflates developer feedback, full regression, coverage, compatibility validation, and release verification. That makes the default loop expensive, duplicates the same signal across CI jobs, and charges every explicit verification request for a rich assessment that is not needed to execute a known profile or diagnostic.

The remediation must reduce time-to-signal without weakening the fail-closed identity and release guarantees that protect workspace mutations, durable operations, activation, packaging, and public contracts.

## Goals

1. Make the default developer test command affected and explainable.
2. Preserve a full behavioral suite and a canonical branch-coverage suite as separate intents.
3. Ensure one protected SHA runs at most one canonical full-coverage suite.
4. Treat test-selection and isolation metadata as load-bearing validated input.
5. Introduce a declarative test catalog and immutable verification plan while retaining the existing selector as a migration backstop.
6. Avoid rich workspace assessment for explicit profile and diagnostic runs.
7. Add bounded performance evidence for selection, execution, and preflight.

## Non-goals

- Removing the authoritative production gate.
- Lowering the branch coverage floor.
- Replacing pytest, Python, the modular monolith, or ports/adapters.
- Cutting over selection authority without shadow validation.
- Migrating every serial test to resource-aware scheduling in one unsafe change.

## Design Principles

- Correctness remains fail-closed. Unknown blast radius widens deliberately and records why.
- Every execution binds to immutable head, fingerprint, configuration, policy, catalog, and environment identities where applicable.
- Local feedback and release authority are distinct products with distinct budgets.
- Selection metadata must validate before it can influence concurrency or omission.
- Migration remains reversible at each slice.

## Command Contracts

### `make test`

Runs affected tests plus the safety bundle without coverage. It is the default developer and model feedback path.

### `make test-full`

Runs the complete behavioral suite without coverage using the validated lane plan.

### `make coverage`

Runs the complete suite once with branch coverage and the repository coverage floor. It emits reusable coverage observations.

### `make verify`

Runs contracts, gates, formatting, lint, strict type checking, metadata validation, and affected tests. It does not run full coverage or packaging.

### `make gate`

Runs the authoritative production verification: static contracts, canonical coverage, compatibility checks, live activation, package build, and installed-wheel smoke. It remains operator/CI only.

Existing aliases may remain for one transition release, but help text and profiles must publish the new semantics unambiguously.

## Test Catalog

Introduce `tests/catalog.toml` as a versioned declarative authority generated initially from `tests/test-groups.toml` and `tests/coverage-map.json`.

Each entry declares:

- test file identity;
- owning capability;
- test kind;
- isolation class;
- named resources;
- supported platforms and Python versions;
- cost class;
- covered contracts where known.

Source ownership is declared by capability patterns. Cross-capability dependencies are explicit. The catalog compiler validates:

- every test file has exactly one owner;
- every source path is owned or explicitly exempt;
- every referenced file exists;
- every isolation and resource value is valid;
- catalog rendering and digest are deterministic.

The existing selector remains the execution authority during report-only migration. The new planner emits a shadow plan and mismatch report. Cutover requires zero false negatives across the agreed evidence window.

## Verification Plan

The planner consumes intent, changed paths, catalog identity, source ownership, explicit dependency edges, safety bundle, and environment capabilities. It emits an immutable plan containing:

- plan and catalog digests;
- selected tests and reasons;
- widening reason when applicable;
- serial, parallel, exclusive-resource, compatibility, and system lanes;
- expected budget and selected-count telemetry.

The runner consumes the plan; it does not reload metadata to make new scheduling decisions. Invalid metadata or digest mismatch fails with a typed metadata error. The safe emergency fallback is all-exclusive execution, never implicit all-parallel execution.

## Snapshot Preflight

Introduce a `SnapshotToken` captured under the workspace lock with:

- workspace id;
- head SHA;
- full fingerprint;
- cheap validity token;
- changed paths;
- configuration generation;
- policy hash;
- capture timestamp.

Explicit profile, diagnostic, and ad-hoc modes capture the token, validate caller expectations, and admit durable work without collecting code intelligence, PR state, CI checks, or risk recommendations.

Plan and auto modes use the token as the identity anchor for an optional rich assessment. Assessment providers must not recompute the full fingerprint. Before the effect boundary, the system compares the cheap validity token; after execution it primes a new fingerprint.

## Rich Assessment

The rich assessment remains a read model for planning and automatic routing. Providers receive the same token and run with bounded deadlines. Remote provider unavailability yields typed partial evidence rather than blocking explicit execution or silently becoming a full-profile decision without a reason.

## Resource-Aware Isolation

Catalog isolation classes are:

- `pure`;
- `sandboxed_fs`;
- `sandboxed_process`;
- `exclusive:<resource>`;
- `system`.

Initial implementation preserves current serial groups by compiling them to exclusive resources. A tracer capability then migrates to unique HOME, XDG, state root, release root, socket path, git repository, and process session fixtures. Only after stress evidence may broad serial flags be narrowed.

## CI Topology

Pull requests run:

1. static contracts and metadata validation;
2. affected canonical tests;
3. path- or catalog-triggered compatibility and live-activation checks.

Protected branch pushes run:

1. one canonical full branch-coverage suite;
2. no-coverage compatibility shards for remaining Python/OS cells;
3. live activation;
4. package and installed-wheel smoke;
5. the stable umbrella `production-gate`.

Coverage-map observations and performance evidence are derived from the canonical coverage job. No separate full-suite job may exist only to rebuild selection metadata.

A workflow input or policy switch must restore the previous full matrix for emergency rollback.

## Performance Ledger

Every affected, full, coverage, and gate run emits bounded JSON with:

- intent, environment, head, plan, and catalog identities;
- selected count and widening reason;
- lane and stage durations;
- slowest test files or nodes available from pytest timing;
- fingerprint source and duration;
- assessment provider durations;
- cache and reuse outcomes.

The first release is report-only. Blocking budgets are introduced only for deterministic gross regressions after a baseline window.

Initial targets:

- warm explicit verification preflight p95 below 2 seconds;
- cold explicit verification preflight p95 below 5 seconds;
- affected developer test p95 below 120 seconds on the reference environment;
- one canonical full-coverage suite per protected SHA;
- at least 50% CI compute-minute reduction after compatibility cutover.

## Typed Failures

Add or reuse typed errors for:

- invalid selection metadata;
- stale catalog or plan identity;
- stale snapshot;
- assertion failure;
- infrastructure failure;
- contention or leaked resource;
- coverage failure;
- compatibility failure;
- release-effect failure.

Operator guidance must identify the safe next action and whether rerunning, widening, refreshing, or restoring a known-good plan is appropriate.

## Migration Slices

### Slice 1 — Correct command baseline

Add `test-full`, `coverage`, and `gate`; change `test` to affected execution; update profiles, help, docs, and command-drift tests. Keep the production gate unchanged.

### Slice 2 — Fail-closed lane metadata

Change the full-suite runner to reject invalid manifest/catalog metadata or use a safe all-exclusive fallback. It must never widen concurrency when metadata is unreadable.

### Slice 3 — Snapshot preflight

Add `SnapshotToken` and route explicit profile/diagnostic/ad-hoc verification through minimal preflight. Keep plan/auto on rich assessment initially. Prove one full fingerprint maximum before admission.

### Slice 4 — Catalog and shadow planner

Generate catalog v1, validate it, compile a deterministic plan, and compare it with the existing selector in report-only mode.

### Slice 5 — Performance evidence

Emit bounded selection, preflight, lane, and slow-test artifacts. Add tests for deterministic schema and synthetic slowdown/widening evidence.

### Slice 6 — CI de-duplication

Create one canonical coverage job and no-coverage compatibility shards. Reuse canonical observations for map drift. Preserve the umbrella gate and emergency full-matrix switch.

### Slice 7 — Resource isolation tracer

Migrate one high-cost serial capability to sandbox/resource declarations, stress it, and measure critical-path reduction. Preserve exclusive fallback.

### Slice 8 — Planner cutover and legacy removal

After the shadow evidence gate, enable catalog planning for low-risk capabilities and expand gradually. Remove dynamic coverage-map authority, manual overrides, duplicate workflows, and broad serial flags only when their replacements are proven.

## Testing Strategy

Every production behavior change follows RED, GREEN, REFACTOR.

Required layers:

- unit tests for command semantics, catalog validation, plan determinism, snapshot identity, and failure classification;
- integration tests for affected/full/coverage runners and durable admission;
- fault tests for corrupt metadata, stale snapshots, leaked processes, and partial provider evidence;
- workflow contract tests for one canonical coverage suite and umbrella dependency semantics;
- shadow comparison tests and seeded dependency/compatibility defects;
- final full verification and production gate evidence before merge.

## Rollback

Each slice is additive or controlled by explicit policy:

- command aliases can map back to full no-coverage execution;
- snapshot preflight can route back to rich assessment;
- catalog planner can remain report-only or revert to the old selector;
- resource classes can revert to exclusive execution;
- CI can enable the full matrix switch;
- performance thresholds can return to report-only.

Rollback must never delete tests, lower coverage, bypass identity binding, or change production state without the existing durable and reviewed effect boundary.

## Completion Criteria

The remediation is complete only when:

1. all command contracts and profile permissions match this design;
2. invalid test metadata cannot produce widened concurrency;
3. explicit verification no longer collects rich assessment evidence;
4. catalog completeness and deterministic plan tests pass;
5. performance artifacts are emitted and bounded;
6. protected CI runs one canonical coverage suite and compatibility shards;
7. at least one serial capability is safely resource-isolated;
8. shadow selection evidence supports planner cutover or the old selector remains explicitly retained with documented blockers;
9. the exact final tree passes the repository verification gate and production gate.
