# Control Plane Foundations and Runtime Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore RepoForge's operator and verification contracts, make GitHub-native governance and runtime state self-diagnosing, then complete the durable task, approval, verification-efficiency, configuration-drift, mutation-retry, and code-intelligence foundations in one isolated worktree.

**Architecture:** Preserve the existing dependency direction: interfaces call application use cases, domain contracts remain provider-neutral, and adapters own GitHub, subprocess, filesystem, runtime, and persistence details. Each capability is independently deployable and committed separately even though all work shares one worktree. Exact-tree final verification remains authoritative; caches, ad-hoc execution, diagnostics, and deduplication are iteration evidence only.

**Tech Stack:** Python 3.10+, dataclasses/enums, existing `StateRepository` and JSON durable-state adapters, MCP/FastMCP, GitHub CLI adapters, POSIX shell, Make, pytest, Ruff, strict Mypy.

## Global Constraints

- Work only in workspace `control-plane-foundation-f19b20ede8`.
- Keep every public input bounded and every state mutation optimistic or exact-state-bound.
- Do not expose arbitrary shell, model-provided repository paths, unreviewed commands, merge, force-push, release administration, or workflow editing.
- Audit only safe identifiers, hashes, counts, classifications, and bounded metadata; never source bodies, patches, secrets, raw initialize payloads, or command-output bodies.
- Use failing tests before production code for every behavior change.
- Use narrow diagnostics or the quick profile while iterating; run the full profile once on the final exact tree.
- Keep one independently reviewable Conventional Commit per task.
- Update release contracts and golden prompts only when the public MCP surface changes intentionally.

---

### Task 1: Restore deterministic developer, runtime, and release commands

