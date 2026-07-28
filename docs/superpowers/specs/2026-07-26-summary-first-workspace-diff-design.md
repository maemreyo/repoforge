# Summary-first workspace diff design

Date: 2026-07-26
Status: approved for implementation
Related: #232, #236, PR #250

## Outcome

Make `workspace_diff` cheap by default without weakening exact-state evidence or removing access to
full patch hunks. The default response lists changed files and per-file change counts. Agents request
hunks explicitly and narrowly when they need to inspect patch content.

The design intentionally keeps the public Forge v2 roster at 28 tools.

## Evidence and motivation

Lifetime operation metrics show that legacy `workspace_diff` payloads dominate the measured read
traffic. The current `workspace_diff_v2` path is smaller on average but can still approach its
120,000-byte ceiling. The contract already supports a hunk-free `DiffFile` because `hunks` defaults
to an empty tuple, while `repo_history` already makes patch delivery opt-in through
`include_patch=False`.

Issue #236 remains open and blocked. This change is an immediate inline-payload reduction; it does
not claim to implement generic result resources or complete adaptive transport.

## Considered approaches

### 1. Boolean opt-in for hunks — selected

Add `include_hunks: bool = False` to `WorkspaceDiffInput`.

This matches the existing `repo_history.include_patch` convention, is additive at the schema level,
and has the smallest implementation and documentation surface. It deliberately changes default
behavior: callers that relied on implicit hunks must request them.

### 2. Detail enum

Add `detail: Literal["summary", "hunks"] = "summary"`.

This is more extensible but introduces a new vocabulary for only two modes and does not match the
existing history contract. No third representation is currently required.

### 3. Separate summary tool

Add another public tool for diff summaries.

This is rejected because the v2 roster is fixed at 28 tools and a second tool would duplicate
selection, cursor, exact-state, policy, and metrics behavior.

## Public contract

`WorkspaceDiffInput` gains:

```python
include_hunks: bool = False
```

Existing fields retain their defaults:

- `staged=False`
- `path_glob=None`
- `max_files=100`
- `byte_budget=120_000`
- `cursor=None`

`WorkspaceDiffOutput` and `DiffFile` do not require a structural change. In summary mode every
returned `DiffFile.hunks` is empty. Status, additions, deletions, workspace identity, pagination,
and truncation evidence remain present.

Generated schemas, release contracts, tool descriptions, developer documentation, and golden prompts
must be updated in the same patch.

## Data flow

### Summary mode

1. Resolve the workspace and exact `head_sha` / workspace fingerprint.
2. Apply path policy, `staged`, and `path_glob`.
3. Obtain per-file status and additions/deletions through a Git summary/stat path.
4. Represent binary or otherwise uncountable changes conservatively without fabricating line counts.
5. Paginate summary objects under `max_files` and `byte_budget`.
6. Return `DiffFile` objects with `hunks=()`.

Summary mode must not generate, parse, materialize, and then discard a full unified diff. Reducing
transport while retaining full hunk construction would leave avoidable CPU and memory cost.

Untracked files remain visible. Their summary is derived without constructing a unified diff; a text
file may use its current line count as additions, while binary files use conservative zero line counts
under the existing contract.

### Hunk mode

When `include_hunks=True`, retain the existing structured hunk behavior. Callers should combine this
mode with a narrow `path_glob` and a small `max_files`. The server continues to enforce the byte
budget and returns resumable pagination evidence.

The contract keeps `max_files=100` because summary pages are cheap. Documentation and tool guidance
must recommend at most 20 files per hunk request. A hard conditional cap is deferred unless telemetry
shows callers still make broad hunk requests; silently changing an explicitly supplied `max_files`
would make request semantics harder to reason about.

## Cursor and exact-state rules

The cursor binding request identity must include:

- `staged`
- `path_glob`
- `include_hunks`

A cursor issued for summary mode must be rejected in hunk mode and vice versa. Existing workspace head
and fingerprint binding remains unchanged. `max_files` and `byte_budget` remain page-size controls,
not query identity, so a caller may resume with a smaller transport page.

