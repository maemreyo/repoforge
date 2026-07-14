# Durable PR Check Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Add a durable, cancellable, resumable exact-SHA PR check watch that returns an operation reference.

**Architecture:** Store watch-specific state separately from `OperationTask`. A coordinator validates immutable workspace and PR identity, polls checks through the existing GitHub gateway, updates durable progress, and resumes eligible watches after restart.

**Tech Stack:** Python 3.10+, dataclasses, enums, existing operation lifecycle, private JSON persistence, typed ports, FastMCP, pytest.

## Global Constraints

- No check mutation, workflow dispatch, merge, or force push.
- Persist only safe IDs, hashes, counts, selectors, and references.
- Bound timeout, polling, selectors, retries, and evidence references.
- Use test-first development and exact-tree verification.

---

### Task 1: Domain and private store

**Files:**
- Create `src/repoforge/domain/pr_check_watch.py`
- Create `src/repoforge/ports/pr_check_watch_store.py`
- Create `src/repoforge/adapters/persistence/json_pr_check_watch_store.py`
- Modify exports and `src/repoforge/domain/errors.py`
- Test in `tests/test_pr_check_watch.py`

- [ ] Write failing tests for invariants, deterministic bytes, permissions, compare-and-swap, corruption, future schema, bounds, and forbidden fields.
- [ ] Run the focused tests and confirm the intended failures.
- [ ] Implement the typed record, transitions, stable errors, and atomic private persistence.
- [ ] Re-run the focused tests.

### Task 2: Background execution seams

**Files:**
- Create `src/repoforge/ports/background_tasks.py`
- Create `src/repoforge/ports/sleeper.py`
- Create `src/repoforge/adapters/background.py`
- Modify `src/repoforge/testing/fakes.py` and exports.

- [ ] Write failing tests for keyed deduplication, key release, and deterministic sleeping.
- [ ] Implement a bounded production runner and test fakes.
- [ ] Re-run focused tests.

### Task 3: Coordinator and recovery

**Files:**
- Create `src/repoforge/application/workspace/pr_watch.py`
- Modify `src/repoforge/application/operations/recovery.py`
- Modify `src/repoforge/application/context.py`
- Modify `src/repoforge/bootstrap.py`

- [ ] Write failing tests for exact identity capture, pending progress, both completion modes, cancellation, timeout, transient outages, staleness, failure evidence references, restart recovery, and duplicate-worker prevention.
- [ ] Implement `WorkspacePrWatchCommand`, result, coordinator start, one-iteration polling, bounded loop, and active-watch recovery.
- [ ] Re-run focused tests.

### Task 4: Service and MCP contract

**Files:**
- Modify `src/repoforge/application/service.py`
- Modify `src/repoforge/interfaces/mcp/server.py`
- Modify MCP contract tests and documentation.
- Update `docs/contracts/release-contract-v1.json` only after reviewing the generated delta.

- [ ] Write failing service and actual MCP tests for schema, annotations, immediate operation reference, status, and cancellation.
- [ ] Wire the coordinator through the application, service facade, and thin MCP handler.
- [ ] Update tool inventory and user documentation.
- [ ] Re-run operation and MCP tests.

### Task 5: Verification and publication

- [ ] Run focused PR-watch, operation, MCP, and contract tests.
- [ ] Run the production verification profile and inspect all gates.
- [ ] Review the exact diff and safety boundaries.
- [ ] Run RepoForge `full` verification for the final tree.
- [ ] Commit, push without force, and create a draft PR with `Closes #10`, evidence, risks, compatibility notes, and non-goals.