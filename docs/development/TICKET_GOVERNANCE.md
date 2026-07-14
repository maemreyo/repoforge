# Ticket governance

RepoForge uses GitHub issues as the human-reviewed implementation contracts and
`docs/roadmaps/REPOFORGE_TICKET_GRAPH.json` as the deterministic selection index.
The graph is intentionally compact: detailed scope, acceptance criteria, migration
notes, and verification evidence stay in the issue body and specification comments.

## Required metadata

Every initiative and implementation ticket records Type, Priority, Status, Parent,
Blocked by, Blocks, and one or more Roadmap keys. Allowed priorities are `P0` through
`P3`. Allowed statuses are `Backlog`, `Ready`, `In progress`, `Blocked`, `In review`,
and `Done`.

`Ready` means the issue is fully specified, all listed blockers are `Done`, and an
agent can implement it without inventing product or safety policy. A blank field,
stale dependency edge, circular blocker chain, or authoritative comment that adds an
open blocker makes the ticket non-selectable even when its title still contains
`READY`.

## Authoritative state

The newest explicit tracking update in the issue body or an authoritative maintainer
comment supersedes older title/body metadata. The checked-in graph must be regenerated
from that resolved state. The offline validator fails closed on duplicate IDs, missing
parents, unknown or asymmetric blocker edges, parent/child drift, cycles, and Ready
tickets with open blockers.

## Commands

Run offline validation and show the next deterministic tickets:

```bash
python scripts/validate_ticket_graph.py --next --limit 7
```

The production verification gate runs the same offline validation. Optional live drift
checks are read-only and bounded; they may report unavailable GitHub evidence, but they
never edit issues, labels, projects, or comments.

## Update workflow

1. Update the issue contract and dependency metadata.
2. Regenerate the corresponding node and reciprocal dependency edges in the JSON graph.
3. Run the offline validator and review its diagnostics.
4. Move a ticket to `In progress` when implementation begins, `In review` only after a
   draft PR exists, and `Done` only after the implementation is merged and completion
   evidence is recorded.
5. Update the parent initiative and program issue #3 in the same tracking pass.

Closing an issue does not by itself imply `Done`; cancelled or superseded work should be
recorded as non-selectable with an explanation. Never auto-repair GitHub metadata from
the validator. Drift is evidence for a reviewed tracking update, not permission to
mutate external state.