## Metrics semantics

The existing aggregate `change_metrics` describe the whole workspace rather than the selected
`staged` / `path_glob` result. This patch must make that scope explicit in documentation and must
not present the aggregate as page-local evidence.

Audit details for `workspace_diff_v2` gain `include_hunks`. Operation metrics remain backward
compatible. Daily buckets will be used to compare:

- summary and hunk call counts;
- average and maximum result bytes;
- failures and resumptions;
- broad hunk requests.

If the current metrics model cannot segment by `include_hunks` without a schema expansion, record the
mode in bounded audit details now and create a follow-up metric dimension rather than overloading the
action name.

## Agent guidance

The MCP instructions and tool description must teach this sequence:

1. call `workspace_diff` in default summary mode after meaningful changes;
2. select files whose status or change counts require inspection;
3. request `include_hunks=True` with a narrow `path_glob`;
4. follow `next_cursor` when the selected diff is truncated.

The 120 KB budget remains a safety ceiling, not the intended working response size.

## Compatibility and rollout

This is an additive schema change but a deliberate default-semantic change. Roll it out atomically
with generated contracts, descriptions, tests, and runtime activation so an agent never sees the new
default without guidance explaining how to request hunks.

No compatibility period with `include_hunks=True` is used: that would preserve the dominant payload
behavior and defer the intended benefit. Explicit callers that set `include_hunks=True` retain full
hunks.

The release notes must call out the new default and provide one summary call and one narrow hunk call
example.

## Error handling

- Invalid or cross-mode cursors fail with the existing typed cursor error and unchanged state.
- A summary-stat command failure returns the existing typed Git/command failure; it must not silently
  fall back to a full diff.
- Path-policy, symlink, maximum-file-size, and workspace identity invariants remain unchanged.
- Source or transport truncation continues to return bounded resumable evidence.
- Generic result resources are out of scope and remain tracked by #236.

## Test strategy

### Contract tests

- `include_hunks` exists and defaults to `False`.
- Unknown values and undeclared fields still fail closed.
- Generated JSON schemas and release contracts agree.
- The public tool count remains 28.

### Retrieval unit tests

- Default diff returns correct paths, status, additions/deletions, and empty hunks.
- `include_hunks=True` returns the existing structured hunks.
- Summary mode covers modified, added, deleted, renamed, untracked, staged, binary, and empty changes.
- `path_glob`, `max_files`, and byte-budget pagination work in both modes.
- Summary mode does not call the full-diff adapter path.
- Hunk mode does not call the summary-only adapter path.

### Cursor tests

- Summary cursor resumes summary pages.
- Hunk cursor resumes hunk pages.
- Cross-mode, cross-workspace, cross-head, cross-fingerprint, and cross-filter replay is rejected.
- Resuming with a smaller `max_files` or byte budget remains valid.

### Integration and release tests

- Service and MCP responses default to empty hunks.
- Explicit hunks round-trip through the public tool.
- Tool descriptions teach summary-first usage.
- Generated-contract drift checks pass.
- Production-composition release gates exercise both modes.
- A representative many-file summary remains materially below the previous hunk payload.

## Acceptance criteria

- Default `workspace_diff` returns no hunks and preserves per-file change evidence.
- Explicit narrow hunk requests preserve current patch fidelity.
- Summary mode avoids constructing a full unified diff.
- Cursor replay cannot cross detail modes.
- No public tool is added or removed.
- Documentation and generated contracts match runtime behavior.
- Targeted tests, lint, strict typing, build, and the required final repository gate pass.
- Post-activation daily metrics can distinguish summary-first adoption from explicit hunk use.

## Non-goals

- Generic MCP resource transport.
- Completing all of #236.
- Lowering the global `max_files` default.
- Removing the 120 KB safety ceiling.
- Changing authorization, path policy, branch policy, commit gates, or publication behavior.
