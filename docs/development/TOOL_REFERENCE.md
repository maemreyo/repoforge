# RepoForge tool reference

RepoForge exposes forty-three focused MCP tools. Each tool has one clear responsibility, and read
operations are separated from write operations so ChatGPT can apply an appropriate confirmation
flow.

## Provider registry

Provider manifests and the provider registry are internal application contracts, not MCP tools.
`ProviderManifest` records the reviewed provider ID, kind, version, digest-pinned executable or image,
supported languages/capabilities, health-probe arguments, coverage/confidence model,
network/filesystem requirements, output limits, and declared fallback. Provider manifests live only in
immutable resolved configuration in this stage; the minimal editable source format has no provider
enrollment path. `ConfigProviderRegistry` accepts only explicitly configured manifests, rejects
duplicate IDs and invalid fallback graphs, orders listings deterministically, and never promotes a
discovered binary into capability. Static availability checks resolve only configured executables and
verify their SHA-256 without executing provider code. Image availability and health-probe execution are
deferred to the provider lifecycle stage. Provider configuration is advisory evidence only and cannot
authorize repository, filesystem, command, network, or publishing access.

## Execution environments

Execution environments are internal application contracts, not MCP tools. `ExecutionEnvironmentPort`
encapsulates doctor, idempotent prepare/cleanup, deterministic identity, approved-command execution,
and declared artifact collection. The native reviewed adapter delegates to the existing constrained
command executor, preserving profile argv, working directory, timeout, output bounds, and failure
behavior. Its identity includes normalized platform/architecture, versions of known safely inspectable
profile tools, reviewed environment names and value hashes, recognized lockfile and manifest digests,
working-directory/network/filesystem policy, and adapter version. Unknown executables produce partial
identity without an extra probe. It excludes source
bodies, command output, full environment bodies, secrets, and absolute user paths. Verification
receipts add an optional `environment_identity_hash`; legacy receipts without this field remain valid.

## Client capability negotiation

Client capabilities are connection-scoped internal contracts, not repository authority and not an MCP
tool. The MCP adapter captures the current session's `InitializeRequestParams` and normalizes protocol
version, client identity, Apps/UI resources, form and URL elicitation, Tasks, progress and cancellation
notifications, tool search and deferred discovery, resource subscriptions, extension versions, and
bounded compatibility flags. Missing, partial, malformed, unknown, and legacy declarations fail closed;
RepoForge never probes an optional protocol method that the client did not negotiate.

`CapabilityPolicy` is the single application decision point for extension emission. Unsupported Apps
fall back to bounded structured results with stable action IDs. Unsupported elicitation returns
`INPUT_REQUIRED` with one stable decision ID and bounded allowed options. Unsupported MCP Tasks use the
existing durable RepoForge operation ID with `operation_status` and, when supported by the operation,
`operation_cancel`. Unsupported tool search exposes the complete safe static tool surface, while missing
progress notifications use status polling by operation ID. These fallbacks preserve existing repository,
filesystem, command, verification, and publication policy; capability data can never grant or widen
access.

## Derived ticket readiness

`repo_issue_next` is an advisory external-read tool. It validates the checked-in graph before any
network access, then reads at most 200 tracked GitHub issues with at most eight concurrent workers. It
never assigns, edits, closes, relabels, or reorders an issue. A missing, malformed, or unreadable live
issue fails closed for that ticket instead of silently trusting stale manifest status.

For each ticket, RepoForge derives status from live open/closed state, specification completeness,
unresolved design gates, parent activity, closed blockers, explicit supersession, and configurable WIP
limits by priority and initiative. Selectable implementation tickets are ordered deterministically by
priority, delivery wave, delivery sequence, then issue number. Results include the derived and declared
status, stable reason codes and messages, unresolved blockers, WIP conflicts, and advisory metadata
repairs. Those repairs are output only; applying them remains a separate human-controlled GitHub action.

## Local runtime commands

`rf runtime status` is a local operator command, not an MCP tool. It compares the reviewed lock
generation on disk with the generation loaded by the live MCP process. Repository mutations include
the same `config_generation`, `active_generation`, and `restart_required` fields. When restart is
required, RepoForge continues to fail closed on stale resolved locks and reports the exact next
action. `rf runtime start` creates a new tunnel-client session leader, records its PID and generation
in a private local state file, and refuses a duplicate managed start. `rf runtime stop` validates the
recorded process identity and process group before sending termination signals; it cannot execute
arbitrary commands or target an unrecorded process. `rf runtime restart` performs that controlled
stop then starts the reviewed runtime again. This stage does not yet provide request draining,
health-check rollback, or in-process hot reload.

