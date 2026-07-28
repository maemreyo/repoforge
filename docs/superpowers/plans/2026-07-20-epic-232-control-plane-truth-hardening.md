# EPIC #232 Control-Plane Truth Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Every behavior change follows RED → GREEN → REFACTOR, each issue receives a focused diff review, verification evidence, and an atomic commit.

**Goal:** Deliver the full receipt-first control-plane program tracked by #232 while preserving the fixed 28-tool Forge V2 public roster and all exact-state, policy, idempotency, and no-blind-retry invariants.

**Architecture:** Add one cryptographically bound runtime contract identity and one shared durable effect-receipt vocabulary, then project those foundations into configuration reconciliation, runtime activation, issue-graph governance, retrieval, observability, workspace freshness, PR concurrency, operation lifecycle, and failure artifacts. GitHub-native issue/sub-issue/dependency state remains authoritative. Public capabilities are extended only as typed branches of existing tools.

**Tech Stack:** Python 3.10+, Pydantic v2 contracts, FastMCP, immutable configuration generations, JSON durable stores, Git/GitHub adapters, pytest, Ruff, Mypy, production-composition release gates.

## Global Constraints

- Keep exactly 28 public Forge V2 tools; no hidden or 29th tool.
- GitHub-native issue graph is operational authority; never revive a checked-in graph as authority.
- Every mutation uses exact HEAD/fingerprint or exact remote-version locks.
- Durable receipts and idempotency must survive response loss and restart.
- Never blind-retry an unknown external-write outcome; reconcile from authoritative remote evidence first.
- Preserve branch/path/policy/approval/final-verification invariants.
- Do not add strict gates to ordinary agent loops when typed warnings and recovery evidence are sufficient.
- Generated artifacts are regenerated from reviewed commands and verified against production composition.
- Reference models may supplement but never replace production verification.

## Recorded Pre-Implementation Evidence

- Source relaxed policy intends `max_changed_files = 150`; active runtime currently exposes 149. Restoration proposal requires operator approval `chg-8ccea248a195f0845fa1`.
- Temporary Makefile mutation in workspace `issue-reconciliation-pol-f32a602022` was restored exactly.
- Accepted and active configuration generation are both generation 6 with the same digest.
- Source and reviewed `issue_writes` proposal agree on enabled operations and budgets.
- Discovery advertises `workspace_create.issue_ids` max 100 while active runtime rejects more than 16: direct contract-skew evidence.
- Fresh workspace has identical base/latest SHA and ahead=behind=0, but freshness reports `diverged`/`refresh_required=true`: direct #239 evidence.
- A baseline test operation remains `running` with a stale timestamp while audit reports the corresponding verification succeeded: direct #242 evidence.

---

### Task 1: #233 Runtime Contract Identity and Stale-Client Rejection

