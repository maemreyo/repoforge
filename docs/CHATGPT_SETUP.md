# Connect RepoForge to ChatGPT with Secure MCP Tunnel

RepoForge now has a two-command happy path. The user config contains only the tunnel identifier and
local repositories; RepoForge generates a reviewed runtime lock with the full safety policy and
allowlisted verification commands.

## 1. Install RepoForge and authenticate GitHub

```bash
uv tool install git+https://github.com/maemreyo/repoforge.git
gh auth login
gh auth setup-git
```

Contributors can instead clone the repository and run `uv sync --extra dev`.

Download `tunnel-client` from the OpenAI Platform tunnel settings and place it on `PATH`.

## 2. Configure all repositories once

```bash
rf setup \
  --tunnel-id tunnel_... \
  /absolute/path/to/repoforge \
  /absolute/path/to/work-frontier
```

`rf setup` performs repository detection, creates the minimal config, generates the reviewed runtime
lock, runs diagnostics, and performs a safe worktree smoke test. Use `--skip-smoke` only when the
machine is temporarily offline or a remote cannot be fetched.

The generated user config is intentionally small:

```toml
version = 1

[tunnel]
id = "tunnel_..."

[[repo]]
id = "repoforge"
path = "/absolute/path/to/repoforge"

[[repo]]
id = "work-frontier"
path = "/absolute/path/to/work-frontier"
```

The generated resolved config lives under `~/.local/state/repoforge/config-locks/`. It contains the
secure defaults and exact command allowlists. Do not edit it directly.

## 3. Start RepoForge

```bash
rf start
```

When `CONTROL_PLANE_API_KEY` is not already set, `rf start` requests it with a hidden terminal prompt.
The key is passed only to `tunnel-client`; it is not written to TOML, logs, audit records, or shell
history.

`rf start` automatically:

1. validates that the user config and generated lock still match;
2. refuses to start when `Makefile`, `package.json`, `pyproject.toml`, or another command source changed;
3. runs essential RepoForge diagnostics;
4. initializes or repairs the tunnel profile only when needed; and
5. starts `tunnel-client run`.

The compatibility script now delegates to the same command:

```bash
./scripts/run-tunnel.sh
```

## Repository management

```bash
rf repo list
rf repo add /absolute/path/to/another-repository
rf repo remove repository-id
```

When a repository changes its verification command sources, review the proposed lock diff:

```bash
rf repo refresh
```

Nothing is written until the command changes have been reviewed and explicitly accepted:

```bash
rf repo refresh --accept
```

Existing full `[server]` and `[repositories.*]` configurations remain supported. With a legacy config,
start the tunnel using an explicit tunnel ID:

```bash
rf start --tunnel-id tunnel_...
```

## 4. Create the ChatGPT Plugin

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

Keep the `rf start` terminal open while ChatGPT scans or invokes tools.

## 5. Run read-only discovery tests

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

Then follow [FULL_FLOW_TESTING.md](FULL_FLOW_TESTING.md) for the first controlled write.

## Troubleshooting

### RepoForge reports that the resolved config is stale

Review and accept changes to detected commands:

```bash
rf repo refresh
rf repo refresh --accept
```

This fail-closed behavior prevents a changed project manifest or Makefile from silently expanding the
commands available to ChatGPT.

### The tunnel is not visible in ChatGPT

Run:

```bash
rf start --dry-run
```

Confirm that `tunnel-client` is installed, the tunnel belongs to the same OpenAI organization and
ChatGPT workspace, and the runtime key can use that tunnel.

### GitHub operations fail

```bash
gh auth status
gh auth setup-git
```

Verify that every configured remote exists and that the authenticated account can push branches and
create pull requests.

### RepoForge refuses to commit

Refresh workspace status and inspect the diff. A commit is rejected when the workspace changed after
verification, the verification receipt is missing, a denied path changed, or the configured change
budget was exceeded. Restore the intended tree and rerun verification.

## Guided multi-repository onboarding

Prefer the guided workflow over manually assembling JSON and approval-token loops:

```bash
rf onboard /Users/you/Documents/projects
```

RepoForge performs environment preflight, discovers real Git worktrees, excludes linked worktrees such as `.claude/worktrees/*`, detects an existing configuration, skips already enrolled paths, and reviews each proposal in the terminal. It accepts the selected batch as one immutable generation and performs at most one runtime activation.

For automation, first request a non-mutating plan:

```bash
rf onboard /Users/you/Documents/projects \
  --non-interactive \
  --tunnel-id tunnel_... \
  --activate never \
  --plan-only
```

Re-run with the exact decisions and `--approve approve:PROPOSAL_ID` values shown. Exit code `3` means operator input is still required and no unsafe capability was silently approved. Resume interrupted work with `rf onboard resume SESSION_ID`; inspect or cancel it with `status` and `cancel`.

When two repositories have the same directory name, resolve the conflict explicitly:

```bash
rf onboard /Users/you/Documents/projects \
  --repo-id /Users/you/Documents/projects/client/api=client-api \
  --repo-id /Users/you/Documents/projects/legacy/api=legacy-api
```
