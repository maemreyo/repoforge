# Getting started

Choose the narrowest path that reaches your first verified success.

## 1. Local-first MCP stdio

Use this when developing RepoForge, testing a repository policy, or connecting through MCP Inspector without provisioning a tunnel.

```bash
rf setup --local /absolute/path/to/repository
rf config path
rf serve
```

`rf setup` first returns reviewed repository proposals. Rerun it with every exact `approve:<proposal-id>` token it prints. Then launch MCP Inspector in another terminal:

```bash
./scripts/inspect-mcp.sh
```

Call `repo_list`, then one read-only repository tool. This verifies the accepted configuration, packaged MCP server, and repository access without `tunnel-client`, `CONTROL_PLANE_API_KEY`, or ChatGPT developer mode.

## 2. Connect ChatGPT through a managed tunnel

Use this after the local path works and remote ChatGPT access is required. Configure or upgrade the source configuration with a tunnel ID, install `tunnel-client`, export `CONTROL_PLANE_API_KEY`, and run:

```bash
rf start
rf runtime status
```

See [CHATGPT_SETUP.md](CHATGPT_SETUP.md) for the full tunnel flow and credential boundaries.

## 3. Guided multi-repository onboarding

Use `rf onboard ROOT` when discovering several repositories or reviewing repository-specific decisions interactively. Standard installs include the Rich and InquirerPy UI. Non-TTY runs and `--ui plain` retain the plain deterministic flow.

```bash
rf onboard /absolute/search/root --plan-only
```

Use `rf setup` for a small known repository set and `rf onboard` for discovery, per-repository review, or resumable onboarding sessions.

## Where files are stored

Run `rf config path` at any time, including before configuration exists. It reports the editable source config, generated lock directory, state root, onboarding sessions, audit log, runtime log, metrics, and diagnostics paths with existence status.
