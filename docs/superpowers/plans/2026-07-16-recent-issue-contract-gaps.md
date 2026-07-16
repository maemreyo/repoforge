# Recent Issue Contract Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the verified implementation gaps in issues #138, #139, #144, #145, and #146, and reconcile the checked-in roadmap with live closed-issue state, using one isolated worktree.

**Architecture:** Preserve existing public tool inputs and synchronous result shapes. Fix behavior at existing boundaries: one outer audit choke point, a private persistent LRU cache, schema-compatible metrics counters, a dedicated private durable operation-result store plus atomic admission lock, and workspace-aware commit history selection. Do not modify runtime deployment/restart behavior or perform unrelated formatting cleanup.

**Tech Stack:** Python 3.10+, dataclasses, JSON persistence, existing `LockManager`/`AtomicJsonFileStore`, pytest, RepoForge verification profiles.

## Global Constraints

- Work only in workspace `fix-recent-issue-contrac-7e7483ebb9`.
- Do not fix the pre-existing repository-wide formatting gate from review item (1).
- Do not restart, reload, or modify the deployed MCP runtime from review item (2).
- Preserve path, branch, audit-redaction, output-bound, and exact-tree verification invariants from `AGENTS.md`.
- Write a failing regression test before each production-code fix.
- Keep `operation_list` compact; only `operation_status` may resolve a stored structured result.
- Update generated release-contract artifacts only when the MCP schema actually changes.

---

### Task 1: Restore one-event auditing for `repo_issue_next`

**Files:**
- Modify: `tests/test_repo_issue_graph_tools.py`
- Modify: `src/repoforge/application/repository/issue_next.py`

**Interfaces:**
- Consumes: `ApplicationContext.audited`, `ApplicationContext.repo`, `_read_live_states`.
- Produces: one `repo_issue_next` audit event for success/failure and no nested `repo_issue_next_live` event.

- [ ] Add a regression assertion that a successful call creates exactly one audit event across all `repo_issue_next*` actions.
- [ ] Add a regression test that an unknown `repo_id` creates one failed `repo_issue_next` audit event.
- [ ] Run the two tests and confirm they fail for nested auditing/pre-audit repository validation.
- [ ] Make `_read_live_states` pure and move repository resolution inside the outer audited operation.
- [ ] Run `uv run pytest tests/test_repo_issue_graph_tools.py -q` and confirm green.

### Task 2: Implement true persistent LRU eviction for GitHub reads

**Files:**
- Modify: `tests/test_github_read_cache.py`
- Modify: `src/repoforge/adapters/persistence/json_github_read_cache.py`
- Modify: `CHANGELOG.md`
- Modify: `docs/development/TOOL_REFERENCE.md`

**Interfaces:**
- Consumes: `GitHubReadCache.get/put`, `LockManager`.
- Produces: cache entries with `stored_at` and `last_accessed_at`; eviction orders by last access, with legacy fallback to `stored_at`.

- [ ] Replace the insertion-order eviction test with an access-refresh test: access A after storing B, insert C, and assert B is evicted.
- [ ] Run the test and confirm the current implementation evicts A incorrectly.
- [ ] Update `get` to refresh `last_accessed_at` atomically under the cache lock and `put` to initialize it.
- [ ] Preserve TTL age from `stored_at`, corrupt-entry fallback, private permissions, and bounded entry size.
- [ ] Run `uv run pytest tests/test_github_read_cache.py -q` and confirm green.

### Task 3: Compute average result size from observed payloads only

**Files:**
- Modify: `tests/test_audit_query.py`
- Modify: `tests/test_phase6_operational_hardening.py`
- Modify: `src/repoforge/adapters/observability/json_metrics.py`
- Modify: `src/repoforge/adapters/audit/query.py`
- Modify: `README.md`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `JsonMetricsSink.record`, `summarize_operation_metrics`.
- Produces: `result_bytes_count` in lifetime/day stats; `result_bytes_avg = total / observed_count`; legacy v1/v2 fallback derives an initial observed count from successful calls only when byte aggregates exist.

