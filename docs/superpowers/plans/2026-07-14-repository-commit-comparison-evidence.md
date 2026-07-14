# Repository Commit and Comparison Evidence Implementation Plan

**Goal:** deliver issue #6 as one independently reviewable PR with two bounded read-only MCP tools.

## Task 1 — Freeze typed contracts and RED integration tests

- Add `tests/test_repository_commit_evidence.py` using temporary real Git repositories.
- Cover branch/tag/SHA equivalence, sanitized commit/actor metadata, root and merge commits, add/modify/delete/rename/binary evidence, denied rename/symlink/gitlink omission, deterministic order, patch truncation, dirty clone isolation, comparison merge-base/ahead/behind, path glob, unrelated history, malformed parser records, invalid refs/limits, service calls, and MCP protocol metadata/invocation.
- Run the repository verification profile and require failures only at missing types/use cases/tools.

## Task 2 — Extend immutable ref and Git evidence ports

- Add frozen commit/file/compare evidence dataclasses to `ports/git.py`.
- Add `read_commit_evidence` and `compare_commits` semantic port methods.
- Extend exact ref resolution with reviewed local tags while retaining full-SHA ancestry checks and rejection of arbitrary branches/revision expressions.
- Add stable evidence error codes.

## Task 3 — Implement Git CLI adapter

- Parse commit metadata through a NUL-delimited fixed format.
- Select the first parent or empty tree for commit evidence.
- Parse `--raw -z` and `--numstat -z` with rename/copy handling and reject malformed or unknown status records.
- Enforce path policy before returning any name and sanitize actor/message metadata before application output.
- Generate optional patches only for visible non-binary literal pathspecs.
- Compute merge-base and ahead/behind; distinguish unrelated from shallow/incomplete history.
- Keep all output deterministic and bounded.

## Task 4 — Add application/service/MCP surface

- Add `repository/commit_read.py` and `repository/compare.py` with typed commands/results, limit and glob validation, and safe audit metadata.
- Wire both through `CodingService`.
- Add two thin `READ_ONLY` MCP tools.
- Keep `repo_recent_commits` unchanged.

## Task 5 — Contracts, docs, and tracker state

- Update MCP tests and tool count from 37 to 39.
- Regenerate and review `docs/contracts/release-contract-v1.json` only for the two intended tools.
- Update tool reference, testing strategy, roadmap current state, and issue #4/program tracking.
- PR body records compatibility, safety, verification, and deferred semantic impact.

## Task 6 — Review and publication

- Review the exact diff for free-form Git args, denied-path leakage, binary patch bodies, unbounded output, and unrelated cleanup.
- Run focused tests, production verification, and RepoForge `full` exact-tree verification.
- Commit `feat(repository): add commit comparison evidence`.
- Push without force and open a draft PR with `Closes #6`.
- Move #6 to `In review` and update parent/program tracking; do not merge or mark ready.
