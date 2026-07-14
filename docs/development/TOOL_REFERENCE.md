# RepoForge tool reference

RepoForge exposes thirty-six focused MCP tools. Each tool has one clear responsibility, and read
operations are separated from write operations so ChatGPT can apply an appropriate confirmation
flow.

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

## Repository proposal commands

`rf repo inspect PATH` and `rf repo add PATH --preview` inspect local repository facts and return a
structured `pending_approval` proposal without changing configuration or running discovered commands.
Detected verification profiles are classified as an `expansion`. The `repo add --preview` proposal ID
binds the current configuration, path, repository ID, and profiles, so `rf repo add PATH --approve ID`
is the explicit operator action that enrolls exactly the reviewed capability.

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
| `repo_list` | List configured repositories, profiles, branch policy, pull-request defaults, and change limits. |
| `repo_status` | Read Git status, remotes, current branch information, and `gh auth status`. |
| `repo_context` | Inspect manifests, scripts, engines, root files, and bounded instruction-file previews. |
| `repo_tree` | List policy-allowed regular files from the reviewed default branch or an explicit reachable full commit ID. |
| `repo_read_file` | Read a bounded UTF-8 line range from one committed blob and return its SHA-256 plus exact snapshot identity. |
| `repo_read_files` | Read one bounded line range from multiple committed blobs in the same resolved snapshot, subject to `max_batch_files`. |
| `repo_search` | Run bounded fixed-string search against one committed snapshot with an optional safe path glob. |
| `repo_recent_commits` | Read bounded local commit history, up to one hundred commits. |
| `repo_issue_read` | Read a GitHub issue through `gh` with bounded output. |
| `repo_pr_read` | Read pull-request metadata, files, commits, checks, and reviews through `gh`. |

The committed snapshot tools never checkout or read working-tree file contents. An omitted `ref`
resolves to the configured default base branch; an explicit ref must be an allowlisted base branch or
a full commit object ID reachable from one. Every result returns `resolved_ref` and `commit_sha`.
Denied paths, symlinks, gitlinks, binary or non-UTF-8 blobs, oversized files, unsafe globs, ambiguous
object prefixes, remote refs, and revision expressions fail closed. Tree and search results are sorted,
and line, batch, result, file-size, and tool-output limits report truncation explicitly.

## Workspace lifecycle

| Tool | Purpose |
|---|---|
| `workspace_create` | Create one isolated worktree and unique `ai/*` branch from an allowlisted base. |
| `workspace_list` | List workspaces managed by the local RepoForge registry. |
| `workspace_status` | Return HEAD, branch, Git status, workspace fingerprint, verification state, and change metrics. |
| `workspace_remove` | Remove a clean local worktree; remote branches and pull requests are untouched. |

## Read, search, and edit

| Tool | Purpose |
|---|---|
| `workspace_tree` | List tracked and untracked paths permitted by repository policy. |
| `workspace_read_file` | Read a bounded UTF-8 line range and return the file SHA-256 for optimistic locking. |
| `workspace_read_files` | Read the same bounded range from multiple files, subject to `max_batch_files`. |
| `workspace_search` | Run bounded literal repository search with an optional path glob. |
| `workspace_write_file` | Create or replace a complete UTF-8 file using optimistic SHA locking. |
| `workspace_replace_text` | Perform an exact replacement with a file SHA and expected occurrence count. |
| `workspace_apply_patch` | Apply a validated unified patch against an expected HEAD and workspace fingerprint. |
| `workspace_restore_paths` | Restore selected tracked paths or remove selected untracked files. |
| `workspace_diff` | Return the diff, diff stat, untracked patch, and change-budget metrics. |

## Verification and publication

| Tool | Purpose |
|---|---|
| `workspace_run_profile` | Run one explicitly named allowlisted command profile; the profile may be non-verifying. |
| `workspace_verify` | Run the default or named verification profile and store a receipt for the exact resulting tree. |
| `workspace_commit` | Commit the exact verified tree after enforcing path policy and the configured change budget. |
| `workspace_push` | Push the workspace branch without force and record the pushed commit SHA. |
| `workspace_create_draft_pr` | Create a draft pull request with configured labels, reviewers, and maintainer-edit policy. |
| `workspace_update_draft_pr` | Update the title or body of the existing draft pull request without changing draft state. |
| `workspace_pr_status` | Read draft state, mergeability, review decision, and rolled-up checks. |
| `workspace_pr_checks` | Return compact `pass`, `fail`, `pending`, `skipping`, and `cancel` CI buckets plus exact Check Run selectors when available. |
| `workspace_pr_check_details` | Resolve one exact `check-run:<id>` selector into bounded Check Run identity, status, attempt, failed-step, annotation, and source metadata. |
| `workspace_pr_failure_evidence` | Return a redacted, bounded failure excerpt, class, hash, retryability, source coverage, uncertainty, and truncation metadata for one selected Check Run. |

Call `workspace_pr_checks` first and reuse an exact `check-run:<id>` selector; URLs, API paths,
job IDs, and arbitrary `gh` arguments are not accepted. Details and failure evidence require the
workspace's current HEAD, recorded successful push, and selected Check Run to share the same commit
SHA. Evidence uses at most fifty annotations and prioritizes annotations, failed-step metadata, and
Check Run output before a bounded job log. Credential assignments, bearer tokens, credential URLs,
private-key blocks, high-confidence secret-shaped tokens, and lines exposing denied repository paths
are redacted or withheld locally. Optional GitHub permissions or log failures produce explicit
`complete`, `partial`, or `none` coverage plus uncertainty rather than raw external errors. These tools
do not rerun, cancel, watch, or administer GitHub Actions.

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

### `rf onboard ROOT [ROOT ...]`

Runs environment preflight, discovery, proposal review, required decisions, exact approvals, candidate smoke tests, one atomic batch acceptance, and at most one activation. Interactive review is presented as Discovery → Safe defaults → Ambiguous decisions → Repository summaries → Config diff → Apply.

Important options include `--ui auto|rich|plain`, `--defaults safe|ask|none`, `--template`, `--activate`, `--plan-only`, `--non-interactive`, `--decision`, `--policy-override`, `--approve`, `--repo-id PATH=ID`, `--wait`, and `--rollback-on-failure`. Non-interactive mode accepts only `--defaults none` and never loads optional terminal UI packages.

### Session actions

```bash
rf onboard status SESSION_ID
rf onboard resume SESSION_ID
rf onboard cancel SESSION_ID
rf onboard --resume SESSION_ID
```

Exit codes are stable: `0` for completion/read-only success, `2` for validation or operation failure, and `3` when decisions or exact approvals remain. Session files are private metadata with schema version 1.