- [ ] Add a regression test with one 10,000-byte success and nine failures; expected average is 10,000, not 1,000.
- [ ] Add a regression test for an unserializable successful result not increasing the observed count.
- [ ] Run tests and confirm the current denominator fails.
- [ ] Add and merge `result_bytes_count`, bump the metrics schema, and preserve v1/v2 migration.
- [ ] Run `uv run pytest tests/test_audit_query.py tests/test_phase6_operational_hardening.py -q` and confirm green.

### Task 4: Persist background profile results and make admission atomic

**Files:**
- Create: `src/repoforge/ports/operation_result_store.py`
- Create: `src/repoforge/adapters/persistence/json_operation_result_store.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/application/operations/dto.py`
- Modify: `src/repoforge/application/operations/status.py`
- Modify: `src/repoforge/application/operations/manager.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `tests/test_background_run_profile.py`
- Modify: `tests/test_operation_tasks.py`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `OperationResultStore.save(operation_id, result)`, `.read(operation_id)`, `.delete(operation_id)`; private atomic JSON under `state_root/operation-results`.
- Produces: `OperationStatusView` containing the existing summary fields plus `result: dict[str, Any] | None`; `operation_list` remains summary-only.
- Produces: atomic global admission using lock key `background-profile-admission` around count + durable create/start.

- [ ] Add a failing test that completed background status returns the full structured profile result and that it survives rebuilding the service.
- [ ] Add persistence safety, permissions, and size tests for the result store.
- [ ] Add a deterministic concurrent-admission regression test with `max_background_profiles = 1`, delayed counting, and two workspaces; exactly one admission must succeed.
- [ ] Run the new tests and confirm missing-result/racy-admission failures.
- [ ] Implement and wire the result-store port/adapter.
- [ ] Save the audited `WorkspaceRunProfileResult` before marking the operation succeeded; clean up on failed transition and delete with expired operation records.
- [ ] Resolve the result only in `operation_status`.
- [ ] Guard count + create/start with the global admission lock while retaining the per-workspace lock.
- [ ] Run `uv run pytest tests/test_background_run_profile.py tests/test_operation_tasks.py -q` and confirm green.

### Task 5: Use workspace branch history in `repo_task_context`

**Files:**
- Modify: `tests/test_repo_task_context.py`
- Modify: `src/repoforge/application/repository/recent_commits.py`
- Modify: `src/repoforge/application/repository/task_context.py`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `RecentCommitsReader.compute_from_path(repo_id, path, limit)`.
- Behavior: with `workspace_id`, recent commits come from the validated workspace path; without it, they come from the configured source/default branch as before.

- [ ] Add a failing test that commits only in the workspace and expects that commit first in the task-context bundle.
- [ ] Run the test and confirm current source-clone history is returned.
- [ ] Add the path-aware pure reader method and reuse the already validated workspace path.
- [ ] Run `uv run pytest tests/test_repo_task_context.py -q` and confirm green.

### Task 6: Reconcile roadmap state and verify the combined change

**Files:**
- Modify: `docs/roadmaps/REPOFORGE_TICKET_GRAPH.json`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Closed issues verified on 2026-07-16: #137, #138, #139, #142, #143, #144, #145, #146.
- Open issues #134, #135, #136 remain `In progress`; #140/#141/#147 remain unchanged.

- [ ] Change the eight verified closed nodes to `Done` without removing historical dependency edges.
- [ ] Run `uv run python scripts/validate_ticket_graph.py`.
- [ ] Run all narrow suites from Tasks 1–5 together.
- [ ] Review `workspace_diff` for scope, safety, generated artifacts, and accidental formatting-only changes.
- [ ] Run the repository `full` verification profile and record any pre-existing failure separately from regressions introduced by this branch.
