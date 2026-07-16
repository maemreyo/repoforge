# GitHub ticket project synchronization

RepoForge can project the checked-in ticket graph into one existing GitHub Project V2 and native
issue relationships. The manifest remains the source of intent. GitHub supplies live issue identity,
current project state, and relationship evidence.

The command is deliberately dry-run-first:

```bash
rf tickets sync \
  --repo-id repoforge \
  --owner maemreyo \
  --project-number 7
```

The default command performs authentication, permission, rate-limit, project-access, snapshot, drift,
and planning reads only. It does not create fields, add items, edit values, or create relationships.
Review the returned `changes`, `conflicts`, `pending_change_ids`, and `preflight` evidence before applying.

Apply the exact deterministic plan explicitly:

```bash
rf tickets sync \
  --repo-id repoforge \
  --owner maemreyo \
  --project-number 7 \
  --apply \
  --idempotency-key repoforge-ticket-sync-2026-07-16
```

Use `--owner-type user` for a user-owned Project. The default is `organization`.

## Permissions and preflight

Dry-run requires an authenticated GitHub CLI session with access to the target repository and Project.
For classic OAuth tokens, RepoForge expects `read:project` or `project`. Apply expects `project` plus
`repo` or `public_repo`. Fine-grained tokens and GitHub App credentials may not expose classic scope
strings; RepoForge reports that limitation and validates access through the actual bounded Project and
issue API calls.

Preflight reports:

- whether GitHub CLI authentication succeeded;
- detected classic OAuth scopes and missing scopes;
- target Project access;
- the lowest remaining request budget across REST core and GraphQL, with its reset time;
- warnings when rate capacity is low.

Apply never starts when preflight is not ready. Repository `read_only` and `publish_enabled` policy also
apply: a dry-run remains available, while an external write is rejected unless the repository policy
permits publishing.

## Managed projection

RepoForge owns only the following Project fields:

- `Type`
- `Priority`
- `Status`
- `Parent / Initiative`
- `Sequence`
- `Roadmap phase`

It plans the following views:

- `Ready Queue`
- `By Initiative`
- `Blocked`
- `Roadmap`
- `In Review`
- `Done`

It also adds manifest-declared parent/sub-issue and blocked-by relationships. Existing fields, views,
items, or relationships outside this managed set are preserved. RepoForge never deletes issues, closes
work, rewrites issue bodies or specification comments, removes unmanaged relationships, or mutates pull
requests through this command.

When an existing managed field or view has an incompatible shape, the planner emits a conflict instead
of overwriting it. Resolve the conflict in GitHub or update the checked-in intent, then rerun dry-run.

## Determinism and repeat runs

Every planned mutation receives a stable SHA-256 change ID derived from its constrained kind and
canonical payload. Changes are ordered as fields, project items, managed values, sub-issues,
dependencies, and views. A matching projection produces `noop` on a repeat run.

`Ready Queue` intent is `Status:Ready`, ordered by `Priority` and then `Sequence`. GitHub's current view
creation API accepts the view name, layout, and filter but does not accept sort configuration. RepoForge
therefore creates the filtered view and returns an explicit manual action for its sort order instead of
claiming the view is fully configured.

## Partial failure and recovery

Apply executes one stable change at a time. Each change uses an idempotency key formed from the command
key and the stable change ID. On the first failure, RepoForge stops and returns:

- `completed_change_ids` for operations already confirmed or replayed;
- the exact failed change and bounded redacted error;
- `pending_change_ids` that were not attempted;
- any manual actions already discovered.

Rerun the same command with the same `--idempotency-key`. Previously completed changes are replayed from
the private idempotency store rather than submitted twice, and execution resumes with the failed or
remaining work. Run dry-run again after external edits or uncertain API outcomes to refresh the live
snapshot before applying.

## Operational boundaries

The adapter exposes no arbitrary REST endpoint, GraphQL document, command, environment value, or shell
fragment. It maps typed changes to a fixed command set: GitHub Project field/item operations, REST
sub-issue operations, REST issue-dependency operations, and REST Project-view creation. Output,
pagination, timeouts, repository identity, owner identity, project number, and payload types remain
bounded and validated.
