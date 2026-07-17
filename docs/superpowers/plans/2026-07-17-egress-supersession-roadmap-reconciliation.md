# Egress, Supersession, and Roadmap Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the completed roadmap foundations and add one central secret-safe egress policy plus explicit requirement-evolution and partial-completion governance.

**Architecture:** One isolated worktree and one PR contain independently reviewable commits. Secret handling is centralized in a pure domain engine and one recursive application serialization boundary while legacy redaction helpers delegate to the same engine. Requirement evolution extends typed live ticket metadata, graph validation, readiness derivation, rendering, and GitHub read adapters without adding GitHub write authority.

**Tech Stack:** Python 3.10+, frozen dataclasses, enums, regular expressions, SHA-256 identities, existing RepoForge GitHub/readiness adapters, pytest, Ruff, strict Mypy.

## Global Constraints

- Preserve the frozen MCP roster and runtime protocol unless a reviewed additive contract change is explicitly required.
- Never return or persist detected secret values.
- Audit, metrics, diagnostics, recordings, and errors contain finding IDs, categories, ranges, counts, digests, and policy reasons only.
- GitHub issue access remains read-only; no Project V2 apply or hidden issue mutation.
- Requirement history is bounded, deterministic, append-oriented, and safe-metadata-only.
- One worktree is intentional; each independently rejectable concern receives its own commit.

---

### Task 1: Roadmap reconciliation evidence

**Files:**
- Create: `docs/roadmaps/2026-07-17-foundation-reconciliation.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: merged PR #201 and existing foundations on `main`.
- Produces: exact closure rationale for #70, #102, #18, #81, and #195 plus post-cutover follow-up boundaries.

- [ ] Write the reconciliation document with delivered evidence, remaining non-goals, and explicit close/follow-up decisions.
- [ ] Add one changelog entry.
- [ ] Run `quick` verification.
- [ ] Commit `docs(roadmap): reconcile completed foundations`.

### Task 2: Typed secret-safe egress engine

**Files:**
- Create: `src/repoforge/domain/egress.py`
- Create: `tests/test_egress_policy.py`
- Modify: `src/repoforge/domain/redaction.py`
- Modify: `src/repoforge/domain/ci_evidence.py`

**Interfaces:**
- Produces: `EgressDecision`, `EgressContentClass`, `EgressDestination`, `EgressRequest`, `EgressFinding`, `EgressResult`, `evaluate_egress`, and `sanitize_egress_data`.
- Legacy `redact_text`, `redact_data`, and CI sanitization delegate to the engine.

- [ ] Write failing tests for provider tokens, assignments, private keys, credential URLs, explicit secrets, sensitive config shapes, binary/invalid encoding, denied sources, bounds, Unicode, overlap merging, false-positive allowlists, and absence of secret values in findings.
- [ ] Run the focused test and verify RED.
- [ ] Implement bounded typed detection, deterministic finding IDs, merged ranges, allowlists, redaction/withhold/reject decisions, and recursive structured sanitization.
- [ ] Run focused tests and existing security/CI-evidence tests to GREEN.
- [ ] Run formatter and `quick` verification.
- [ ] Commit `feat(security): add secret-safe egress policy`.

### Task 3: Enforce egress at public serialization boundaries

**Files:**
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/application/diagnostics/bundle.py`
- Modify: `tests/test_service_tools.py`
- Modify: `tests/test_security.py`
- Create: `docs/development/SECRET_SAFE_EGRESS.md`

**Interfaces:**
- Consumes: `sanitize_egress_data` from Task 2.
- Produces: every `CodingService` payload is recursively sanitized before MCP/CLI serialization; existing diagnostic helpers remain compatible.

- [ ] Write failing facade tests proving secrets in nested source/findings/log payloads are sanitized while hashes, UUIDs, selectors, public fixtures, and ordinary source remain unchanged.
- [ ] Verify RED.
- [ ] Apply the central boundary in `_result` and route structured diagnostics through the shared API.
- [ ] Verify focused and integration tests GREEN.
- [ ] Document decision semantics, safe metadata, allowlists, and integration requirements.
- [ ] Run formatter and `quick` verification.
- [ ] Commit `feat(security): enforce egress on public payloads`.

### Task 4: Typed requirement evolution and partial completion

**Files:**
- Modify: `src/repoforge/domain/tickets.py`
- Modify: `src/repoforge/application/tickets/live.py`
- Modify: `src/repoforge/application/tickets/graph.py`
- Modify: `src/repoforge/application/tickets/readiness.py`
- Modify: `src/repoforge/application/repository/issue_graph.py`
- Modify: `src/repoforge/application/repository/issue_next.py`
- Modify: `src/repoforge/application/repository/issue_spec.py`
- Modify: `src/repoforge/adapters/github/ticket_graph.py`
- Modify: `tests/test_ticket_readiness.py`
- Modify: `tests/test_ticket_graph.py`
- Modify: `tests/test_repo_issue_graph_tools.py`

**Interfaces:**
- Produces: `RequirementRelationType`, `RequirementRelation`, `PartialCompletion`, and bounded evolution metadata on `TicketDeliveryMetadata`.
- Recognized relations: `supersedes`, `superseded_by`, `split_into`, `merged_into`, and `invalidates`.

- [ ] Write failing domain/parser tests for every relation, duplicate/self edges, bounded fields, partial-completion records, comments, and deterministic normalization.
- [ ] Verify RED.
- [ ] Implement typed contracts and live issue/comment parsing.
- [ ] Write failing graph/readiness tests for supersession cycles, invalidated blocker assumptions, selection exclusion, and closed partial work not becoming Done.
- [ ] Verify RED.
- [ ] Implement graph validation and readiness derivation.
- [ ] Extend cached GitHub snapshots with bounded comment bodies and render evolution metadata in graph/next/spec results.
- [ ] Run focused tests GREEN, formatter, and `quick` verification.
- [ ] Commit `feat(governance): add requirement evolution lifecycle`.

### Task 5: Templates, governance docs, and closure semantics

**Files:**
- Modify: `.github/ISSUE_TEMPLATE/implementation-ticket.yml`
- Modify: `.github/ISSUE_TEMPLATE/initiative.yml`
- Modify: `tests/test_ticket_graph.py`
- Create: `docs/development/REQUIREMENT_EVOLUTION.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: typed relation and partial-completion vocabulary from Task 4.
- Produces: explicit authoring fields for split, merge, rejection, supersession, invalidation, and partial completion.

- [ ] Add required evolution and partial-completion fields to both issue forms.
- [ ] Extend issue-form contract tests.
- [ ] Document append-only examples and PR closure rules.
- [ ] Run focused tests, formatter, and `quick` verification.
- [ ] Commit `docs(governance): define supersession and partial completion`.

### Task 6: Final verification and publication

**Files:**
- Review all changed files.

**Interfaces:**
- Produces: one draft PR closing #50, #69, #70, #102, #18, #81, and #195 with commit-level scope boundaries.

- [ ] Run focused tests for egress, security, CI evidence, tickets, graph tools, architecture, MCP contracts, and service tools.
- [ ] Run release-contract diagnostic.
- [ ] Confirm base status and refresh if required.
- [ ] Run authoritative `full` profile on the exact final tree.
- [ ] Push and create one draft PR with exact SHA verification evidence.
- [ ] Watch all GitHub checks to terminal state; fix deterministic failures in separate commits.
