# AGENTS.md

This file is the canonical operating guide for coding agents working on RepoForge itself.

RepoForge is a write-capable local MCP server. Correctness includes not only producing valid Python,
but also preserving the safety boundary between ChatGPT, local repositories, Git, GitHub, and the
operator. A change that weakens that boundary is not acceptable merely because tests pass.

A more deeply nested `AGENTS.md` may narrow these rules for its subtree. Explicit user instructions
take precedence, provided they do not require unsafe or unsupported behavior.

## Mission

RepoForge gives ChatGPT a constrained workflow for:

1. inspecting an allowlisted local Git repository;
2. creating an isolated `git worktree` on an `ai/*` branch;
3. reading and changing allowed files;
4. running repository-defined command profiles;
5. committing an exactly verified tree;
6. pushing without force; and
7. creating and observing a draft pull request.

RepoForge is not a general shell, remote administration tool, merge bot, secret manager, or CI
bypass mechanism.

## Source-of-truth order

When project sources disagree, use this order:

1. executable tests and safety checks;
2. typed configuration models and service behavior;
3. MCP tool contracts and annotations;
4. this file and `SECURITY.md`;
5. user-facing documentation and examples.

Do not weaken executable safety behavior to match stale documentation. Update the documentation
instead.

## Repository layout

```text
src/repoforge/config.py       TOML models, defaults, and validation
src/repoforge/discovery.py    repository detection and config rendering
src/repoforge/user_config.py  minimal user intent and reviewed runtime config locks
src/repoforge/security.py     branch, path, patch, and policy enforcement
src/repoforge/runner.py       constrained subprocess execution
src/repoforge/state.py        workspace registry, locks, fingerprints, receipts
src/repoforge/service.py      Git, worktree, gh, verification, and PR operations
src/repoforge/onboarding.py   setup, repository enrollment, and tunnel startup UX
src/repoforge/server.py       MCP tool metadata and registration
src/repoforge/cli.py          init, doctor, smoke test, audit, and tunnel DX
tests/                        unit, security, integration, CLI, and MCP contract tests
docs/                         setup, operations, tools, testing, and golden prompts
scripts/                      reproducible developer and operator commands
```

Keep policy in the policy layer. Do not duplicate security decisions across tool handlers.

## Supported development environment

- Python 3.10 or newer.
- `uv` is the preferred environment and lockfile runner.
- Git is required for integration tests.
- GitHub CLI is required for live operator checks, but automated tests must use a fake `gh` unless a
  test is explicitly marked as live.
- Node.js and `npx` are required only for MCP Inspector workflows.

## Golden commands

Run from the RepoForge repository root:

```sh
uv sync --extra dev
make lint
make typecheck
make test
make build
make check
```

The complete local gate is:

```sh
./scripts/test-all.sh
```

Before a live Plugin session:

```sh
rf doctor --fix
rf smoke-test --repo-id work-frontier
./scripts/e2e-preflight.sh
```

Use MCP Inspector for manual protocol inspection:

```sh
./scripts/inspect-mcp.sh
```

## Non-negotiable safety invariants

The following invariants must remain true unless the user explicitly requests a security redesign
and the change includes a documented threat-model update:

- No arbitrary shell or free-form command argument exposed to the model.
- Repository access is allowlisted by `repo_id`; model-provided absolute paths are not accepted.
- File paths are canonicalized and may not escape the repository or worktree.
- Protected branches cannot be edited.
- Writable branches must use the configured prefix, normally `ai/`.
- Work happens in an isolated worktree, not in the user's source clone.
- Denied paths include secrets, credentials, private keys, `.git`, and GitHub Actions workflows by
  default.
- Symlinks, submodules, and gitlinks cannot be used to escape file policy.
- Writes use optimistic locking or an exact workspace fingerprint.
- A verification receipt is invalidated by any subsequent tree change.
- Commit requires the exact verified tree when the repository policy requires verification.
- Push never uses force.
- Pull requests are created as drafts.
- There is no merge, auto-merge, branch-protection, secrets, release, or workflow-editing tool.
- MCP stdio mode writes protocol messages only to stdout. Diagnostics belong on stderr.
- Audit logs never contain secrets, file bodies, patches, PR bodies, or the full process environment.

A helper refactor that bypasses any of these checks is a security regression.

## Working rules

1. Read the affected service, policy, configuration, tests, and tool metadata before editing.
2. Keep changes scoped to one coherent outcome.
3. Prefer small typed helpers over a broad abstraction that obscures security decisions.
4. Do not accept a new generic `dict[str, Any]` when a typed model can express the contract.
5. Keep returned structured fields stable. Add fields compatibly; do not silently rename or remove
   them.
6. Preserve deterministic output ordering where practical.
7. Bound file reads, search results, subprocess output, and batch sizes.
8. Use explicit timeouts for every subprocess that can block.
9. Do not add dependencies without explaining why the standard library or current dependencies are
   insufficient.
