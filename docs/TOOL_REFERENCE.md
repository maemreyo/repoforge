# RepoForge tool reference

RepoForge exposes twenty-seven focused MCP tools. Each tool has one clear responsibility, and read
operations are separated from write operations so ChatGPT can apply an appropriate confirmation
flow.

## Local runtime commands

`rf runtime status` is a local operator command, not an MCP tool. It compares the reviewed lock
generation on disk with the generation loaded by the live MCP process. Repository mutations include
the same `config_generation`, `active_generation`, and `restart_required` fields. When restart is
required, RepoForge continues to fail closed on stale resolved locks and reports the exact next
action; this release does not yet supervise or restart the tunnel process automatically.

## Repository inspection

| Tool | Purpose |
|---|---|
| `repo_list` | List configured repositories, profiles, branch policy, pull-request defaults, and change limits. |
| `repo_status` | Read Git status, remotes, current branch information, and `gh auth status`. |
| `repo_context` | Inspect manifests, scripts, engines, root files, and bounded instruction-file previews. |
| `repo_recent_commits` | Read bounded local commit history, up to one hundred commits. |
| `repo_issue_read` | Read a GitHub issue through `gh` with bounded output. |
| `repo_pr_read` | Read pull-request metadata, files, commits, checks, and reviews through `gh`. |

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
| `workspace_pr_checks` | Return compact `pass`, `fail`, `pending`, `skipping`, and `cancel` CI buckets. |

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
