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
evidence. `repo_issue_next` derives readiness from the same snapshot, so it does not
perform a separate live call per issue. Both tools accept `fresh=true` when a caller
must bypass the short-lived cache.

Graph reads are capped at 200 issues. Missing pages, inaccessible issues, malformed
metadata, or API failures are reported through `evidence_complete`, `unavailable`,
and `truncated`; RepoForge does not silently treat partial evidence as complete.

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
