# Connect RepoForge to ChatGPT with Secure MCP Tunnel

This guide connects a local RepoForge process to ChatGPT web. RepoForge runs on your machine and uses
your existing local Git checkout and GitHub CLI authentication.

## 1. Install RepoForge

Clone the repository and synchronize the locked environment:

```bash
git clone https://github.com/maemreyo/repoforge.git
cd repoforge
uv sync --extra dev
```

Authenticate GitHub CLI:

```bash
gh auth login
gh auth setup-git
```

## 2. Configure repositories

Generate a configuration for one repository:

```bash
uv run rf   --config "$HOME/.config/repoforge/config.toml"   init   --repo /absolute/path/to/repository   --repo-id my-repository
```

Alternatively, review and install one of the tracked examples:

```bash
mkdir -p "$HOME/.config/repoforge"
cp config.local-dev.toml "$HOME/.config/repoforge/config.toml"
```

Tracked examples contain maintainer-specific absolute paths. Update them before use.

## 3. Validate local operation

Run diagnostics:

```bash
uv run rf --config "$HOME/.config/repoforge/config.toml" doctor --fix
```

Run a non-mutating smoke test:

```bash
uv run rf   --config "$HOME/.config/repoforge/config.toml"   smoke-test   --repo-id my-repository
```

The smoke test creates and removes a temporary local worktree and branch. It does not edit files,
push a branch, or create a pull request.

Inspect the MCP tool surface before connecting ChatGPT:

```bash
./scripts/inspect-mcp.sh
```

MCP Inspector should discover twenty-seven tools. Confirm that no arbitrary-shell, merge, force-push,
protected-branch write, secret-management, or workflow-editing tool exists.

## 4. Install and start Secure MCP Tunnel

Download `tunnel-client` from the OpenAI Platform tunnel settings and place it on `PATH`.

Preview the tunnel commands RepoForge will use:

```bash
uv run rf   --config "$HOME/.config/repoforge/config.toml"   tunnel-command   --tunnel-id tunnel_...
```

Set the runtime credentials in a dedicated terminal:

```bash
export CONTROL_PLANE_API_KEY="sk-..."
export TUNNEL_ID="tunnel_..."
```

Do not place the runtime key in this repository, shell history, screenshots, issue reports, or chat
messages.

Start the tunnel:

```bash
./scripts/run-tunnel.sh
```

The script runs RepoForge diagnostics, initializes the tunnel profile, runs
`tunnel-client doctor --explain`, and keeps the tunnel process active. Leave this terminal open while
ChatGPT scans or invokes tools.

## 5. Create the ChatGPT Plugin

In ChatGPT developer-mode Plugin settings, create:

```text
Icon: plugin-icon.png
Name: RepoForge
Description: Safely inspect and modify allowlisted local Git repositories in isolated worktrees,
run predefined verification profiles, push AI branches, and create draft pull requests.
Connection: Tunnel
Available tunnel: select your RepoForge tunnel
Authentication: No Authentication
```

RepoForge does not connect ChatGPT directly to `api.githubcopilot.com`. The local process calls the
already-authenticated `gh` executable on your machine, so the Plugin does not depend on GitHub
Copilot OAuth or Dynamic Client Registration.

## 6. Run read-only discovery tests

Open a new conversation with only RepoForge enabled:

```text
Use only RepoForge.

Repository ID: my-repository.

Do not create a workspace or modify anything. Inspect repository status, repository context, recent
commits, instruction files, and configured verification profiles. Report every RepoForge tool called
and whether any write confirmation appeared.
```

Expected result:

- RepoForge is selected;
- only repository read tools run;
- no workspace or branch is created;
- no write confirmation appears.

Run an indirect discovery prompt in a separate conversation:

```text
Check whether my configured local repository is ready for a safe coding task. Do not modify
anything. Explain the fast and full validation profiles and state which Plugin and tools were used.
```

Run a negative prompt in another conversation:

```text
What is the current weather in Hanoi?
```

RepoForge should not be selected for the negative case.

## 7. First controlled write

After read-only discovery passes, use a small documentation-only canary in an isolated workspace.
Review the complete diff before verification, review it again before commit, and create only a draft
pull request.

Follow [FULL_FLOW_TESTING.md](FULL_FLOW_TESTING.md) and record results with
[TEST_RUN_RECORD.md](TEST_RUN_RECORD.md).

## Troubleshooting

### Inspector reports that a TOML file is invalid JSON

Ensure the Inspector command separates Inspector arguments from MCP server arguments:

```bash
npx -y @modelcontextprotocol/inspector@latest --   "$PWD/.venv/bin/repoforge"   --config "$HOME/.config/repoforge/config.toml"   serve
```

The `--` delimiter is required.

### The tunnel is not visible in ChatGPT

Confirm that:

- `tunnel-client run` is still active;
- the tunnel belongs to the same OpenAI organization and ChatGPT workspace;
- the runtime key has permission to read and use the tunnel;
- `tunnel-client doctor --profile repoforge --explain` reports a healthy connection.

### GitHub operations fail

Run:

```bash
gh auth status
gh auth setup-git
```

Verify that the configured remote exists and that the authenticated account can push branches and
create pull requests.

### RepoForge refuses to commit

Refresh workspace status and inspect the diff. A commit is rejected when the workspace changed after
verification, the verification receipt is missing, a denied path changed, or the configured change
budget was exceeded. Restore the intended tree and rerun verification.
