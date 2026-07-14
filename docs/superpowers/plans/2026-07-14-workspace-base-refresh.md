# Workspace Base Freshness and Safe Refresh Implementation Plan

**Issue:** #12  
**Status:** Implemented  
**Goal:** Make upstream base drift visible and refresh an isolated `ai/*` workspace without rebase, force-push, protected-branch mutation, automatic conflict resolution, or stale receipt reuse.

## Architecture

The feature follows existing RepoForge dependency direction:

```text
MCP / CodingService
        ↓
workspace base-status / preview / refresh use cases
        ↓
workspace domain bindings + Git port records
        ↓
fixed-argv Git CLI adapter + workspace store
```

Public callers provide only a workspace ID, exact optimistic-lock values, and an opaque preview ID. They cannot choose a remote, arbitrary ref, strategy, executable, Git argument, or filesystem path.

## Delivered contracts

### `workspace_base_status`

Returns the configured base name, recorded or conservatively inferred workspace-base SHA, local and last-known/latest remote base SHAs, current HEAD, ahead/behind counts, upstream and workspace changed paths, overlaps, publication state, remote availability, and one explicit staleness classification.

### `workspace_refresh_preview`

Fetches the configured base, verifies the exact HEAD and fingerprint before and after inspection, computes merge evidence without changing the worktree, and returns a self-authenticating preview ID bound to:

- workspace identity;
- configured base;
- workspace-base SHA;
- target remote-base SHA;
- HEAD SHA;
- workspace fingerprint;
- merge-only strategy;
- predicted conflict paths;
- clean/dirty state.

### `workspace_refresh`

Recomputes and validates the full preview binding under the workspace lock, then performs only a controlled `git merge --no-ff --no-edit <exact-sha>`.

- Already integrated targets return `current`.
- Clean merges create a controlled merge commit.
- Conflicts return exact policy-allowed paths, abort the merge, and verify restoration of the original HEAD, fingerprint, and clean state.
- Registry persistence failure compensates by resetting to the exact prior HEAD.
- No operation writes a remote, rebases, force-pushes, or mutates a base/protected branch.

## Evidence and publication safety

Every successful refresh invalidates current verification, assessment, architecture, and execution-plan receipts. A refresh-created merge commit receives a private marker, but cannot be pushed until:

1. exact-tree verification succeeds for the resulting HEAD; and
2. `workspace_commit` explicitly approves that controlled merge commit.

Normal `workspace_push` then publishes it without force.

## Test coverage

The real-Git test suite uses temporary bare remotes and worktrees and covers:

- no-op/current state;
- local-base stale, remote-base stale, diverged, and unavailable-remote states;
- upstream-only and local-only histories;
- pushed branches refreshed and later pushed without force;
- read-only preview behavior;
- stale previews after workspace or remote-base changes;
- overlapping content conflicts;
- rename/delete conflicts;
- deterministic conflict abort and restoration;
- receipt invalidation;
- registry-save compensation and audit failure evidence;
- protected-branch rejection;
- service and in-memory MCP protocol contracts;
- reviewed release-contract expansion from 37 to 40 MCP tools.

## Verification and publication checklist

- [x] Typed domain and Git port records added.
- [x] Fixed-argv Git fetch, comparison, preview, merge, abort, and compensation implemented.
- [x] Workspace base status, preview, and refresh use cases implemented.
- [x] Existing typed diagnostic functionality from issue #11 preserved.
- [x] Service and MCP adapters added with reviewed annotations.
- [x] Real-Git, service, MCP, safety, rollback, and contract tests added.
- [x] Tool reference, roadmap, and release contract updated.
- [ ] Run the final RepoForge `full` exact-tree verification on the completed tree.
- [ ] Commit, push without force, and create one draft pull request containing `Closes #12`.
