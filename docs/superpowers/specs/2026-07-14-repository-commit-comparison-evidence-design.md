# Repository Commit and Comparison Evidence Design

## Context

Issue #6 extends the immutable repository snapshot capability from issue #5. Agents need to inspect one committed change or compare two reviewed committed refs without creating a workspace, reading the dirty source tree, or calling GitHub APIs.

## Selected architecture

Add two typed read-only application use cases over the semantic Git port:

```text
repo_commit_read(repo_id, ref, max_files=100, include_patch=false)
repo_compare(repo_id, base_ref, head_ref, path_glob?, max_files=100, include_patch=false)
```

The Git adapter owns all subprocess argv, exact-ref resolution, NUL-delimited parsing, merge-base calculation, file statistics, rename/copy handling, and bounded patch production. Application use cases validate public limits/globs, invoke the typed port, and audit safe identifiers only. MCP remains a thin closed-world read adapter.

## Ref policy

- Reviewed base branches resolve as exact `refs/heads/<name>` refs.
- Full object IDs resolve only when the commit is reachable from an allowed base branch.
- Exact local tags are accepted in plain or `refs/tags/<name>` form only when their peeled commit remains reachable from reviewed base history.
- Abbreviated hashes, revision expressions, remote refs, arbitrary local branches, control characters, and malformed tag names fail closed.
- Every result includes the caller's requested ref, canonical resolved ref, and exact commit SHA.

## Typed evidence

The Git port returns immutable models for:

- commit identity: commit/tree/parent SHAs, sanitized author and committer identity/date, sanitized subject and bounded body, plus explicit identity/message redaction and truncation flags;
- changed file: status, current path, previous path for rename/copy, additions/deletions or binary state;
- commit evidence: first-parent/root comparison identity, aggregate statistics, deterministic allowed files, omission/truncation metadata, optional bounded aggregate patch;
- comparison evidence: exact base/head/merge-base SHAs, ahead/behind counts, aggregate statistics, deterministic allowed files, omission/truncation metadata, optional bounded aggregate patch.

Merge commits are described against their first parent. Root commits are compared against Git's empty tree.

## Safety and bounds

- Paths are parsed from NUL-delimited Git output and passed through repository path policy before becoming model-visible.
- A rename/copy is visible only when both old and new paths are allowed; otherwise the complete entry is omitted.
- Binary file entries remain visible when their paths are allowed, but binary patch bodies are never requested and `binary_patch_omitted=true` is explicit.
- Optional patches are generated only for the already-approved visible non-binary path set, using literal pathspecs and no external diff command.
- Patch output is bounded by the server tool-output limit and exposes truncation metadata.
- Actor names/emails and commit subject/body are sanitized for credentials, private keys, high-entropy token shapes, and denied-path snippets; callers receive explicit redaction and truncation flags.
- File results are sorted deterministically and bounded by `max_files`; invalid limits fail with a stable error rather than silently widening capability.
- Dirty source-clone files are irrelevant because all reads use committed object IDs.

## Error model

Add stable errors for:

```text
REPOSITORY_HISTORIES_UNRELATED
REPOSITORY_HISTORY_INCOMPLETE
REPOSITORY_EVIDENCE_LIMIT_INVALID
REPOSITORY_EVIDENCE_PARSE_FAILED
```

Existing immutable-ref errors remain authoritative for missing, ambiguous, external, and disallowed refs.

## Compatibility

Existing `repo_tree`, `repo_read_file(s)`, `repo_search`, and `repo_recent_commits` behavior remains unchanged. Extending the resolver to exact reviewed tags is additive. The MCP inventory increases from 37 to 39 tools and the golden release contract changes only for the two intentional read-only tools.

## Testing

Use temporary real Git repositories to cover branch/tag/SHA equivalence, root/empty/merge commits, add/modify/delete/rename/binary changes, deterministic ordering and truncation, denied paths, dirty source clone isolation, path globs, unrelated histories, invalid refs/limits, service wiring, and actual in-memory MCP invocation/annotations.

Final verification is `scripts/verify-production.sh --allow-dirty` followed by RepoForge `full` exact-tree verification.
