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
make test-fast
make test-affected
make test-groups-check
make build
make check
make production-check
```

Equivalent direct commands are:

```bash
uv run ruff check .
uv run mypy src/repoforge
uv run pytest --cov=repoforge --cov-report=term-missing
uv build
```

Run the complete project gate with:

```bash
./scripts/test-all.sh
```

`make check` and `make production-check` both run the same authoritative gate,
`scripts/verify-production.sh` (dirty vs. clean tree); there is no separate lightweight
variant. The production authority is `scripts/verify-production.sh`; use `--allow-dirty`
only while iterating. Its ordered guarantees are documented in
[INTEGRITY_POLICY.md](INTEGRITY_POLICY.md), while issue metadata and tracking rules are
documented in [TICKET_GOVERNANCE.md](TICKET_GOVERNANCE.md).

A change is not complete until ticket and release contracts, linting, strict typing, tests with the
configured branch-coverage threshold, clean package builds, and installed-wheel smoke all pass.
The production gate runs deterministic capability-group-aware test shards (a dedicated serial lane
for stateful groups, plus timing-aware balancing for the rest) and combines their coverage data; set
`REPOFORGE_TEST_SHARDS` to a positive integer to tune local parallelism without changing scope.

### Module-aware test selection

`tests/test-groups.toml` is a checked-in manifest that maps every `tests/test_*.py` file to one
capability group (contracts, workspace core, operations, runtime/activation,
policy/configuration, GitHub provider, verification/diagnostics, ticket-graph/release-e2e, or the
cross-cutting `platform` catch-all), plus a small always-on safety bundle. `make test-groups-check`
(`scripts/select_affected_tests.py --check-completeness`) fails if any test file is unmapped, mapped
to more than one group, or stale; this runs as part of the normal test suite too
(`tests/test_select_affected_tests.py`).

`make test-affected` (`scripts/select_affected_tests.py --run`) maps the paths changed since
`REPOFORGE_TEST_AFFECTED_BASE` (default `main`), plus the current working tree, to test groups and
runs only the selected groups' tests plus the safety bundle. It **fails closed**: any changed path
that does not match a group's `source_globs` (or matches a small always-wide list such as
`pyproject.toml`, `Makefile`, `config.repoforge.toml`, or `.github/workflows/**`) escalates the run
to the full suite rather than silently skipping something it cannot map.

Do not run `make test` immediately before `make check` / `make production-check` / the `full`
verification profile on the same tree: `full` already runs the complete suite via
`scripts/verify-production.sh`, so running `test` first only duplicates that work. Use `quick` for
lint/type feedback, `test-affected` for a fast targeted pass, and `full` as the single authoritative
run before a PR is ready.

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
