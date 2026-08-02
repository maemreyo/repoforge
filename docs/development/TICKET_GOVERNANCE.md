# Ticket governance

GitHub is RepoForge's authoritative ticket system. Issue bodies remain the human-reviewed
implementation contracts; native sub-issues define the parent/child tree, native
blocked-by relationships define delivery dependencies, and optional Project V2 fields
supply workflow metadata. There is no checked-in production ticket-graph file to update.

## Configuration

Each repository that uses graph tools declares one reviewed root issue:

```toml
[repositories.example.ticket_graph]
root_issue = 3
repository = "owner/repository"

# Optional Project V2 metadata overlay.
project_owner = "owner"
project_number = 7
project_owner_type = "organization"
```

`repository` is optional for direct reads because RepoForge can resolve it from the
checkout, but it is required for deterministic webhook routing. Project owner and number
must be configured together.

## Required metadata

Every initiative and implementation ticket should record Type, Priority, and Status
either in configured Project fields, labels such as `priority:P1`, or simple issue-body
lines such as `Priority: P1`. Allowed priorities are `P0` through `P3`. Allowed
statuses are `Backlog`, `Ready`, `In progress`, `Blocked`, `In review`, and
`Done`.

Parent, children, blockers, and blocked tickets come from GitHub's native relationships;
do not duplicate those edges in a repository file. `Ready` means the issue is fully
specified, its parent is active, all blockers are complete, and WIP policy allows
selection.

## Read behavior

`repo_issue_graph` traverses the configured root through native sub-issues, reads native
dependencies, overlays configured Project fields, and returns deterministic bounded
evidence. Reads are served through batched GraphQL requests (aliased issue queries)
rather than one `gh` process per issue, so a fresh 40-node graph uses a handful of
provider processes; `graph` and `next` results expose `read_stats` with the
provider-process, captured-stdout-byte, and duration counts by capability (top-level
totals exact, per-capability entries shared attribution). A provider process is a `gh`
subprocess launch, not a claim of exactly one HTTPS request, and failed launches are
still counted. Cache hits report `cache_hit_reason`/`cache_age_ms`; live reads report
`cache_miss_reason`. `repo_issue_next` derives readiness from the same snapshot, so it
does not perform a separate live call per issue. Both tools accept `fresh=true` when a
caller must bypass the short-lived cache.

Graph reads are capped at 200 issues. Missing pages, inaccessible issues, malformed
metadata, or API failures are reported through `evidence_complete`, `unavailable`,
`capability_coverage`, and `truncated`; RepoForge does not silently treat partial
evidence as complete. When status or priority metadata is missing, the graph applies a
safe default (`Backlog`/`P3`) and reports one aggregated `METADATA_DEFAULTED` diagnostic
listing the missing fields, without marking the issue provider unavailable. A Project
overlay read failure is isolated to the `project_overlay` capability: GitHub-native
graph evidence remains complete and a `PROJECT_OVERLAY_UNAVAILABLE` diagnostic is
reported; the failure degrades the capability and makes the snapshot not fully
`evidence_complete`, but it is never reported as traversal truncation. Sub-issue or
blocker references pointing at another repository are never mapped onto the same issue
number in the local repository: the affected capability is degraded and a
`CROSS_REPOSITORY_RELATION_UNSUPPORTED` diagnostic is reported instead. An edge node
whose repository identity is missing, null, or malformed fails the capability closed
with an `EDGE_REPOSITORY_IDENTITY_MISSING` diagnostic: without a valid repository
identity a local and a cross-repository issue number cannot be told apart, so the
relation is never assumed local. A same-repository blocker that exists outside the
traversed subtree is never dropped silently: the dependency capability is degraded and
a `DEPENDENCY_OUTSIDE_SELECTION_SCOPE` diagnostic names the unexpanded blockers, so
`next` refuses to select the issue until its dependency context is hydrated. When the
200-node traversal budget is exhausted, `truncated` and `sub_issues_truncated` are set
and a `SUB_ISSUE_TRAVERSAL_BUDGET_EXCEEDED` diagnostic is reported per affected parent;
the unseen children are not enumerated into the schema-bounded `unavailable` sets.

Cached graph snapshots are bound to the resolved GitHub repository slug, a digest of the
reviewed source configuration (root, project owner/number, field names), the reader
contract version, the GitHub API version, and the observation authority fingerprint; a
payload checksum is recomputed and verified on every cache read. Any binding mismatch — a
repointed remote, an edited project field, a reader change, or a rotated authority —
is a cache miss, never stale evidence served as current. The graph cache is fail-closed
by default: RepoForge never reads credentials, so it cannot prove the ambient GitHub
authority is unchanged between a cache write and a later read. When a repository resolves
a configured auth profile, the cache binds to a secret-free auth-issued fingerprint
derived from that identity (host, login/installation, profile), so rotating the credential
generation is automatically a cache miss. The operator-pinned
`server.github_read_cache_authority_digest` remains an explicit legacy fallback in the
compatibility window and must name the current credential generation, rotated whenever
that authority changes. Until an authority is provable the graph cache is neither served
nor written (`cache_miss_reason=authority_not_pinned`); cache telemetry reports
`authority_origin` (`auth_issued` vs `manual_legacy`).
Signed webhooks invalidate the affected graph cache entries; the next read re-traverses
the batched graph rather than merging partial state.

## Update workflow

1. Edit the issue contract in GitHub.
2. Add or remove native sub-issue and blocked-by relationships in GitHub.
3. Update Status, Priority, Type, and Initiative Project fields when a Project is
   configured.
4. Use `fresh=true` for an immediate read, or enable webhook invalidation for automatic
   cache refresh.
5. Move a ticket to `In progress` when work begins, `In review` after a draft PR
   exists, and `Done` after merge and completion evidence.

The legacy `scripts/validate_ticket_graph.py` remains only as an explicit fixture
validator. It is not part of the production gate and does not define operational ticket
state.
