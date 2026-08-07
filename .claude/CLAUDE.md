<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

## Testing — never default to the full suite

For an ordinary change, run `make test` (equivalently `uv run python
scripts/select_affected_tests.py --run`), never `./scripts/test-all.sh`. `make test` maps the
current diff to its exact covering tests via `tests/coverage-map.json` /
`tests/test-groups.toml` and runs only those, with real `-n 3` parallelism — the full suite is
single-process and takes the better part of an hour, which is not a routine local gate.

The selector fails closed: a changed path with no coverage/group mapping escalates to the full
suite rather than silently under-testing. If that fires for a path that should have a narrow
mapping, fix the mapping (add the path to the owning group's `source_globs` in
`tests/test-groups.toml` — exact-listed, not just covered by a wildcard, for a
`src/repoforge/**.py` module the coverage map cannot attribute, e.g. generated/data-only
files) instead of accepting a full run as the new normal. See `AGENTS.md`'s "Golden commands"
and "Testing expectations" sections for the full rationale.

Run `./scripts/test-all.sh` only for a release candidate, to reproduce an order-dependent
failure the selector's narrower run cannot, or when the user explicitly asks for a full run.

## Deploying a fix: `git commit` alone does nothing to the live instance

A running RepoForge is a frozen release copy under `~/.local/share/repoforge/releases/`, not this
checkout. To actually ship a source change: commit (uncommitted worktree → `rf upgrade` fails
`WORKTREE_DIRTY`), `rf upgrade --from-worktree . --keep 5` to stage without activating, then
`rf upgrade --from-worktree . --activate --watch --health-window 90 --keep 5` to activate with
auto-rollback. Verify via `rf runtime status`'s `running_executable`, not by importing this
checkout's source directly — that only proves the fix, not that the deployed server has it. See
`AGENTS.md`'s "Shipping a code fix to the live instance" for the full flow and rollback command.
