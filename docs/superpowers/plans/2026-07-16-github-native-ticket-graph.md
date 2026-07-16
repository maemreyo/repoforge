# GitHub-Native Ticket Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GitHub issues, Project V2 fields, sub-issues, and blocked-by relationships the sole operational ticket graph so ticket changes never require editing a checked-in JSON manifest.

**Architecture:** Add a typed per-repository GitHub graph source rooted at one native parent issue. A read-only GitHub adapter traverses bounded native sub-issues and dependencies into one normalized `TicketGraphSnapshot`; an optional Project V2 target overlays Project-specific fields when configured. Application graph and readiness readers consume the snapshot through a port and the existing private TTL cache. Pure graph validation/readiness stays unchanged, while the production manifest and graph-to-GitHub desired-state path are retired.

**Tech Stack:** Python dataclasses, GitHub CLI/API, Project V2 GraphQL, GitHub REST sub-issue/dependency endpoints, existing `GitHubReadCache`, pytest with fake command executor.

## Global Constraints

- GitHub is the only operational source of truth.
- All remote access is read-only, paginated, bounded, timed out, and explicitly reports incomplete evidence.
- Cache is private evidence only; `fresh=true` bypasses it.
- No GitHub ticket mutation is introduced.
- Pure JSON graph fixtures remain available for domain tests only.

---

### Task 1: Add typed graph-source configuration

**Files:**
- Modify: `src/repoforge/config.py`
- Modify: `config.example.toml`
- Modify: `config.repoforge.toml`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `GitHubTicketGraphConfig(root_issue: int, project_owner: str | None = None, project_number: int | None = None, project_owner_type: str = "organization", status_field: str = "Status", priority_field: str = "Priority", initiative_field: str = "Initiative", type_field: str = "Type")`.
- Produces: `RepositoryConfig.ticket_graph: GitHubTicketGraphConfig | None`.

- [ ] **Step 1: Write failing config parsing tests**

Cover a valid `[repositories.ticket_graph]` table with only `root_issue`, an optional complete Project overlay, and rejection of non-positive roots, half-configured Project targets, invalid owner type, blank field names, and unknown keys.

- [ ] **Step 2: Run the config test module**

Expected: FAIL because `GitHubTicketGraphConfig` does not exist.

- [ ] **Step 3: Implement the typed configuration model and parser**

Use exact bounds and keep configuration secrets out of this model. `root_issue` is required and is the traversal/default graph root. Project owner and number must either both be absent or both be present; Project metadata is an overlay, never the membership source.

- [ ] **Step 4: Add reviewed RepoForge example configuration**

Configure RepoForge with `root_issue = 3`. Add the existing Project target only if its reviewed owner/number are already known; never guess them and never add tokens or webhook secrets to TOML.

- [ ] **Step 5: Re-run config tests**

Expected: PASS.

### Task 2: Define the live graph snapshot port

**Files:**
- Modify: `src/repoforge/domain/tickets.py`
- Create: `src/repoforge/ports/ticket_graph.py`
- Modify: `src/repoforge/ports/__init__.py`
- Test: `tests/test_github_ticket_graph_adapter.py`

**Interfaces:**
- Produces: `TicketGraphSnapshot(graph: TicketGraph, observed_at: str, evidence_complete: bool, unavailable: tuple[int, ...], truncated: bool)`.
- Produces protocol:

```python
class TicketGraphGateway(Protocol):
    def read(
        self,
        cwd: Path,
        source: GitHubTicketGraphConfig,
        *,
        max_items: int,
    ) -> TicketGraphSnapshot: ...
```

- [ ] **Step 1: Write failing normalization tests**

Fixture cases cover Project fields, closed-to-Done mapping, native parent/sub-issue edges, blocked-by edges, reciprocal children/blocks derivation, deterministic ordering, absent optional fields, and non-issue project items.

- [ ] **Step 2: Run the new adapter test module**

Expected: FAIL because the port and snapshot do not exist.

- [ ] **Step 3: Add the typed snapshot and gateway port**

Keep the domain independent of `gh`, filesystem persistence, and MCP.

- [ ] **Step 4: Re-run the type-focused tests**

Expected: port imports and snapshot construction PASS; adapter tests still FAIL.

### Task 3: Build the bounded GitHub graph adapter

**Files:**
- Replace responsibility in: `src/repoforge/adapters/github/ticket_graph.py`
- Modify: `src/repoforge/adapters/github/__init__.py`
- Test: `tests/test_github_ticket_graph_adapter.py`

**Interfaces:**
- Consumes: configured root issue, optional Project overlay target, and repository path.
- Produces: `CommandGitHubTicketGraphGateway.read(...) -> TicketGraphSnapshot`.

- [ ] **Step 1: Implement bounded root traversal**

Starting at `root_issue`, breadth-first traverse native sub-issues up to 200 unique issues with explicit pagination and cycle detection. Read issue number/title/state/body/labels and reject malformed top-level responses; mark pagination or node-limit truncation explicitly.

- [ ] **Step 2: Implement native dependency reads and optional Project overlay**

For traversed issues only, read `dependencies/blocked_by` using bounded REST endpoints. Normalize relationships to included issue numbers and derive reciprocal `children`, `blocks`, and `blockers` deterministically. When a Project target is configured, reuse the safe adapter pattern from `ticket_project.py` to overlay configured field values for those same issues; Project items never add graph membership.

- [ ] **Step 3: Map GitHub metadata to domain enums**

Map closed issues to `Done`; validate open status, priority, type, initiative, wave, and sequence field values. Missing or invalid required metadata yields diagnostics/unavailable evidence rather than guessed values.

