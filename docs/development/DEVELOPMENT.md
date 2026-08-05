# Development guide

## Environment setup

Synchronize the locked development environment:

```bash
uv sync --extra dev
```

`uv.lock` is committed to make the dependency graph reproducible. Use locked execution in CI and
release validation when configuration drift must fail instead of updating the lockfile:

```bash
uv sync --extra dev --locked
uv run --locked pytest
```

## Quality gates

The supported Make targets are:

```bash
make lint
make typecheck
make test
make test-full
make test-fast
make test-affected
make coverage
make gate
make test-groups-check
make build
make check
make production-check
```

Equivalent direct commands are:

```bash
uv run ruff check .
uv run mypy src/repoforge
uv run python scripts/select_affected_tests.py --run
uv run python scripts/select_affected_tests.py --full --run
uv run python scripts/run_test_suite.py --coverage-dir .cache/verification/coverage
uv build
```

Run the complete project gate with:

```bash
./scripts/test-all.sh
```

`make test` is the default development loop: affected tests plus the safety bundle, without
coverage. `make test-full` runs every behavioral test without coverage, while `make coverage`
runs the single canonical full branch-coverage suite. `make test-fast` and
`make test-affected` are transition aliases for `test-full` and `test` respectively.

`make gate` and `make production-check` run the clean-tree authoritative gate. `make check`
runs the same gate with a dirty-tree allowance for development. They delegate to
`scripts/verify-production.sh` (dirty vs. clean tree); there is no separate lightweight
variant. The production authority is `scripts/verify-production.sh`; use `--allow-dirty`
only while iterating. Its ordered guarantees are documented in
[INTEGRITY_POLICY.md](INTEGRITY_POLICY.md), while issue metadata and tracking rules are
documented in [TICKET_GOVERNANCE.md](TICKET_GOVERNANCE.md).

A change is not release-ready until ticket and release contracts, linting, strict typing, the
canonical branch-coverage run, clean package builds, and installed-wheel smoke all pass. During
iteration, `make verify` deliberately stops at affected tests so it does not duplicate release work.
The production gate runs the suite in validated lanes and combines their coverage data: exclusive
lanes for stateful (`parallel = false`) groups, then everything proven isolated in one xdist run. Set
`REPOFORGE_TEST_WORKERS` to a positive integer to tune local parallelism without changing scope; the
default of 3 is the measured cap for this suite, above which git child processes oversubscribe cores
and produce contention-flaky failures.

### Module-aware test selection

`tests/test-groups.toml` is the current execution authority and maps every `tests/test_*.py` file to one
capability group (contracts, workspace core, operations, runtime/activation,
policy/configuration, GitHub provider, verification/diagnostics, ticket-graph/release-e2e, or the
cross-cutting `platform` catch-all), plus a small always-on safety bundle. `make test-groups-check`
(`scripts/select_affected_tests.py --check-completeness`) fails if any test file is unmapped, mapped
to more than one group, or stale; this runs as part of the normal test suite too
(`tests/test_select_affected_tests.py`).

`tests/catalog.toml` is the declarative shadow catalog. It adds source-to-test edges, isolation and
named resources, platform/Python support, and cost classes. Validate it with
`uv run python scripts/test_catalog.py --check tests/catalog.toml`. The catalog does not execute tests
yet; shadow comparison must remain free of false negatives before any capability cuts over.

`make test` (`scripts/select_affected_tests.py --run`) maps the paths changed since
`REPOFORGE_TEST_AFFECTED_BASE` (default `main`), plus the current working tree, to test groups and
runs only the selected groups' tests plus the safety bundle. It **fails closed**: any changed path
that does not match a group's `source_globs` (or matches a small always-wide list such as
`pyproject.toml`, `Makefile`, or `.github/workflows/**`) escalates the run
to the full suite rather than silently skipping something it cannot map.

Use `make test` throughout iteration, `make test-full` when broad behavioral confidence is needed,
and one `make coverage` observation when branch coverage is the question. Do not run canonical
coverage immediately before `make gate`: the production gate already records and enforces the same
full-suite coverage evidence.

Run evidence is written under `.cache/verification/`. Protected CI runs one canonical coverage job
and reuses its `.coverage` data for map validation. Set `REPOFORGE_CI_FULL_MATRIX=1` only as an
emergency rollback to full no-coverage execution in every compatibility matrix cell.

## Local MCP debugging

Launch MCP Inspector:

```bash
./scripts/inspect-mcp.sh
```

Run the stdio server directly:

```bash
REPOFORGE_CONFIG="$HOME/.config/repoforge/config.toml" uv run rf serve
```

The stdio transport reserves stdout for MCP JSON-RPC messages. Send diagnostics to stderr or the
configured audit log; never print debug output to stdout.

## Source layout

