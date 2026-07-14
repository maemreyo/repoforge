# Repository Snapshot Inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, read-only repository snapshot inspection at the reviewed default branch or an explicit reachable commit without creating a workspace or reading dirty working-tree state.

**Architecture:** Repository application readers resolve one immutable snapshot through typed Git-port operations, then list, read, batch-read, or search committed objects. The Git CLI adapter uses `rev-parse`, `merge-base`, `ls-tree`, `cat-file`, and `grep` only; service and MCP layers remain thin adapters.

**Tech Stack:** Python 3.10+, dataclasses, Git CLI, FastMCP, pytest, temporary real Git repositories.

## Global Constraints

- Preserve the repository allowlist and canonical repository-relative path policy.
- Accept only the reviewed/default branch, configured allowed base branches, or a full commit object ID reachable from an allowed base branch.
- Reject abbreviated, remote, symbolic, revision-expression, and otherwise disallowed refs with stable error codes.
- Never checkout, mutate, fetch, or read uncommitted source-clone files.
- Enforce UTF-8, binary, symlink, gitlink, batch, line, result, file-size, and output limits.
- Preserve deterministic output ordering and exact snapshot identity in every result.
- Existing workspace read tools remain compatible.

---

### Task 1: Specify repository snapshot behavior with failing tests

**Files:**
- Create: `tests/test_repository_snapshot.py`
- Modify: `tests/test_mcp_contract.py`

**Interfaces:**
- Consumes: existing `ForgeEnvironment`, `CodingService`, MCP in-memory client, and Git fixture helpers.
- Produces: executable expectations for `repo_tree`, `repo_read_file`, `repo_read_files`, and `repo_search`.

- [ ] Add service-level tests proving default-branch and explicit-commit reads return `resolved_ref` and `commit_sha`.
- [ ] Add a dirty-clone test proving committed content is returned after the source working tree changes.
- [ ] Add deterministic tree/search ordering and truncation tests.
- [ ] Add negative tests for denied paths, traversal, binary blobs, symlinks, gitlinks, oversized batches, missing refs, abbreviated refs, remote refs, and disallowed local branches.
- [ ] Extend MCP surface and invocation tests with the four read-only tools.
- [ ] Run the repository `full` profile and confirm failure is caused by the missing public operations.

### Task 2: Add typed Git snapshot primitives

**Files:**
- Modify: `src/repoforge/domain/errors.py`
- Modify: `src/repoforge/ports/git.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/git/cli.py`

**Interfaces:**
- Produces: `ResolvedRepositoryRef`, `GitSnapshotBlob`, `resolve_snapshot_ref`, `list_snapshot_files`, `read_snapshot_blob`, and `search_snapshot`.

- [ ] Add stable ref error codes for not found, ambiguous, external, and disallowed refs.
- [ ] Resolve omitted refs to `refs/heads/<default_base>` and allowed branch names to exact local branch refs.
- [ ] Resolve only full hexadecimal commit IDs and require reachability from an allowed base branch.
- [ ] List committed regular files using bounded `git ls-tree`, filtering denied paths and excluding symlink/gitlink entries.
- [ ] Read one exact literal path by obtaining its tree entry, rejecting unsupported modes, checking blob size, and reading bytes with `git cat-file`.
- [ ] Search one committed tree with bounded fixed-string `git grep`, normalize output to `path:line:text`, filter denied paths, and report truncation.
- [ ] Run focused repository snapshot tests and confirm the Git adapter behaviors pass.

### Task 3: Add repository application readers and public adapters

**Files:**
- Create: `src/repoforge/application/repository/tree.py`
- Create: `src/repoforge/application/repository/file_read.py`
- Create: `src/repoforge/application/repository/files_read.py`
- Create: `src/repoforge/application/repository/search.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`

**Interfaces:**
- Produces: `repo_tree(repo_id, ref=None, max_entries=2000)`, `repo_read_file(repo_id, relative_path, ref=None, start_line=1, end_line=500)`, `repo_read_files(repo_id, relative_paths, ref=None, start_line=1, end_line=500)`, and `repo_search(repo_id, query, ref=None, path_glob=None, max_results=200)`.

- [ ] Implement immutable snapshot resolution once per operation.
- [ ] Return `repo_id`, `resolved_ref`, `commit_sha`, bounded data, hashes where applicable, and `truncated` state.
- [ ] Reuse one resolved snapshot across batch reads and return per-path structured errors without changing the snapshot.
- [ ] Add the four service facade methods.
- [ ] Register the four MCP tools with read-only, closed-world annotations and descriptions beginning with `Use this`.
- [ ] Run service and MCP tests and confirm all new operations pass.

### Task 4: Review compatibility contracts and documentation

**Files:**
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md`
- Modify: `docs/contracts/release-contract-v1.json`
- Modify: contract and architecture tests only when required by intentional surface changes.

**Interfaces:**
- Consumes: final tool schemas generated by the MCP server.
- Produces: reviewed public contract and user-facing documentation for committed snapshot inspection.

- [ ] Document purpose, inputs, output identity, bounds, and safety behavior for each tool.
- [ ] Mark issue #5 capability as implemented in the roadmap without claiming dependent commit/diff inspection.
- [ ] Regenerate and review the release contract because the MCP tool surface intentionally changes.
- [ ] Review the full workspace diff for unrelated changes and public-contract drift.
- [ ] Run `scripts/verify-production.sh --allow-dirty` through the repository verification profile.
- [ ] Run RepoForge exact-tree `full` verification, commit the verified tree, push without force, and create a draft PR containing `Closes #5`.