- [ ] **Step 4: Add failure-path tests**

Cover timeout, auth failure, rate limit, malformed JSON, pagination overflow, duplicate issue numbers, PR project items, cross-repository items, and partial relationship evidence.

- [ ] **Step 5: Run adapter tests**

Expected: PASS with fake `gh`; assert no write verbs are invoked.

### Task 4: Reuse the private TTL cache for graph snapshots

**Files:**
- Modify: `src/repoforge/ports/github_read_cache.py`
- Modify: `src/repoforge/adapters/persistence/json_github_read_cache.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `tests/test_github_read_cache.py`

**Interfaces:**
- Adds cache invalidation and repository-scoped graph key support:

```python
def invalidate(self, repo_id: str, repo_path: Path, *, kind: str | None = None) -> int: ...
```

- Adds `ApplicationContext.cached_github_graph_read(..., fresh: bool) -> tuple[dict[str, Any], bool]`.

- [ ] **Step 1: Write failing graph cache tests**

Cover miss/put/hit, TTL expiry, `fresh=true`, repository path rebinding, incomplete snapshots, corruption fallback, and scoped invalidation.

- [ ] **Step 2: Add repository-scoped cache entries and invalidation**

Use a stable sentinel number for graph entries while preserving existing issue/PR keys and schema-version compatibility.

- [ ] **Step 3: Add application read-through orchestration**

Cache only already-normalized bounded snapshot payloads. A cache failure remains a live-read miss.

- [ ] **Step 4: Run cache tests**

Expected: PASS without weakening existing cache behavior.

### Task 5: Switch MCP graph and next-ticket tools to GitHub

**Files:**
- Modify: `src/repoforge/application/repository/issue_graph.py`
- Modify: `src/repoforge/application/repository/issue_next.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_repo_issue_graph_tools.py`
- Modify: `tests/test_mcp_contract.py`

**Interfaces:**
- Adds `fresh: bool = False` to `RepositoryIssueGraphCommand`, `RepositoryIssueNextCommand`, service methods, and MCP tools.
- Graph result adds `source="github"`, `cache_hit`, `observed_at`, `evidence_complete`, and `unavailable`; removes operational dependence on `manifest_found`.

- [ ] **Step 1: Rewrite graph tool tests against a fake gateway**

Assert filtering, default root, truncation, cache hit, forced refresh, and explicit incomplete evidence without creating a manifest file.

- [ ] **Step 2: Rewrite next-ticket tests against one snapshot**

Eliminate the current N per-issue live read loop. Feed graph nodes and live issue bodies from the same bounded snapshot so readiness uses one consistent observation.

- [ ] **Step 3: Wire the gateway through bootstrap and application services**

Keep MCP handlers thin and annotate both tools as external reads.

- [ ] **Step 4: Add protocol schema/invocation tests**

Assert `fresh` defaults to false and result evidence fields are present.

- [ ] **Step 5: Run graph, readiness, cache, and MCP contract tests**

Expected: PASS.

### Task 6: Retire the committed operational manifest and reverse sync ownership

**Files:**
- Delete: `docs/roadmaps/REPOFORGE_TICKET_GRAPH.json`
- Delete: `src/repoforge/application/tickets/repo_manifest.py`
- Modify or retire: `scripts/validate_ticket_graph.py`
- Modify: `scripts/verify-production.sh`
- Modify: `Makefile`
- Modify: `src/repoforge/application/tickets/project_sync.py`
- Modify: `src/repoforge/ports/ticket_project.py`
- Modify: `tests/test_ticket_project_sync.py`
- Preserve: `tests/fixtures/tickets/*.json`

**Interfaces:**
- Production validation targets live graph configuration and pure domain fixtures, not a repository manifest.
- Ticket project sync becomes a bounded read-only consistency report for GitHub-native data; it no longer projects committed desired state onto GitHub.

- [ ] **Step 1: Add failing tests proving no production manifest is required**

Assert `make check` scripts contain no manifest validator and graph tools work in a repository with no `docs/roadmaps` graph.

- [ ] **Step 2: Remove manifest loading and production-gate invocation**

Retain `load_ticket_graph(path)` only for explicit CLI/test fixture validation if useful; remove the fixed default path and `make tickets` maintenance obligation.

- [ ] **Step 3: Make project sync read-only or explicitly deprecate it**

Remove graph-driven `ADD_SUB_ISSUE` and `ADD_BLOCKED_BY` planning from the normal path. Preserve unmanaged Project state and return clear migration guidance for any deprecated apply mode.

- [ ] **Step 4: Run production-script and ticket-sync tests**

Expected: PASS with no checked-in operational graph.

### Task 7: Documentation, contract, and final verification

**Files:**
- Modify: `README.md`
- Rewrite: `docs/development/TICKET_GOVERNANCE.md`
- Modify: `docs/operations/TICKET_PROJECT_SYNC.md`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/contracts/release-contract-v1.json`

**Interfaces:**
- Produces: migration and operator instructions for Project configuration, scopes, cache, and `fresh`.

- [ ] **Step 1: Document GitHub-native ownership and migration**

State that issue lifecycle and relationships are edited only in GitHub; no generated or hand-maintained graph file is committed.

- [ ] **Step 2: Refresh the reviewed MCP contract**

Review only the expected `fresh` parameters, descriptions, external-read annotations, and tool-surface hash.

- [ ] **Step 3: Review `workspace_diff` and run the `full` profile**

Expected: all source, contract, test, build, and clean-wheel gates PASS.

- [ ] **Step 4: Commit the verified tree**

Commit message:

```text
feat(tickets): derive ticket graph from GitHub
```
