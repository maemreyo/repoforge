# Execution Plans, DAG, Cache, and Failure Intelligence Implementation Plan

> Execute this plan with TDD. Keep each GitHub issue in its own commit and do not modify Forge v2 contracts or schemas.

## Task 1 — Issue #20: immutable execution plans

**Create**
- `src/repoforge/domain/execution_plan.py`
- `src/repoforge/ports/execution_plan_store.py`
- `src/repoforge/adapters/persistence/json_execution_plan_store.py`
- `src/repoforge/application/workspace/execution_plan.py`
- `tests/test_execution_plans.py`

**Modify**
- persistence/port exports, `ApplicationContext`, `AdapterOverrides`, `build_application`, `CodingService`.

Steps:
1. Write RED domain tests for deterministic plan IDs/hashes, stage normalization, cycles, missing final gate, unknown profile/diagnostic, and stale identity reasons.
2. Add RED persistence tests for private permissions, restart, corruption, unsupported schema, immutable duplicate behavior, and bounded listing.
3. Implement the domain and JSON store over `JsonStateRepository`.
4. Implement plan creation from current `WorkspaceAssessment` and separate exact-plan acceptance.
5. Run focused tests, Ruff, and strict Mypy; commit `feat(plans): add immutable execution plans`.

## Task 2 — Issue #21: durable plan execution

**Create**
- `src/repoforge/domain/execution_receipt.py`
- `src/repoforge/ports/execution_receipt_store.py`
- `src/repoforge/adapters/persistence/json_execution_receipt_store.py`
- `src/repoforge/application/workspace/execute_plan.py`
- `tests/test_plan_execution.py`

**Modify**
- profile/diagnostic runners only as required to expose an internal already-locked execution path;
- operation cancellation dispatch;
- application composition and service facade;
- MCP server, tool reference, release contract, and MCP tests.

Steps:
1. Write RED tests for iteration/full boundaries, required/optional failure, stale plan, current verification handoff, stage receipt persistence, cancellation, restart visibility, and actual MCP invocation.
2. Implement durable background operation orchestration with progress and partial receipts.
3. Delegate diagnostics/profiles to existing runners; final full stage must create the existing authoritative verification receipt.
4. Add only `workspace_execute_plan` to the public v1 tool surface.
5. Run focused tests and contract checks; commit `feat(execution): execute immutable workspace plans`.

## Task 3 — Issue #45: verification DAG and iteration cache

**Create**
- `src/repoforge/domain/verification_dag.py`
- `src/repoforge/ports/iteration_cache.py`
- `src/repoforge/adapters/persistence/json_iteration_cache.py`
- `tests/test_verification_dag_cache.py`

**Modify**
- plan compiler and executor.

Steps:
1. Write RED tests for deterministic topological order, cycles, unknown dependencies, stage hashes, hit/miss matrix, final/mutating non-cacheability, dependency receipt keys, corruption, and eviction.
2. Implement deterministic DAG compilation and cache-key construction.
3. Add read-only iteration cache lookup/write and new current receipts on hits.
4. Ensure cache hits never create commit eligibility and final verification always runs.
5. Run focused tests; commit `feat(verification): add execution DAG and iteration cache`.

## Task 4 — Issue #46: failure intelligence

**Create**
- `src/repoforge/domain/failure_intelligence.py`
- `src/repoforge/ports/failure_evidence_store.py`
- `src/repoforge/adapters/persistence/json_failure_evidence_store.py`
- `tests/test_failure_intelligence.py`

**Modify**
- plan executor, operation result/status evidence, service/MCP serialization tests.

Steps:
1. Write RED table-driven classification tests covering all specified failure classes, structured-first precedence, bounds, redaction, mutation facts, flaky evidence, and safe actions.
2. Implement deterministic failure IDs, bounded evidence, and private store.
3. Persist failure evidence from failed plan stages and reference it from stage/operation results.
4. Verify no secret/source body/raw unbounded output reaches model, audit, or state.
5. Run focused tests; commit `feat(failures): add structured execution failure intelligence`.

## Final integration

1. Review exact diff and change-budget metrics.
2. Run formatter over changed paths.
3. Run all new focused suites plus service/MCP/operation/persistence/security tests.
4. Run the authoritative RepoForge full profile on the exact final tree.
5. Commit any contract/docs regeneration separately if required.
6. Push without force, open a draft PR with `Closes #20`, `Closes #21`, `Closes #45`, and `Closes #46`.
7. Watch exact-SHA CI to terminal and report any remaining limitations honestly.