`rf runtime logs --tail N` returns at most 1,000 lines from the supervisor-owned tunnel log. The
reader bounds file access to one megabyte and redacts credential-shaped `token`, `secret`, password,
authorization, and control-plane-key values before printing; log bodies never enter the audit log.

`rf runtime status` includes two bounded local checks: whether the identity-validated managed tunnel
process is live and whether its MCP child has published an active generation. It performs no network
health request and therefore cannot disclose tunnel credentials.

`rf runtime reload` is currently the supervisor-managed restart strategy. It validates and stops only
the recorded managed process group before starting the latest reviewed configuration; it does not
mutate a live MCP container in place.

## Diagnostics command

`rf diagnostics bundle` writes a bounded local JSON artifact for support and incident triage. It
contains config hashes, retained generation numbers, and non-secret runtime metadata only. It excludes
configuration bodies, repository file bodies, patches, PR bodies, runtime logs, the full environment,
and tunnel credentials.

When a managed runtime is active, accepted repository additions and refreshes restart it automatically;
a failed expansion restores the prior validated generation. A repository removal is restrictive: failed
activation leaves the restricted configuration on disk and never restores removed repository access.

## Local audit and metrics commands

`rf audit` and `rf audit stats` are local operator commands, not MCP tools. They only read state that
every consumer call (MCP or CLI) already durably records through `ApplicationContext.audited`; they add
no new persistence or instrumentation.

`rf audit --last N --action NAME --failed --slow MS` returns up to `N` (bounded 1-1000, default 20) most
recent private audit events, most recent first, from the active configuration's `audit.jsonl`. `--action`
filters to one action name, `--failed` returns only failed calls, and `--slow MS` returns only calls
whose recorded `duration_ms` is at least `MS`. Each returned event includes its `correlation_id`,
`duration_ms`, and, for failures, `error_code` and `error_type`; audit bodies remain redacted exactly as
written by the audit sink.

`rf audit stats` renders the active configuration's aggregate `operation-metrics.json` as one row per
action: call count, failure count and rate, average and maximum `duration_ms`, and up to three most
frequent failure error codes, sorted slowest-average-first. Use it to find which tool is failing most or
taking the longest before diving into `rf audit --action NAME --slow MS` for individual calls.

`operation-metrics.json` keeps lifetime `operations` totals (unbounded in time, for backward
compatibility with schema version 1 files) alongside bounded `buckets`: one aggregate per action per
UTC day, retained for 30 days and pruned on every write. `rf audit stats --since DATE` (optionally with
`--until DATE`, both `YYYY-MM-DD`) aggregates only the matching daily buckets instead of the lifetime
totals, so an operator can isolate the days after a fix shipped and compare its failure rate or latency
against the period before — without the lifetime totals diluting the comparison. `rf audit stats` with
no `--since`/`--until` is unchanged and still reports the lifetime totals.

`rf repo inspect PATH` and `rf repo propose PATH` inspect local repository facts and return a
structured `pending_approval` proposal without changing configuration or running discovered commands.
Detected verification profiles are classified as an `expansion`. The `repo propose` proposal ID binds
the current configuration, path, repository ID, and profiles, so
`rf repo add PATH --approve approve:PROPOSAL_ID` is the explicit operator action that enrolls exactly
the reviewed capability.

## Configuration generation commands

`rf config history` lists only complete paired source/lock snapshots. `rf config rollback N` validates
the requested snapshot against its source before atomically restoring both files; it never accepts a
partial, stale, or modified snapshot and reports the activation impact after restoration.
Snapshot retention is bounded to the newest ten complete generations.

## Durable operations

| Tool | Purpose |
|---|---|
| `operation_status` | Read one exact durable operation with bounded phase, progress, result-reference, error, retryability, cancellation, and timestamp metadata. |
| `operation_list` | List at most one hundred operations, optionally filtered by `task:<id>`, `workspace:<id>`, or state, with deterministic cursor pagination. |
| `operation_cancel` | Idempotently request cancellation using an optional optimistic `expected_updated_at`; it does not mark the operation terminal. |