**Files:**
- Create: `src/repoforge/domain/runtime_contract.py`
- Modify: `src/repoforge/contracts/registry.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `src/repoforge/interfaces/mcp/contract.py`
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `src/repoforge/contracts/v2.py`
- Modify: runtime state/host/bootstrap files only where identity persistence requires it
- Test: `tests/test_runtime_contract_identity.py`, MCP/config/runtime contract tests

**Produces:** deterministic input/output/tool-surface/build/config/process identity; discovery and response metadata; pre-dispatch `CLIENT_CONTRACT_STALE`; startup generated-contract self-check.

- [ ] Write focused failing identity-chain, tampered-schema, config-inspect, and pre-dispatch stale-client tests.
- [ ] Run exact selectors with `intent=tdd_red`; confirm failures are due to absent behavior.
- [ ] Implement the smallest complete identity domain and runtime wiring.
- [ ] Regenerate reviewed contracts; run focused GREEN tests, Ruff, Mypy, and production contract checks.
- [ ] Review `workspace_diff`, commit atomically with `feat(control-plane): bind runtime contract identity (#233)`.

### Task 2: #234 Durable Outcome Receipts

**Files:**
- Modify: `src/repoforge/domain/execution_receipt.py`
- Modify: operation/receipt stores and service boundaries
- Modify: workspace mutate/refresh/commit/push, PR and issue mutation gateways
- Test: receipt state-machine, fault-injection, restart, same-key concurrency tests

**Produces:** `accepted | applying | applied_unvalidated | applied_validated | rolled_back | failed_before_effect | failed_after_effect | unknown`, immutable result references, exact post-state recovery.

- [ ] Write RED tests at pre-effect, commit, post-effect serialization, response validation, and restart boundaries.
- [ ] Implement shared receipt transitions and same-key authoritative replay without duplicating effects.
- [ ] Run focused GREEN/property tests and review after-effect recovery payloads.
- [ ] Commit atomically with `feat(receipts): make effect boundaries durable (#234)`.

### Task 3: #241 Configuration and Ticket-Graph Reconciliation

**Files:** configuration source/document/admin, bootstrap, repository issue-graph/live projection, V2 contracts and diagnostics tests.

**Produces:** per-repository source/resolved/accepted/active/runtime projection identities, typed drift reasons, exact reconciliation actions, fail-closed graph reads.

- [ ] RED matrix for source→resolved→accepted→active→provider projection.
- [ ] Implement provenance and drift classification without inventing provider evidence.
- [ ] Verify current RepoForge fixture identifies the actual projection failure.
- [ ] Commit atomically with `feat(config): reconcile source active graph truth (#241)`.

### Task 4: #245 Receipt-Backed Runtime Activation

**Files:** runtime activation/hot reload/host/supervisor, operation/receipt stores, continuation metadata and fault tests.

**Produces:** activation operation/receipt before candidate construction; hot-reload/restart/rollback/drain outcomes; reconnect-safe continuation.

- [ ] Extend existing #245 partial implementation with RED tests for lost IPC, candidate failure, stale discovery, and reconnect continuation.
- [ ] Implement durable activation handoff and typed `RECONNECT_REQUIRED` where transport permits.
- [ ] Verify no Makefile/workspace mutation is needed for repository-only reload.
- [ ] Commit atomically with `feat(runtime): make activation reconnect-safe (#245)`.

### Task 5: #158 Desired Issue-Graph Proposal

**Files:** new issue-graph proposal domain/store/planner, existing graph readers, repo_issue application and tests.

**Produces:** canonical symbolic graph, validation findings, deterministic publication order, proposal hash and expiry; no writes.

- [ ] RED tests for byte stability, cycles, unresolved refs, duplicate markers, identity staleness and no-effect dry run.
- [ ] Implement immutable proposal and planner.
- [ ] Verify a #232-shaped graph plans deterministically.
- [ ] Commit atomically with `feat(governance): plan desired issue graphs (#158)`.

### Task 6: #160 Durable GitHub Issue-Graph Publication Saga

**Files:** issue mutation gateway, GitHub adapter, durable publication/receipt stores, operation manager and tests.

**Produces:** resumable node/edge saga, exact marker reconciliation, rate-limit pause/resume, no duplicate effects.

- [ ] RED fault matrix before/after every node and edge effect.
- [ ] Implement deterministic publication and authoritative reconciliation.
- [ ] Verify duplicate-edge 422 only becomes success after a confirming read.
- [ ] Commit atomically with `feat(governance): publish issue graphs durably (#160)`.

### Task 7: #162 Governed Workflow Inside `repo_issue`

**Files:** `contracts/v2.py`, registry/server routing, repo_issue family/task-context, instructions/goldens and workflow tests.

**Produces:** typed `manage` branch for plan/apply/status/reconcile and resumable task context, preserving 28 tools.

- [ ] RED contract and end-to-end workflow tests.
- [ ] Implement closed discriminated union and operation/receipt return paths.
- [ ] Regenerate schemas and prove roster remains exactly 28.
- [ ] Commit atomically with `feat(repo-issue): expose governed graph workflow (#162)`.

### Task 8: #246 PR Completion Intent and Post-Merge Reconciliation

**Files:** workspace PR domain/application/contracts, issue reconciliation service, GitHub adapter and regression tests.

**Produces:** typed `closes | advances | supersedes | relates`, closure keyword rendering, post-merge report and receipt-backed closes.

- [ ] RED regression for integration PRs dropping child closure intent.
- [ ] Implement explicit dispositions and per-issue acceptance evidence binding.
- [ ] Verify only fully completed issues render `Closes #...`.
- [ ] Commit atomically with `feat(pr): preserve issue completion intent (#246)`.

### Task 9: Independent and Foundation-Consumer Workstreams

Implement in dependency-ready order with one atomic commit per issue:

- [ ] #235 session-pinned deterministic repository selection and audit compaction.
- [ ] #236 adaptive bounded retrieval after confirming #193 production dependency.
- [ ] #237 generated-path-aware refresh after confirming #188 production dependency.
- [ ] #238 structured correlated runtime JSONL after #233/#234.
- [ ] #239 truthful freshness preflight and safe clean recreate after #237.
- [ ] #240 exact PR remote-version token after #233/#234.
- [ ] #242 operation record migration/invariant repair after #234.
- [ ] #243 provider-neutral failure artifacts after #234 and closed #221 dependency.

For each issue: RED selector → GREEN selector → refactor selector → `workspace_diff` → focused verification → atomic commit and implementation evidence.

### Task 10: #244 Production Fault Matrix and Release Gates

**Files:** `scripts/run_v2_release_gates.py`, `scripts/verify-production.sh`, production-composition tests, benchmark corpus, `docs/testing/TEST_RUN_RECORD.md`.

**Produces:** zero unknown effect outcomes, zero synthetic timestamps, identity consistency, resumable retrieval, generated refresh/PR/graph proof, agent-efficiency baselines.

- [ ] Add deterministic cross-layer fault matrix and installed-wheel smoke flows.
- [ ] Run generated contract checks and prove fixed 28-tool roster.
- [ ] Run full authoritative production verification on the exact final tree.
- [ ] Run rollback/restart drill and record exact build/contract/config identities.
- [ ] Commit atomically with `test(release): gate control-plane truth (#244)`.

### Task 11: Publish and Reconcile Delivery

- [ ] Refresh against latest main before final verification; resolve only with exact source evidence.
- [ ] Run full production gate and generated-contract checks fresh.
- [ ] Push the allowlisted branch without force.
- [ ] Create/update a draft PR describing completed, advanced, and blocked issues; include verification and rollback notes.
- [ ] Use `Closes #...` only for acceptance-complete tickets.
- [ ] Watch CI to terminal state, inspect typed failure evidence, fix through additional atomic commits until green.