```text
src/repoforge/domain/       pure contracts, invariants, errors, risk, and patch models
src/repoforge/application/  use cases for configuration, onboarding, repositories, and workspaces
src/repoforge/ports/        typed boundaries for Git, filesystem, persistence, runtime, and GitHub
src/repoforge/adapters/     constrained local implementations of those boundaries
src/repoforge/interfaces/   CLI, MCP, and runtime composition-facing adapters
tests/                      unit, security, integration, CLI, and MCP protocol tests
docs/                       operator, developer, testing, and tool documentation
scripts/                    reproducible development and operational entry points
```

Keep security policy in the policy layer. MCP handlers should remain thin adapters over typed service
methods.

## Adding or changing an MCP tool

Every tool change should include:

1. One clear read or write responsibility.
2. A typed service method with constrained inputs.
3. A precise tool name, title, description, and accurate MCP annotations.
4. Stable structured output and actionable error messages.
5. Server-side branch, path, state, and permission enforcement.
6. Positive, negative, stale-state, and failure-path tests.
7. Invocation through an actual in-memory MCP client session.
8. Updates to [TOOL_REFERENCE.md](TOOL_REFERENCE.md) and relevant golden prompts.

Do not add arbitrary command strings, generic filesystem access, merge operations, force-pushes,
protected-branch writes, secret operations, or workflow-editing capabilities.

## Configuration changes

When configuration fields or defaults change:

1. preserve compatibility where practical;
2. update `config.example.toml` and relevant tracked examples;
3. add valid and invalid configuration tests;
4. run `rf config path`, `rf show-config`, `rf doctor`, and `rf repo list`;
5. document required operator actions;
6. verify that permissions were not silently broadened.

### Resource budgets

Resource budgets are configured in `[server.resource_budget]`. Repository-specific
`[repositories.<repo_id>.resource_budget]` tables inherit server values and may only tighten them.
Budgets constrain local resource pressure; they do not reduce required verification or expand
repository, command, network, or publication authority.

### Repository policy presets

A resolved repository table can set `policy = "strict" | "standard" | "relaxed"`. The typed loader expands the selected preset into the reviewed lock, then lets explicitly supplied repository fields win. A path-only repository table uses `strict`; existing expanded repository tables remain compatible without a preset.

Editable source configuration stays minimal: rendering omits the default `standard` source template and empty decision or policy-override lists. Parsing restores those defaults before RepoForge generates the fully explicit reviewed lock.

| Preset | Read-only | Publishing | Changed files | Diff lines | Changed bytes |
| --- | --- | --- | ---: | ---: | ---: |
| `strict` | yes | no | 25 | 2,000 | 5 MiB |
| `standard` | no | no | 75 | 6,000 | 10 MiB |
| `relaxed` | no | yes | 150 | 12,000 | 25 MiB |

All presets preserve hard safety invariants: protected branches, canonical path enforcement, denied paths, and symlink/submodule escape protections. `relaxed` is not an unrestricted policy.

## Documentation changes

Documentation should be written in clear professional English. Keep commands executable, avoid
machine-specific credentials, and distinguish automated validation from checks that require a live
GitHub account or Secure MCP Tunnel.

When tool metadata changes, rerun direct, indirect, and negative Plugin prompts from
[PLUGIN_TEST_CASES.md](../testing/PLUGIN_TEST_CASES.md).

## Definition of done

Before presenting a change as complete:

- the requested behavior is implemented without weakening safety boundaries;
- relevant unit, integration, security, and MCP contract tests pass;
- `./scripts/test-all.sh` passes;
- tool schemas, annotations, and documentation agree;
- generated distributions build successfully;
- the final diff contains only intended changes;
- the completion report lists every command actually run and every live check not run.

### Reviving an old PR whose work was partially squash-merged

A PR that has been open across a squash-merge of most of its content onto `main` is
not a candidate for a blind `git rebase` or a full `git cherry-pick` of its branch
history: the squash is not patch-identical to the original commits, so those
operations replay already-merged ancestors and conflict. Reconstruct the change as a
minimal delta instead:

1. Inspect merge-base and divergence between the PR branch and `main`.
2. Compare the final trees (`git diff main...pr-branch`) and check patch/tree overlap
   with what `main` already contains.
3. Identify commits that were squash-equivalently absorbed by `main`.
4. When the branch history is no longer replay-safe, rebuild a minimal branch from
   exact `origin/main`: apply only the unique delta (verified by a tree diff), and
   keep any fix `main` does not already carry.
5. Run the PR gates (unit, lint, typecheck, coverage-map check) on the rebuilt HEAD.
6. After merge, verify the push-only/main-only gates — `canonical-coverage` and
   `compatibility` typically run only on `main` pushes, and a coverage-map repair is
   not proven until the `main` run goes green.
7. Only then clean up the workspace and the merged branch.

Do not declare a change mergeable on a HEAD whose CI never ran, and never let a
squash-merged equivalent in `main` be silently dropped from the rebuilt delta.