RepoForge stores one schema-versioned operation record per private file under the local state root.
Writes use cross-process locking, atomic replacement, fsync, `0600` files, and compare-and-swap on
`updated_at`. On startup, due non-terminal operations expire, unrecoverable running operations become
`orphaned`, and terminal records older than seven days are pruned. Public interfaces cannot create
operations or update progress; those capabilities are internal foundations for approved future
consumers. Operation records never contain source bodies, patches, raw logs, secrets, or environment
bodies.

CLI equivalents are `rf operation status ID`, `rf operation list`, and `rf operation cancel ID`.

## Repository inspection

| Tool | Purpose |
|---|---|
| `repo_list` | List configured repositories, profiles, safe diagnostic metadata, branch policy, pull-request defaults, and change limits. |
| `repo_status` | Read Git status, remotes, current branch information, and `gh auth status`. |
| `repo_context` | Inspect manifests, scripts, engines, root files, and bounded instruction-file previews. |
| `repo_tree` | List policy-allowed regular files from the reviewed default branch or an explicit reachable full commit ID. |
| `repo_read_file` | Read a bounded UTF-8 line range from one committed blob and return its SHA-256 plus exact snapshot identity. |
| `repo_read_files` | Read one bounded line range from multiple committed blobs in the same resolved snapshot, subject to `max_batch_files`. |
| `repo_search` | Run bounded fixed-string search against one committed snapshot with an optional safe path glob. |
| `repo_recent_commits` | Read bounded local commit history, up to one hundred commits. |
| `repo_commit_read` | Inspect one exact reviewed commit with metadata, deterministic changed-file statistics, first-parent/root comparison identity, and an optional bounded patch. |
| `repo_compare` | Compare two exact reviewed commits with merge-base, ahead/behind counts, optional safe path glob, deterministic changed files, and an optional bounded patch. |
| `repo_issue_read` | Read a GitHub issue through `gh` with bounded output. |
| `repo_pr_read` | Read pull-request metadata, files, commits, checks, and reviews through `gh`. |

The committed repository tools never checkout or read working-tree file contents. An omitted snapshot
`ref` resolves to the configured default base branch; explicit refs may be reviewed base branches,
exact local tags whose peeled commits remain in reviewed history, or reachable full commit object IDs.
Every result returns canonical resolved refs and exact commit SHAs. Abbreviated hashes, revision
expressions, arbitrary local branches, and remote refs fail closed.

Commit evidence compares merge commits with their first parent and root commits with Git's empty tree.
Comparison evidence returns the exact merge base plus deterministic ahead/behind counts. Changed paths
are parsed from NUL-delimited Git output and filtered by repository policy before becoming visible; a
rename or copy is returned only when both paths are allowed. Binary entries retain bounded statistics
but binary patch bodies are omitted explicitly. Actor names/emails and commit subject/body text are
bounded and sanitized for credential assignments, bearer tokens, credential URLs, private-key blocks,
high-entropy token shapes, and denied-path snippets. Optional patches are generated only for the already-approved visible
non-binary literal path set. File, patch, line, batch, result, and tool-output limits expose truncation
rather than silently widening output.

## Workspace lifecycle

| Tool | Purpose |
|---|---|
| `workspace_create` | Create one isolated worktree and unique `ai/*` branch from an allowlisted base; accepts an optional bounded `issue_ids` list. |
| `workspace_list` | List workspaces managed by the local RepoForge registry, including age, dirty/clean state, and linked `issue_ids`. |
| `workspace_status` | Return HEAD, branch, Git status, workspace fingerprint, verification state, and change metrics. |
| `workspace_base_status` | Fetch the configured remote base and return exact workspace-base, local-base, remote-base, HEAD, ahead/behind, path-overlap, publication, outage, and staleness evidence. |
| `workspace_refresh_preview` | Produce a read-only preview bound to the exact HEAD, fingerprint, recorded workspace base, latest remote-base SHA, merge strategy, and predicted conflict paths. |
| `workspace_refresh` | Merge only the exact reviewed remote-base target with `--no-ff`; it never rebases, force-pushes, writes the remote, or mutates a protected/base branch. |
| `workspace_remove` | Remove a clean local worktree; remote branches and pull requests are untouched. |

