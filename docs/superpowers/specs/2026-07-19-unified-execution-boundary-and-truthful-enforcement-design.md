# Unified Execution Boundary and Truthful Enforcement Design

**Status:** Implemented on the rebuilt Forge v2 branch; pending final production gate and publication  
**Date:** 2026-07-19  
**Source:** Revalidated from `ai/unified-execution-boundary-spec-dc59d003db` against Forge v2 `main` at `9f01d7c691c4891055988a582c1cf83234fd82d8`  
**Program:** Fast, Reproducible Execution / Security and Trust

## Decision

RepoForge uses `ExecutionCoordinator` as the required application boundary for every command that executes repository-controlled code or tooling. The selected `ExecutionEnvironmentPort` is a private backend contract. Profile, diagnostic, ad-hoc, formatter, hygiene, binary snapshot materialization, and accepted-plan execution may not invoke the native command executor directly.

Every execution carries a typed **requested policy** and returns a typed **effective policy**, environment identity, and per-control enforcement assessment. Requested labels are intent, not proof. Native execution truthfully reports host-inherited network and host-account filesystem access, with advisory enforcement for controls it cannot isolate.

This design does not add arbitrary shell syntax, credentials, mounts, new egress, Docker, or microVM execution. It creates the seam required to add stronger backends later without leaving host-execution bypasses.

## Required invariants

1. `ApplicationContext.execution` is required; application runners have no optional raw-executor fallback.
2. One coordinator owns `prepare → exact argv admission → execute → inspect → collect artifacts → cleanup`.
3. Cleanup is exactly once, including admission failure and command failure.
4. `ENFORCEMENT_REQUIRED` fails before process start when the backend cannot satisfy the request.
5. Native identity reports effective host behavior rather than copying requested network/filesystem labels.
6. Timeout, output bounds, and process cleanup report their actual enforcement level. CPU, memory, disk, subprocess-count, and network-byte controls are never represented as enforced when unsupported.
7. Verification reuse and iteration cache hits require exact current identity and policy binding. Legacy evidence may explain a miss but may never grant a hit.
8. Commit eligibility binds the exact workspace fingerprint, profile, environment identity, requested policy, effective policy, and active configuration. Commit re-inspects current execution truth and rejects drift.
9. Public outputs expose one bounded, closed `execution_evidence` shape. Inputs gain no backend, mount, environment, credential, or shell controls.
10. Push remains non-force and publication remains draft-only; this design never merges.

## Architecture

```text
Reviewed command surface
  ├── profile
  ├── diagnostic
  ├── ad-hoc
  ├── formatter / hygiene
  └── accepted execution plan
          │
          ▼
ExecutionRequest compiler
  ├── exact reviewed argv set
  ├── workspace/snapshot scope
  ├── requested network/filesystem/resource policy
  ├── output and timeout bounds
  ├── failure mode
  └── artifact paths
          │
          ▼
ExecutionCoordinator
  ├── prepare_session
  ├── exact argv admission
  ├── execute_in_session / execute_bytes_in_session
  ├── inspect_session
  ├── collect_session_artifacts
  └── cleanup_session exactly once
          │
          ▼
ExecutionEnvironmentPort (private backend)
  └── NativeReviewedAdapter today
```

The coordinator, not a runner or adapter caller, owns lifecycle ordering. Application code receives receipts and inspections; it does not reach around the coordinator to select subprocess behavior.

## Requested policy versus effective behavior

`RequestedExecutionPolicy` expresses the reviewed intent for network, filesystem, resources, and whether advisory execution is acceptable. `EffectiveExecutionPolicy` records what the selected backend actually provides. `EnforcementAssessment` reports each control as `enforced`, `advisory`, `observed`, `unsupported`, or `not_applicable`.

For the native backend:

- requested `offline` does not imply network isolation; effective network is `host_inherited`;
- requested `source_read` or `workspace_write` does not imply mount isolation; effective filesystem is `host_account_access`;
- reviewed argv, timeout, output bounds, cancellation, and descendant cleanup remain enforced by the native process boundary where supported;
- unsupported resource ceilings remain explicit and degrade reuse eligibility when required by policy.

## Identity, receipts, and cache

Environment identity schema v2 binds adapter kind and capability hash, platform/toolchain facts, working-directory policy, requested-policy hash, effective-policy hash, effective network/filesystem values, and enforcement assessment.

Verification receipts persist:

- environment identity hash;
- requested policy hash;
- effective policy hash;
- exact verification profile and workspace fingerprint;
- command evidence and command-source integrity facts.

Accepted-plan stage receipts use schema v2 and carry identity schema version plus requested/effective policy hashes. Iteration-cache schema v2 includes the same dimensions. A compatible schema-v1 entry returns `environment_identity_schema_changed`; it is never reused as a hit and is not silently rewritten.

Read-only non-final stages may be reusable only under exact v2 bindings. Mutating and final stages are never cacheable.

## Surface routing

- **Profiles:** one coordinated session covers the reviewed multi-step command set and post-run inspection.
- **Diagnostics:** selector resolution happens before request compilation; exact resolved argv is admitted by the coordinator.
- **Ad-hoc:** strict mode remains disabled; relaxed mode still admits only configured runner shapes and never satisfies the commit gate.
- **Formatter/hygiene:** text formatter commands and bounded binary `git archive` materialization use coordinated sessions. Unsafe archives fail before formatter execution.
- **Plans:** delegated profile/diagnostic results are the source of execution truth. Plan cache and stage receipts consume their evidence instead of a synthetic platform digest.

## Public evidence

Execution-capable v2 outputs use one closed model:

- adapter kind and identity schema version;
- environment, requested-policy, and effective-policy hashes;
- requested and effective network/filesystem values;
- degraded flag;
- bounded enforcement evidence for every modeled control;
- bounded warnings.

Unknown fields are rejected. Tool count, tool names, annotations, and input schemas remain unchanged. The canonical profile call and `workspace_verify` compatibility surface return the same execution evidence.

## Failure and recovery semantics

- Policy mismatch or unavailable evidence fails closed with a typed error before a new command is admitted where possible.
- Post-run policy or identity drift invalidates evidence and prevents cache/commit reuse.
- A command that mutates the workspace invalidates a prior verification receipt.
- One fresh authoritative verification on the exact current tree replaces legacy or stale evidence.
- Failures preserve bounded, redacted evidence and safe next actions; raw environment values, credentials, source bodies, patches, backend logs, and process trees are not persisted as public evidence.

## Non-goals

- selecting a container or VM backend from a public tool input;
- arbitrary shell execution;
- introducing new credentials, network permissions, mounts, or external writes;
- treating requested policy as enforcement proof;
- allowing legacy receipts or caches to satisfy current gates;
- changing non-force push, protected-branch, or draft-PR-only publication policy.

## Acceptance criteria

Implementation is complete when all of the following hold on one exact final tree:

- every repository-code command path routes through required coordinator wiring;
- application-layer searches prove no raw `commands.run`, backend `execute`, or optional fallback remains;
- native results truthfully report advisory host network/filesystem behavior;
- enforcement-required mismatch fails before process start;
- profile, diagnostic, ad-hoc, formatter/hygiene, and plan results carry bounded execution truth;
- verification receipt and commit gate reject source, environment, policy, adapter, profile, or config drift;
- stage receipt and iteration-cache schema v2 tests pass, including legacy miss classification;
- the static 28-tool roster and all input schemas remain unchanged;
- generated tool-schema and release-contract drift checks pass;
- formatter, Ruff, strict Mypy, full tests, build, packaged smoke tests, and production verification pass;
- branch is pushed without force and only a draft PR is created or updated.