**Files:**
- Modify: `Makefile`
- Modify: `scripts/release.sh`
- Modify: `tests/test_docs_command_drift.py`
- Create: `tests/test_release_script_contract.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces stable Make targets `help`, `setup`, `lint`, `typecheck`, `test`, `build`, `check`, `production-check`, `tickets`, `inspector`, `install-hooks`, `start`, `watch`, `status`, `stop`, and `release`.
- Produces one portable release preflight that refuses omitted bump type, dirty/untracked trees, existing tags, and ambiguous build artifacts.

- [ ] Add tests that parse `config.repoforge.toml` profile commands and assert every referenced Make target exists.
- [ ] Add tests proving the default Make goal is read-only `help`, `check` invokes `scripts/verify-production.sh --allow-dirty`, and `production-check` invokes the clean gate.
- [ ] Add shell-contract tests proving release requires `patch|minor|major`, has no BSD-only `sed -i ''`, checks untracked files, cleans/isolates `dist`, and emits SHA256 checksums.
- [ ] Run the focused tests and observe failures on the current Makefile/release script.
- [ ] Restore the missing verification/tooling targets, make `help` the default, run background/watch startup in one shell, remove broad `pkill`, and add `runtime logs --path-only` consumption rather than parsing pretty JSON.
- [ ] Refactor release version editing through a small Python helper, verify before tag/push, build exactly once into a clean directory, generate checksums, create the GitHub release, then push the reviewed commit/tag without force.
- [ ] Run the focused tests and quick static checks.
- [ ] Commit as `fix(tooling): restore deterministic verification and runtime commands`.

### Task 2: Preserve GitHub ticket-graph configuration and fail closed on incomplete discovery

**Files:**
- Modify: `src/repoforge/application/configuration/source.py`
- Modify: `src/repoforge/application/configuration/document.py`
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `src/repoforge/application/repository/issue_graph.py`
- Modify: `src/repoforge/application/repository/issue_next.py`
- Modify: `src/repoforge/adapters/github/ticket_graph.py`
- Modify: `tests/test_config_admin.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_repo_issue_graph_tools.py`
- Modify: `tests/test_github_ticket_graph_adapter.py`
- Modify: `config.repoforge.toml`
- Modify: `docs/development/TICKET_GOVERNANCE.md`

**Interfaces:**
- Extends `SourceRepository` with a typed optional `ticket_graph` table that round-trips source → proposal → resolved generation.
- `repo_issue_graph` and `repo_issue_next` return explicit diagnostics for missing root, root-unavailable, relationship-read failure, incomplete traversal, and truncation; selection fails closed when evidence is incomplete.

- [ ] Add source-config round-trip tests for `root_issue`, repository slug, optional Project overlay fields, and removal of the table.
- [ ] Add a config-admin test showing `config_inspect` reports source/accepted ticket-graph identities and drift.
- [ ] Add graph tests showing missing configuration is an explicit invalid result rather than an empty successful graph.
- [ ] Add adapter tests preserving endpoint failure details per issue/relationship while bounding output.
- [ ] Run focused tests and observe the current source-config and empty-success failures.
- [ ] Implement typed source metadata and resolved rendering without duplicating policy-patch authority.
- [ ] Return graph coverage (`configured_root`, `observed_root`, `observed_nodes`, `unavailable`, `truncated`, `evidence_complete`) plus deterministic remediation.
- [ ] Make `repo_issue_next` return no selectable tickets whenever coverage is incomplete.
- [ ] Run config, adapter, graph, readiness, task-context, and release-contract tests.
- [ ] Commit as `fix(tickets): preserve graph authority and fail closed on incomplete discovery`.

### Task 3: Add a typed runtime-health snapshot and rediscovery guidance

**Files:**
- Create: `src/repoforge/domain/runtime_health.py`
- Create: `src/repoforge/application/runtime/health.py`
- Create: `src/repoforge/ports/runtime_identity.py`
- Create: `src/repoforge/adapters/runtime/identity.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `tests/test_cli_runtime_commands.py`
- Modify: `tests/test_client_capabilities.py`
- Create: `tests/test_runtime_health.py`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/contracts/release-contract-v1.json`

**Interfaces:**
- Produces `RuntimeHealthSnapshot` with running package/executable identity, accepted/active generation, server/current tool-surface hashes, process identity, normalized connection capabilities, and flags `package_version_skew`, `generation_activation_required`, `restart_required`, `hot_reload_available`, and `client_rediscovery_recommended`.
- Exposes one side-effect-free health projection through runtime status/doctor and an MCP-safe read surface.

- [ ] Add unit tests for healthy, package skew, generation skew, surface change, unknown origin, and combined conditions.
- [ ] Add connection-scoped capability tests proving raw initialize payloads are neither persisted nor returned.
- [ ] Run focused tests and observe missing snapshot behavior.
- [ ] Implement domain classification and platform probing with explicit `UNKNOWN` values.
- [ ] Add stable one-action remediation ordering: activate/reload, reinstall/restart, reconnect/rediscover.
- [ ] Update CLI/MCP contracts and golden prompts.
- [ ] Commit as `feat(runtime): expose version generation and client surface health`.

### Task 4: Add the durable TaskCapsule foundation

**Files:**
- Create: `src/repoforge/domain/task_capsule.py`
- Create: `src/repoforge/ports/task_store.py`
- Create: `src/repoforge/adapters/persistence/json_task_store.py`
- Create: `src/repoforge/application/tasks/__init__.py`
- Create: `src/repoforge/application/tasks/service.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Create: `tests/test_task_capsule.py`
- Create: `tests/test_task_store.py`

**Interfaces:**
- Produces typed `TaskCapsule`, bounded criteria/decision/question/action/binding value objects, explicit state transitions, revisions, and compact `resume_projection()`.
- Produces `TaskStore.create/read/update/list` over existing durable-state primitives; no public MCP tools in this slice.

- [ ] Add table tests for all valid/invalid transitions and field bounds.
- [ ] Add persistence tests for restart, stale revision, permissions, corruption, future schema, deterministic encoding, and audit-safe summaries.
- [ ] Run focused tests and observe missing contracts.
- [ ] Implement the minimal domain and store by composing `StateEnvelope`/`JsonStateRepository` rather than introducing another persistence framework.
- [ ] Add bootstrap factories and import-boundary coverage.
- [ ] Commit as `feat(tasks): add durable task capsule domain and store`.