A refresh preview becomes stale when the workspace HEAD, fingerprint, recorded base, remote target, or
predicted merge evidence changes. A conflicting refresh reports policy-allowed conflict paths and aborts
back to the exact reviewed HEAD and fingerprint. A successful refresh invalidates verification,
assessment, architecture, and execution-plan receipts. When it creates a merge commit, the workspace
must pass exact-tree verification and `workspace_commit` must approve that controlled commit before a
normal non-force push can publish it.

`issue_ids` is optional, free-form, display-only metadata (up to 16 entries, each at most 64
characters); RepoForge never validates it against GitHub or any other tracker, and it cannot be changed
after `workspace_create`. The default workflow is one issue per workspace. Pass every dependent issue ID
at creation time only for a deliberate chain of stacked issues worked sequentially in the same worktree.
`workspace_list` and `workspace_status` surface `issue_ids` alongside `created_at` and the dirty/clean
Git state so an operator or agent can decide what is safe to reuse or remove; RepoForge does not
automatically expire or remove workspaces.

## Read, search, and edit

| Tool | Purpose |
|---|---|
| `workspace_tree` | List tracked and untracked paths permitted by repository policy. |
| `workspace_read_file` | Read a bounded UTF-8 line range and return the file SHA-256 for optimistic locking. |
| `workspace_read_files` | Read the same bounded range from multiple files, subject to `max_batch_files`. |
| `workspace_search` | Run bounded literal repository search with an optional path glob. |
| `workspace_write_file` | Create or replace a complete UTF-8 file using optimistic SHA locking. |
| `workspace_replace_text` | Perform an exact replacement with a file SHA and expected occurrence count. |
| `workspace_apply_patch` | Apply a validated git-style unified diff or OpenAI apply_patch envelope against an expected HEAD and workspace fingerprint; deterministic repairs remain policy-checked and auditable by hash. |
| `workspace_restore_paths` | Restore selected tracked paths or remove selected untracked files. |
| `workspace_diff` | Return the diff, diff stat, untracked patch, and change-budget metrics. |

## Verification and publication

| Tool | Purpose |
|---|---|
| `workspace_run_profile` | Run one explicitly named allowlisted command profile; the profile may be non-verifying. Prefer the `quick` profile during the edit-test loop; run `full` (or the repository default) once, immediately before `workspace_commit`. |
| `workspace_run_diagnostic` | Run one repository-reviewed diagnostic with a typed selector, bounded parser, exact fingerprint check, and explicit mutation reporting. Cheaper than a full profile run for iterating on a single failing path during development. |
| `workspace_verify` | Run the default or named verification profile and store a receipt for the exact resulting tree. Run this once per workspace, right before commit — not on every edit. |
| `workspace_commit` | Commit the exact verified tree after enforcing path policy and the configured change budget. |
| `workspace_push` | Push the workspace branch without force and record the pushed commit SHA. |
| `workspace_create_draft_pr` | Create a draft pull request with configured labels, reviewers, and maintainer-edit policy. |
| `workspace_update_draft_pr` | Update the title or body of the existing draft pull request without changing draft state. |
| `workspace_pr_status` | Read draft state, mergeability, review decision, and rolled-up checks. |
| `workspace_pr_checks` | Return compact `pass`, `fail`, `pending`, `skipping`, and `cancel` CI buckets plus exact Check Run selectors when available. |
| `workspace_pr_watch` | Start a durable, cancellable, resumable watch bound to the exact pushed workspace and PR head; use operation tools for status and cancellation. |
| `workspace_pr_check_details` | Resolve one exact `check-run:<id>` selector into bounded Check Run identity, status, attempt, failed-step, annotation, and source metadata. |
| `workspace_pr_failure_evidence` | Return a redacted, bounded failure excerpt, class, hash, retryability, source coverage, uncertainty, and truncation metadata for one selected Check Run. |

Repository `risk.ordered_profiles` typically ranges from a fast `quick` profile through an intermediate
`test` profile up to a slower `full` profile, plus optional single-target diagnostics. Use the cheapest
option that answers the question during the edit-test loop — `quick` or `workspace_run_diagnostic` — and
run `full` (or the repository default passed to `workspace_verify`) only once, right before
`workspace_commit`. Repeating the full profile on every edit wastes its entire timeout budget on runs
that were always going to fail early; a `quick` or diagnostic failure surfaces the same problem sooner
and cheaper. `workspace_commit` still requires the exact tree that the most recent successful
`workspace_verify` receipt covers.

