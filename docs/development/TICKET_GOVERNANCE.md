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
`CROSS_REPOSITORY_RELATION_UNSUPPORTED` diagnostic is reported instead.

Cached graph snapshots are bound to the resolved GitHub repository slug, a digest of the
reviewed source configuration (root, project owner/number, field names), the reader
contract version, and the GitHub API version; a payload checksum is recomputed and
verified on every cache read. Any binding mismatch — a repointed remote, an edited
project field, or a reader change — is a cache miss, never stale evidence served as
current. Signed webhooks invalidate the affected graph cache entries; the next read
re-traverses the batched graph rather than merging partial state.

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