10. Do not log credentials or add debug output to MCP stdout.

## Adding or changing an MCP tool

Every tool change must include all of the following:

1. A single clear responsibility: one read action or one write action.
2. A typed service method; the MCP function should remain a thin adapter.
3. A precise title and a description beginning with the intended use case.
4. Accurate annotations:
   - read-only tools declare read-only behavior;
   - external writes are marked as open-world writes;
   - overwriting or deleting local state is marked destructive;
   - annotations never replace server-side enforcement.
5. Constrained inputs using enums, limits, reusable IDs, and bounded strings where applicable.
6. Stable structured output with actionable error messages.
7. Positive unit coverage.
8. Negative and stale-state coverage.
9. MCP protocol-level coverage through an actual client session.
10. Documentation updates in `docs/development/TOOL_REFERENCE.md`.
11. Golden-prompt updates when discovery or tool selection can change.

Do not add two tools that differ only by wording. Prefer one well-scoped tool with a constrained
parameter.

## Testing expectations

For ordinary implementation changes:

```sh
uv run pytest tests/path/to/relevant_test.py
./scripts/test-all.sh
```

For safety-policy, state, runner, Git, or write-tool changes, include:

- a positive path;
- invalid input;
- path traversal or denied-path behavior where relevant;
- stale SHA, stale HEAD, or stale fingerprint behavior;
- post-verification mutation behavior;
- subprocess failure and timeout behavior;
- audit behavior without sensitive payloads;
- real local Git/worktree integration;
- in-memory MCP schema and invocation coverage.

For tool metadata changes, run the direct, indirect, and negative prompts in
`docs/testing/PLUGIN_TEST_CASES.md`, then record results using
`docs/testing/TEST_RUN_RECORD.md`.

For release candidates, follow `docs/testing/FULL_FLOW_TESTING.md`.

## Test isolation

Automated tests must not:

- use the developer's real repositories;
- push to a real remote;
- create or update a real pull request;
- depend on the user's global Git configuration;
- read the user's actual credentials;
- rely on an existing worktree or branch.

Use temporary directories, local bare remotes, deterministic fixtures, and a fake `gh` executable.
Live tests must be opt-in and clearly labelled.

## Configuration changes

When changing configuration:

1. update validation and defaults;
2. add migration or compatibility behavior when existing config files would break;
3. update `config.example.toml`;
4. update the Work Frontier example when relevant;
5. add valid and invalid config tests;
6. verify `rf doctor`, `rf show-config`, and `rf init`;
7. document any operator action required after upgrading.

Never silently broaden repository, path, command, environment, or GitHub permissions.

## Documentation rules

Update documentation in the same patch when changing:

- CLI flags or output;
- tool names, parameters, annotations, or structured output;
- config fields and defaults;
- safety policy;
- setup, tunnel, or authentication behavior;
- verification or PR lifecycle behavior.

Examples must use draft PRs and non-sensitive placeholder values. Never include real API keys,
tokens, private URLs, home-directory secrets, or audit payloads copied from a real machine.

## Commit and pull-request guidance

Use Conventional Commit-style subjects:

```text
feat: add bounded workspace operation
fix: reject stale verification receipt
test: cover denied-path regression
docs: add live end-to-end runbook
refactor: centralize fingerprint validation
chore: update locked development dependency
```

A pull request should explain:

- the user-facing or safety outcome;
- affected tools and configuration;
- threat-model impact;
- tests actually run;
- live checks not run;
- compatibility or migration notes.

Do not mark a change ready when the full required gate is red.

## Roadmap issue pickup

When instructed to pick or continue RepoForge roadmap work:

1. read the [master roadmap](docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md) and canonical
   [execution program](https://github.com/maemreyo/repoforge/issues/3);
2. select only an implementation ticket whose canonical status is `Ready` and whose blockers are
   closed;
3. read the parent initiative, ticket body, and all specification or dependency-update comments;
4. inspect current `main`, recent commits, affected contracts, implementation, and tests before
   editing;
5. treat executable tests, safety policy, typed contracts, and current repository behavior as higher
   authority than roadmap prose or stale issue-title text.

If issue metadata and live dependency state disagree, report the drift and follow the derived
readiness rules tracked by issue #68 rather than guessing.

## Definition of done

Before presenting a RepoForge change as complete:

- the requested behavior is implemented without broadening permissions unintentionally;
- relevant positive, negative, failure-path, and stale-state tests exist;
- `./scripts/test-all.sh` passes;
- MCP tool count, schemas, annotations, and documentation agree;
- golden prompts were rerun when metadata changed;
- the Work Frontier config remains valid when relevant;
- generated distributions build successfully;
- `git diff` contains only intended changes;
- the final report lists commands actually run and anything not validated live.
