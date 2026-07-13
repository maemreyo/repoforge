# RepoForge

**Safe local Git workspaces and draft pull requests for ChatGPT.**

RepoForge is a local [Model Context Protocol](https://modelcontextprotocol.io/) server that gives
ChatGPT controlled access to allowlisted Git repositories. It creates isolated worktrees, applies
bounded code changes, runs repository-defined verification profiles, pushes `ai/*` branches without
force, and opens draft pull requests through the GitHub CLI.

RepoForge is deliberately **not** a general-purpose terminal or filesystem bridge. It does not
provide tools for arbitrary shell execution, protected-branch writes, force-pushes, pull-request
merges, secret management, repository administration, or GitHub Actions workflow changes.

> **Project status:** Beta. RepoForge is designed as a personal developer tool for repositories and
> machines you control. Review every diff and keep ChatGPT write confirmations enabled.

## Why RepoForge

ChatGPT web cannot directly access a local checkout, run its test suite, or use an existing `gh`
session. RepoForge provides a constrained bridge with explicit safety boundaries:

- repositories are configured by a short, model-facing ID;
- every task runs in an isolated Git worktree;
- writable branches must match a configured prefix, normally `ai/`;
- file access is restricted by canonical path checks and deny patterns;
- model-visible commands come only from predefined TOML profiles;
- verification receipts are bound to the exact workspace fingerprint;
- pushes are non-force and pull requests are always created as drafts;
- local activity is recorded in a JSONL audit log without storing file bodies, patches, or secrets.

See [SECURITY.md](SECURITY.md) for the complete threat model and limitations.

## Architecture

```text
ChatGPT web
    |
    | MCP tool calls
    v
OpenAI Secure MCP Tunnel
    |
    | local stdio process
    v
RepoForge
    |-- allowlisted repositories
    |-- isolated Git worktrees
    |-- predefined verification profiles
    |-- git + GitHub CLI
    v
ai/* branch -> non-force push -> draft pull request
```

## Key capabilities

- Repository discovery and configuration generation with `rf init` and `rf inspect-repo`.
- Actionable environment diagnostics through `rf doctor`.
- A non-mutating repository/worktree smoke test through `rf smoke-test`.
- Twenty-seven focused MCP tools with separate read and write responsibilities.
- Optimistic file locking, workspace fingerprints, verification receipts, and change budgets.
- Bounded file reads, batch reads, literal search, exact replacement, unified patches, and path
  restoration.
- Draft pull-request creation and updates, plus compact CI status buckets.
- Reproducible Python environments through `uv.lock`.
- Unit, security, local Git integration, fake-`gh`, CLI, and in-memory MCP protocol tests.

## Requirements

- macOS or Linux;
- Python 3.10 or newer;
- Git;
- GitHub CLI (`gh`) authenticated for the repositories you intend to use;
- [`uv`](https://docs.astral.sh/uv/) for the recommended installation path;
- `tunnel-client` when connecting RepoForge to ChatGPT web through Secure MCP Tunnel;
- Node.js and `npx` only when using MCP Inspector.

## Installation

For normal use, install the CLI directly:

```bash
uv tool install git+https://github.com/maemreyo/repoforge.git
```

Contributors can instead clone the repository and run `uv sync --extra dev`.

Authenticate GitHub CLI:

```bash
gh auth login
gh auth setup-git
```

The installed commands are:

```text
repoforge
rf
repoforge-mcp
```

## Configure and start

Configure the tunnel and every local repository once:

```bash
rf setup \
  --tunnel-id tunnel_... \
  /absolute/path/to/repoforge \
  /absolute/path/to/work-frontier
```

RepoForge writes a minimal user config containing only the tunnel identifier and repository paths.
It generates the full safety policy and exact command allowlists into a reviewed lock under
`~/.local/state/repoforge/config-locks/`. Setup also runs diagnostics and a safe worktree smoke test.

After setup, start RepoForge with one command:

```bash
rf start
```

When `CONTROL_PLANE_API_KEY` is absent, `rf start` asks for it using a hidden terminal prompt. The key
is never written to configuration, logs, audit records, or shell history.

Manage repositories without editing TOML:

```bash
rf repo list
rf repo inspect /absolute/path/to/another-repository
rf repo add /absolute/path/to/another-repository --preview
rf repo add /absolute/path/to/another-repository --approve PROPOSAL_ID
rf repo remove repository-id
rf runtime status
rf runtime start
rf runtime stop
rf runtime restart
rf runtime reload
rf runtime logs --tail 100
rf config history
rf config rollback 3
```

Managed runtime changes auto-restart after a successful repository add or accepted refresh; failed
expansions roll back to the previous retained generation. Repository removals are restrictive and
never roll back removed access if activation fails. Foreground runtimes report the reviewed generation
and restart requirement; `rf runtime status` makes that comparison explicit.
`rf runtime start` manages its tunnel-client child as a local process group; `stop` and `restart`
only affect that identity-validated managed child. `rf start` remains the foreground compatibility
entry point.
`rf runtime logs` reads a bounded, redacted tail from the managed tunnel child only.
`rf runtime status` includes local health evidence for the managed tunnel and its MCP child; it does
not make network requests or expose tunnel credentials.
`rf runtime reload` is the Stage-A supervisor-managed reload: it performs the same controlled
process-group restart as `restart`, then starts the latest reviewed configuration generation.

Every accepted minimal configuration is retained as a paired source and resolved-lock snapshot.
`rf config history` lists complete retained generations; `rf config rollback N` validates and restores
the exact source/lock pair for generation `N`, then reports whether the running process needs restart.

`rf repo inspect` and `rf repo add --preview` never write configuration. `rf repo add --preview`
returns a deterministic proposal ID bound to the current config, repository path, ID, and detected
profiles; enrollment requires supplying that exact ID via `--approve`. If a `Makefile`,
`package.json`, `pyproject.toml`, or another command source changes, RepoForge fails
closed. Review the proposed allowlist diff and then accept it explicitly:

```bash
rf repo refresh
rf repo refresh --accept
```

Legacy full `[server]` and `[repositories.*]` configurations remain supported. Detailed setup,
security behavior, and troubleshooting are in [docs/CHATGPT_SETUP.md](docs/CHATGPT_SETUP.md).

## Recommended workflow

1. Inspect the configured repository with `repo_list`, `repo_status`, and `repo_context`.
2. Read relevant instructions, plans, issues, implementation files, and tests.
3. Create one isolated workspace for the approved task.
4. Make small, bounded changes with optimistic locking.
5. Review `workspace_diff` after each meaningful change.
6. Run narrow allowlisted profiles while iterating.
7. Run `workspace_verify` before commit.
8. Stop for human review of the final diff and verification receipt.
9. Commit the exact verified tree, push without force, and create a draft pull request.
10. Read CI status through `workspace_pr_checks`. RepoForge never marks a PR ready or merges it.

Example planning prompt:

```text
Use only RepoForge.

Repository ID: my-repository.

Inspect repository status, instructions, the relevant issue or plan, implementation, and tests.
Do not change anything yet. Return the proposed scope, affected files, risks, narrow tests, and final
verification profile, then stop for approval.
```

## Command-line reference

```text
rf setup              Configure tunnel and repositories in one step
rf start              Validate and start the secure tunnel
rf repo               List, add, remove, or refresh repositories
rf init               Generate a legacy full configuration
rf inspect-repo       Preview ecosystem, scripts, instructions, and profiles
rf doctor             Validate tools, auth, paths, remotes, and profiles
rf smoke-test         Exercise safe repository/worktree operations
rf show-config        Print the resolved configuration
rf list-workspaces    List registered local workspaces
rf remove-workspace   Remove a clean local worktree
rf audit              Read recent local audit events
rf tunnel-command     Print tunnel-client initialization commands
rf serve              Run the MCP server over stdio
```

Use `rf <command> --help` for complete options.

## Development

Run the full local quality gate:

```bash
uv sync --extra dev
./scripts/test-all.sh
```

Equivalent Make targets are available:

```bash
make lint
make typecheck
make test
make build
make check
```

The project enforces strict Mypy checks, Ruff, branch coverage of at least 80%, distribution builds,
security regressions, real local Git/worktree integration, deterministic fake-GitHub tests, and MCP
protocol tests.

## Documentation

- [ChatGPT and tunnel setup](docs/CHATGPT_SETUP.md)
- [Tool reference](docs/TOOL_REFERENCE.md)
- [Development guide](docs/DEVELOPMENT.md)
- [Testing strategy](docs/TESTING.md)
- [Full-flow test runbook](docs/FULL_FLOW_TESTING.md)
- [Starter prompts](docs/STARTER_PROMPTS.md)
- [Plugin regression cases](docs/PLUGIN_TEST_CASES.md)
- [Security model](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

RepoForge is distributed under the [MIT License](LICENSE).