A diagnostic profile is part of the reviewed repository configuration. It fixes the executable and argv
template, selector kind, working directory, timeout, local-only network declaration, mutability, parser,
output limit, and optional artifact paths. Callers provide only `diagnostic_id`, an optional typed selector,
and an optional reviewed workspace fingerprint. Supported selector kinds are `none`, `tracked_path`,
`pytest_node`, `package_name`, `enum`, and `check_id`; path selectors must be policy-allowed tracked
regular files and always occupy one complete argv token. RepoForge never accepts shell fragments,
free-form argv, environment values, executables, or working directories through this tool.

Read-only diagnostics must preserve the exact workspace fingerprint. Artifact diagnostics may change
only configured artifact patterns; every current changed path and any unexpected path is reported. Any
fingerprint change invalidates a prior verification receipt. Missing tools, timeouts, parser failures,
contract drift, dependency/environment failures, output truncation, stale fingerprints, and unexpected
mutation are explicit. Diagnostics do not update golden files, grant commit eligibility, replace
`workspace_verify`, or imply an operating-system network sandbox.

Call `workspace_pr_checks` first and reuse an exact `check-run:<id>` selector; URLs, API paths,
job IDs, and arbitrary `gh` arguments are not accepted. Details and failure evidence require the
workspace's current HEAD, recorded successful push, and selected Check Run to share the same commit
SHA. Evidence uses at most fifty annotations and prioritizes annotations, failed-step metadata, and
Check Run output before a bounded job log. Credential assignments, bearer tokens, credential URLs,
private-key blocks, high-confidence secret-shaped tokens, and lines exposing denied repository paths
are redacted or withheld locally. Optional GitHub permissions or log failures produce explicit
`complete`, `partial`, or `none` coverage plus uncertainty rather than raw external errors. The watch
operation polls with bounded backoff until all checks complete or the first failure appears, persists
only compact counts and exact selectors, resumes eligible active work after restart, and fails closed
when workspace, pushed SHA, PR number, or PR head identity changes. No tool reruns checks, dispatches
workflows, merges, or otherwise administers GitHub Actions.

## Deliberately unsupported capabilities

RepoForge does not expose tools for:

- arbitrary shell execution or unrestricted filesystem access;
- merging a pull request, enabling auto-merge, or marking a draft ready;
- force-pushing;
- writing directly to protected branches;
- reading or managing secrets;
- changing branch protection or repository administration settings;
- creating releases;
- modifying GitHub Actions workflows.

These omissions are part of the security model, not missing convenience features.

## Guided onboarding CLI

### `rf repo discover ROOT [ROOT ...]`

Read-only Git-aware discovery. Reports eligible and excluded repositories with stable reasons. It never creates proposals, sessions, configuration generations, workspaces, or runtime changes.

### `rf repo inspect PATH`

Read-only repository inspection. In addition to repository facts, the response includes `verification_profile_candidates`: bounded, provenance-tagged candidates inferred from Python/uv, Node package-manager, Go, Cargo, and Makefile markers. Detection never executes a command. Dependency-install candidates are marked as requiring explicit network confirmation; accepted profile proposals retain explicit timeouts, and `rf repo inspect` exposes each candidate's network and mutability metadata.

### `rf onboard ROOT [ROOT ...]`

Runs environment preflight, discovery, proposal review, required decisions, exact approvals, candidate smoke tests, one atomic batch acceptance, and at most one activation. The default interactive review is Discovery → Safe defaults → genuinely ambiguous decisions → one consolidated review. In that review Enter accepts, `e` changes one selected decision, and `q` aborts without writing configuration or runtime state.

Important options include `--ui auto|rich|plain`, `--defaults safe|ask|none` (default: `safe`), `--yes`, `--template`, `--activate`, `--plan-only`, `--non-interactive`, `--decision`, `--policy-override`, `--approve`, `--repo-id PATH=ID`, `--wait`, and `--rollback-on-failure`. `--yes` is a zero-prompt safe-default acceptance flow; it stops with exit code `3` rather than guessing an ambiguous decision. Non-interactive mode never loads optional terminal UI packages.

### Session actions

```bash
rf onboard status SESSION_ID
rf onboard resume SESSION_ID
rf onboard cancel SESSION_ID
rf onboard --resume SESSION_ID
```

Exit codes are stable: `0` for completion/read-only success, `2` for validation or operation failure, and `3` when decisions or exact approvals remain. Session files are private metadata with schema version 1.
