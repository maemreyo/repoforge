# GitHub ticket project consistency

GitHub is the source of truth for RepoForge ticket structure and workflow state. Native
sub-issues, native blocked-by relationships, issue state, and optional Project V2 fields
are read directly; no checked-in graph is synchronized back to GitHub.

The existing command remains available as a read-only consistency report:

```bash
rf tickets sync \
  --repo-id repoforge \
  --owner maemreyo \
  --project-number 7
```

It performs authentication, permission, rate-limit, project-access, graph, snapshot, and
drift reads. It may report proposed repairs and conflicts, but those are advisory. Make
the repair directly in GitHub and rerun the command or use `fresh=true` through the
graph tools.

## Apply retirement

`rf tickets sync --apply` is retired and fails before any GitHub call. RepoForge no
longer creates Project fields, adds Project items, changes field values, or rewrites
sub-issue and dependency relationships from repository state. This removes the
two-source synchronization loop that required every ticket edit to be mirrored in a
JSON manifest.

## Expected Project fields

When a Project is configured, RepoForge recognizes these workflow concepts:

- `Type`
- `Priority`
- `Status`
- `Initiative`

Field names are configurable under `repositories.<id>.ticket_graph`. Missing fields or
values make evidence partial; they do not authorize RepoForge to create or mutate the
Project.

## Permissions and bounds

The report requires an authenticated GitHub CLI session with read access to the
repository and, when configured, the Project. Classic OAuth tokens normally need
`read:project` or `project`; fine-grained tokens and GitHub App credentials are
validated through the bounded API calls available to them.

Graph traversal is capped at 200 issues. Pagination, output size, timeouts, repository
identity, owner identity, and project number remain validated. Partial or unavailable
GitHub evidence is surfaced explicitly.

## Automatic freshness

Use the optional signed webhook ingress to invalidate only the affected graph cache when
issues, sub-issues, dependencies, or Project items change. See
[GitHub webhook cache invalidation](GITHUB_WEBHOOKS.md).