### Task 5: Add one approval domain and migrate pending config proposals onto it

**Files:**
- Create: `src/repoforge/domain/approval.py`
- Create: `src/repoforge/ports/approval_store.py`
- Create: `src/repoforge/adapters/persistence/json_approval_store.py`
- Create: `src/repoforge/application/approvals/service.py`
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_config_admin.py`
- Create: `tests/test_approval_domain.py`
- Create: `tests/test_approval_store.py`
- Create: `tests/test_approval_migration.py`

**Interfaces:**
- Produces typed `ApprovalRequest`, `ApprovalSubject`, `ApprovalBinding`, decisions, revisions, expiry, and safe summaries.
- Replaces `PendingPolicyChangeStore` as an independent authority with an adapter/migration into the shared approval store.

- [ ] Add domain and store tests before implementation.
- [ ] Add migration tests for existing `pending-policy-changes/*.json` records and idempotent repeated startup.
- [ ] Implement one queue/store and update config approve/reject flows to consume it.
- [ ] Preserve the current out-of-band CLI approval requirement for capability expansion.
- [ ] Commit as `feat(approval): add shared durable approval requests and config migration`.

### Task 6: Compile profiles into typed verification steps

**Files:**
- Modify: `src/repoforge/config.py`
- Modify: `src/repoforge/domain/verification.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/application/workspace/assessment.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_background_run_profile.py`
- Create: `tests/test_structured_verification_steps.py`
- Modify: `docs/development/TOOL_REFERENCE.md`

**Interfaces:**
- Adds `VerificationStep(id, kind, command)` and compiles legacy `commands` deterministically into `unknown` steps.
- Profile failures return `completed_steps`, `failed_step`, `failure_domain`, `not_run_steps`, and `business_tests_ran`; hygiene may opt into `strict_clean|no_regression`, but no-regression evidence never satisfies commit by itself.

- [ ] Add config migration and runner tests for each failure position, timeout, cancellation, background execution, and legacy profiles.
- [ ] Implement typed step execution while preserving command executor, receipts, and exact-tree commit behavior.
- [ ] Add no-regression hygiene receipt binding to base/workspace/checker/config/environment identities.
- [ ] Commit as `feat(verification): add structured profile steps and hygiene policy`.

### Task 7: Surface proposal-ready verification-profile drift

**Files:**
- Create: `src/repoforge/domain/profile_drift.py`
- Create: `src/repoforge/application/profile_drift.py`
- Modify: `src/repoforge/application/repository/context.py`
- Modify: `src/repoforge/application/repository/task_context.py`
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `tests/test_verification_detection.py`
- Modify: `tests/test_repo_task_context.py`
- Create: `tests/test_profile_drift.py`

**Interfaces:**
- Produces snapshot/generation-bound `ProfileDriftAssessment` with semantic command identity, provenance, network/mutability classification, capability delta, equivalent pending proposal, and a typed `repo_policy_apply(dry_run=true)` payload.

- [ ] Add tests for empty, detected missing, semantically equivalent different-name, networked setup, stale snapshot/generation, and existing pending proposal.
- [ ] Implement one assessment consumed by both repo context tools.
- [ ] Reuse ecosystem packs from diagnostics and config-admin capability classification.
- [ ] Commit as `feat(config): surface proposal-ready verification profile drift`.

### Task 8: Add idempotent local mutation retries

**Files:**
- Modify: `src/repoforge/application/idempotency.py`
- Modify: `src/repoforge/application/workspace/file_write.py`
- Modify: `src/repoforge/application/workspace/edit.py`
- Modify: `src/repoforge/application/workspace/apply_patch.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `tests/test_workspace_edit.py`
- Modify: `tests/test_patch_normalization.py`
- Create: `tests/test_mutation_idempotency.py`
- Modify: `docs/contracts/release-contract-v1.json`

**Interfaces:**
- Adds optional bounded `idempotency_key` to `workspace_write_file`, `workspace_edit`, and `workspace_apply_patch`.
- Canonical requests bind operation/workspace/repository, normalized payload digest, expected HEAD/fingerprint/file hashes, and result envelope; replay returns the original success without reapplying.

- [ ] Add replay, conflict, concurrency, expiry, corruption, and lost-response fault-injection tests.
- [ ] Implement reservation/finalization under existing workspace locking and current optimistic state checks.
- [ ] Improve ambiguous-write recovery envelopes and audit replay/original outcome.
- [ ] Commit as `feat(workspace): add idempotent mutation retries`.

### Task 9: Deduplicate deterministic unchanged verification failures

**Files:**
- Modify: `src/repoforge/domain/retry_guidance.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/application/workspace/run_diagnostic.py`
- Modify: `tests/test_retry_guidance.py`
- Create: `tests/test_verification_deduplication.py`

**Interfaces:**
- Reuses a prior bounded result when workspace fingerprint, target identity, command-source identity, config generation, and environment identity match and the prior failure is deterministic/non-retryable.
- Adds explicit `force_rerun=false`; dedupe never applies to timeout, network, suspected flaky, cancellation, corrupt, or incomplete results.

- [ ] Add profile/diagnostic reuse, invalidation, force-rerun, background-reference, and audit tests.
- [ ] Implement evidence-only reuse without creating a verification receipt or changing commit eligibility.
- [ ] Commit as `feat(verification): reuse deterministic failures on unchanged workspaces`.

### Task 10: Add the provider-neutral code-intelligence baseline

**Files:**
- Create: `src/repoforge/domain/code_intelligence.py`
- Create: `src/repoforge/ports/code_intelligence.py`
- Create: `src/repoforge/adapters/code_intelligence/syntax.py`
- Create: `src/repoforge/application/code_intelligence.py`
- Modify: `src/repoforge/domain/evidence.py`
- Modify: `src/repoforge/application/workspace/assessment.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/bootstrap.py`
- Create: `tests/test_code_intelligence.py`
- Create: `tests/test_code_intelligence_integration.py`
- Modify: `tests/test_workspace_assessment.py`

**Interfaces:**
- Produces provider-neutral snapshot-bound symbol/import/reference/impact/affected-test evidence with explicit coverage, confidence, limitations, and unavailable states.
- First adapter uses bounded syntax/import heuristics and maps affected tests directly to enrolled diagnostic selectors; no daemon, raw query language, project path, or authorization behavior.

- [ ] Add multilingual fixture tests, malformed/unsupported/denied/generated path cases, dirty-workspace invalidation, and provider failure fallback.
- [ ] Implement affected-test candidates first, then import/dependency and symbol facts.
- [ ] Push candidates into assessment and failed-profile next actions rather than exposing a passive tool only.
- [ ] Commit as `feat(intelligence): add provider-neutral affected-test baseline`.

### Task 11: Reconcile docs, contracts, and final verification

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md`
- Modify: `docs/testing/PLUGIN_TEST_CASES.md`
- Modify: `docs/testing/TEST_RUN_RECORD.md`
- Modify: `docs/contracts/release-contract-v1.json`

- [ ] Remove checked-in-graph/project-apply claims that conflict with GitHub-native authority.
- [ ] Record the issue contracts already satisfied by merged code and identify superseded graph reconciliation work.
- [ ] Run focused suites from Tasks 1–10 together.
- [ ] Review `workspace_diff` for scope, safety, generated artifacts, and accidental formatting-only changes.
- [ ] Run the full/default profile once on the exact final tree.
- [ ] Commit any intentional generated docs/contract update separately.
- [ ] Push without force and create one draft PR with a task-by-task verification ledger and explicit remaining live GitHub metadata actions.
