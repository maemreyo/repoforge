# GitHub-Native Ticket Graph and Complete Tool Schemas

## Goal

Remove the checked-in ticket graph as an operational source of truth and make RepoForge's MCP input schemas complete enough that agents can construct valid calls without trial-and-error retries.

## Decisions

1. GitHub is the only operational source of truth for ticket lifecycle, hierarchy, dependencies, status, priority, and initiative membership.
2. `repo_issue_graph` and `repo_issue_next` assemble a bounded graph from live GitHub issue, Project V2, sub-issue, and issue-dependency data.
3. A short-lived private local cache may reduce GitHub calls. Cache entries are evidence only, can be bypassed with `fresh=true`, and must never be required for correctness.
4. Webhooks invalidate relevant cache entries. Polling/live reads remain the correctness fallback when webhooks are unavailable or delayed.
5. `docs/roadmaps/REPOFORGE_TICKET_GRAPH.json` is removed from production verification and normal maintenance. JSON graphs remain only as deterministic test fixtures or explicit exports.
6. Ticket-project synchronization no longer treats a committed graph as desired state. GitHub-native relationships are read directly; RepoForge may still validate and report bounded inconsistencies without creating a second authoritative copy.

## GitHub Integration

The live adapter reads:

- issue state, title, labels, and issue fields;
- Project V2 item fields used for status, priority, and initiative metadata;
- native parent/sub-issue relationships;
- native blocked-by relationships.

The optional webhook receiver recognizes `issues`, `issue_dependencies`, and `projects_v2_item` deliveries, verifies the configured secret, deduplicates delivery IDs, and invalidates only the affected repository cache. It does not mutate tickets or repository source.

If webhook configuration is absent, RepoForge continues to work through bounded live reads and TTL expiry. A missed, duplicate, reordered, or delayed delivery cannot make cached data authoritative.

## Tool Contract Changes

`repo_issue_graph` and `repo_issue_next` gain `fresh: bool = false`. Their outputs identify cache use, snapshot time, truncation, and unavailable evidence.

`workspace_edit.files` must expose a concrete array-item schema containing `relative_path`, `expected_sha256`, and bounded ordered `edits`; no client-facing `Array<unknown>` is acceptable.

Numeric constraints enforced at runtime must also appear in MCP JSON Schema. In particular, `repo_search.context_lines` and `workspace_search.context_lines` expose inclusive bounds `0..5`. Existing runtime validation remains defense in depth.

Schema contract tests must assert nested array item shapes and numeric minimum/maximum values through a real in-memory MCP client session, not only Python annotations.

## Application Boundaries

- Domain graph validation and deterministic ready-ticket selection remain pure and reusable.
- The GitHub adapter owns pagination and normalization of remote issue relationships and Project fields.
- Application services orchestrate bounded reads, cache behavior, and selection without parsing command output directly.
- MCP handlers remain thin typed adapters.
- Webhook handling is an optional ingress adapter that only validates deliveries and invalidates cache keys.

## Failure and Safety Behavior

- GitHub pagination and output remain bounded; incomplete evidence is explicit and never presented as a complete graph.
- Authentication, rate-limit, or network failure returns actionable unavailable evidence and never falls back to a stale committed manifest.
- Webhook signatures are required when the receiver is enabled; payload bodies and secrets never enter audit logs.
- Duplicate deliveries are idempotent.
- External ticket mutation is outside this change.
- Existing workspace optimistic locking, path policy, and verification invariants are unchanged.

## Migration

1. Add live graph assembly behind the existing graph/selection application interfaces.
2. Switch MCP graph and next-ticket tools to live assembly and add `fresh`.
3. Remove the committed manifest from the production gate, Makefile workflow, normal documentation, and project-sync desired-state path.
4. Retain small JSON fixtures in tests for pure domain validation.
5. Document GitHub permissions, optional webhooks, polling fallback, cache semantics, and migration from the removed manifest.

## Verification

- Unit tests cover pagination, normalization, truncation, cache hit/miss/forced refresh, webhook validation/deduplication/invalidation, and deterministic selection.
- MCP contract tests assert complete `workspace_edit` nested schema and numeric bounds.
- Integration tests use fake `gh` and signed webhook fixtures; no real GitHub writes occur.
- The repository production verification profile passes without a checked-in operational ticket graph.

## Success Criteria

- Adding, editing, closing, parenting, or changing dependencies on a GitHub issue requires no repository file edit.
- Agents see usable structured input for `workspace_edit` and reject out-of-range `context_lines` before issuing a tool call.
- `repo_issue_graph` and `repo_issue_next` reflect GitHub after a forced refresh and converge after TTL expiry even without webhooks.
- No new arbitrary command, remote write, or secret exposure capability is introduced.
