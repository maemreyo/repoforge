# Issue #63: GitHub Project Ticket Graph Sync Implementation Plan

> **For RepoForge:** Execute this plan with `superpowers:executing-plans`, using strict test-first development and the isolated RepoForge workspace created for issue #63.

**Goal:** Add a deterministic, dry-run-first command that reconciles the checked-in ticket graph with one GitHub Project V2 plus native sub-issue and blocked-by relationships, while preserving unmanaged GitHub state and supporting resumable partial application.

**Architecture:** Keep the checked-in manifest as the source of intent. A pure domain planner compares that intent with a bounded live GitHub snapshot and emits stable, ordered changes and conflicts. A dedicated application use case performs preflight, dry-run rendering, and explicit application through a new typed GitHub ticket-project gateway. The production adapter uses GitHub REST for sub-issues, issue dependencies, and project views, and GraphQL for Project V2 identity, fields, items, and item field values. The CLI is the only initial interface and defaults to dry-run.

**Tech stack:** Python 3.10+, dataclasses/enums, existing RepoForge command executor and durable state primitives, GitHub CLI (`gh api` / `gh api graphql`), pytest, Ruff, mypy.

---

## Task 1: Freeze the sync domain contract with failing tests

**Files:**
- Create: `tests/test_ticket_project_sync.py`
- Create: `src/repoforge/domain/ticket_sync.py`

1. Write failing tests for deterministic managed field definitions, managed view definitions, canonical change IDs, operation ordering, and no removal of unmanaged fields/views/relationships.
2. Run the narrow ticket sync test through the repository diagnostic/profile mechanism and confirm failure because the domain contract is absent.
3. Implement only the immutable domain types and canonical hashing needed to pass.
4. Re-run and keep all domain tests green.

## Task 2: Implement deterministic planning from manifest plus live snapshot

**Files:**
- Modify: `tests/test_ticket_project_sync.py`
- Create: `src/repoforge/application/tickets/project_sync.py`
- Modify: `src/repoforge/application/tickets/__init__.py`

1. Add failing tests for project creation/lookup intent, one item per manifest node, managed field values, parent/sub-issue edges, blocked-by edges, required views, ready-queue filter/order metadata, conflict reporting, and repeat-run no-op behavior.
2. Implement a pure `plan_ticket_project_sync` function with stable ordering and bounded output.
3. Ensure planner never proposes issue deletion/closure/body edits, PR mutations, or removal of unmanaged state.
4. Re-run tests and refactor only after green.

## Task 3: Define the GitHub port and fake-driven application coordinator

**Files:**
- Modify: `tests/test_ticket_project_sync.py`
- Create: `src/repoforge/ports/ticket_project.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/application/tickets/project_sync.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`

1. Add failing tests for preflight scope diagnostics, dry-run with zero mutations, explicit apply, stable per-change idempotency keys, partial failure result (`completed`, `pending`, `failed`), and resume from a refreshed snapshot.
2. Add a narrow typed `TicketProjectGateway` port; do not extend the pull-request gateway.
3. Add coordinator command/result types and inject the gateway through `ApplicationContext`/`AdapterOverrides`.
4. Apply changes one at a time in deterministic order, record bounded failure metadata, and return partial state rather than losing completed work.
5. Re-run tests.

## Task 4: Build the constrained GitHub REST/GraphQL adapter

**Files:**
- Create: `tests/test_github_ticket_project_adapter.py`
- Create: `src/repoforge/adapters/github/ticket_project.py`
- Modify: `src/repoforge/adapters/github/__init__.py`
- Modify: `src/repoforge/bootstrap.py`

1. Add failing fake-executor tests for read-only snapshot commands, auth/scope preflight, GraphQL project/field/item operations, REST sub-issue and dependency operations, REST project-view creation, bounded pagination/output, API version headers, and safe parsing.
2. Implement only constrained argv construction; no shell, arbitrary endpoint, arbitrary GraphQL, or caller-supplied mutation text.
3. Treat unsupported native capabilities as explicit conflicts/failures with a safe next action; never silently delete or overwrite.
4. Confirm mutation methods are not reachable during dry-run.
5. Re-run adapter tests.

## Task 5: Expose the dry-run-first CLI command

**Files:**
- Modify: `tests/test_cli.py` or create `tests/test_ticket_sync_cli.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/interfaces/cli/main.py`

1. Add failing parser/routing tests for:
   - `rf tickets sync --repo-id REPO --owner OWNER --project-number N`
   - default dry-run
   - explicit `--apply`
   - optional `--idempotency-key`
   - machine-readable planned/applied/conflict/partial-failure output.
2. Wire the use case through `CodingService`.
3. Mark the application action mutating only when apply is requested; dry-run remains read-only.
4. Re-run CLI tests.

## Task 6: Document operations and repair the checked-in ticket graph status

**Files:**
- Create: `docs/operations/TICKET_PROJECT_SYNC.md`
- Modify: `README.md`
- Modify: `docs/roadmaps/REPOFORGE_TICKET_GRAPH.json`
- Modify only if required by frozen contracts: `docs/contracts/*`

1. Document required scopes (`read:project` for dry-run, `project` plus issue write/project permissions for apply), dry-run/apply examples, managed fields/views, conflict policy, recovery, and idempotency behavior.
2. Update the manifest only for issue states that are confirmed live and necessary for deterministic selection; do not change unrelated intent.
3. Do not add an MCP write tool in this issue.

## Task 7: Verification, review, and publication

1. Review the exact diff and change-budget metrics.
2. Run focused tests during iteration and the full `make check` gate.
3. If the pre-existing Ruff formatting failure remains, format exactly the files reported by the authoritative gate and document that baseline repair separately in the PR body.
4. Run `scripts/verify-production.sh --allow-dirty` (via the configured full verification profile) and confirm all tests/types/build/clean-wheel checks pass.
5. Commit with a scoped conventional commit, push without force, create a draft PR linked to #63, and inspect CI.

## Explicit non-goals

- No issue creation, deletion, closure, body/spec-comment rewriting, or PR mutation.
- No project becoming the source of truth.
- No generic GitHub GraphQL/REST execution surface.
- No silent removal of relationships or unmanaged fields/views.
- No scheduled/background synchronization.
- No MCP tool addition in this ticket.
