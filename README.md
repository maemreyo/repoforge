<div align="center">

<img src="plugin-icon.png" alt="RepoForge logo" width="180" />

# RepoForge

### The control plane for agentic software engineering

**Safe execution. Exact evidence. Human control.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-beta-orange)](#project-status)
[![MCP](https://img.shields.io/badge/Model%20Context%20Protocol-compatible-6E56CF)](https://modelcontextprotocol.io/)

[Getting Started](#getting-started) ·
[How It Works](#how-it-works) ·
[Security](#security-model) ·
[Documentation](#documentation) ·
[Roadmap](#roadmap)

</div>

---

## What is RepoForge?

RepoForge is a local [Model Context Protocol](https://modelcontextprotocol.io/) server that gives AI agents controlled access to allowlisted Git repositories.

It sits between an AI client and your development environment, enforcing a safe, reviewable workflow for repository inspection, isolated code changes, verification, Git operations, and draft pull-request publication.

RepoForge is designed around a simple principle:

> An AI-generated change is not ready merely because the code looks plausible.  
> It is ready when the exact source tree has current, reproducible evidence that explains what changed, what may be affected, how it was verified, and which actions were authorized.

Today, RepoForge can:

- inspect explicitly allowlisted repositories;
- create isolated Git worktrees on controlled `ai/*` branches;
- read and modify files within canonical path and change-budget policies;
- run repository-defined, allowlisted command profiles;
- bind verification receipts to the exact workspace tree;
- commit only the verified tree;
- push without force;
- create and update draft pull requests;
- inspect pull-request and CI state;
- validate a deterministic issue dependency graph and select the next Ready work;
- attach explainable risk and ordered verification recommendations to one assessment snapshot;
- reuse private atomic durable-state primitives across operational records;
- manage local configuration generations and runtime lifecycle;
- record bounded, secret-safe audit metadata.

RepoForge is deliberately **not** a general-purpose shell, unrestricted filesystem bridge, merge bot, secret manager, or CI bypass mechanism.

---

## Why RepoForge?

AI agents are increasingly capable of understanding and changing large codebases. The difficult part is no longer only code generation. The difficult part is controlling the entire engineering workflow around it:

- Which repository may the agent access?
- Which files may it change?
- Is the workspace still based on the expected source state?
- Which commands are approved?
- Did the source tree change after verification?
- Is the evidence still current?
- Can the operation be resumed safely after interruption?
- What was pushed, and what remains incomplete?
- Which decisions still require a human?

RepoForge provides a constrained engineering control plane that makes those questions explicit.

### Core promises

1. **Local-first control**  
   Source code and credentials remain under the operator's control.

2. **Least privilege**  
   Repositories, branches, paths, commands, and external writes are explicitly constrained.

3. **Exact-state decisions**  
   Writes, plans, and verification are bound to exact Git and workspace state.

4. **Evidence before confidence**  
   Model confidence cannot replace tests, policy, or current source evidence.

5. **Human authority**  
   Capability expansion and high-impact decisions remain reviewable by a human.

---

## How It Works

```text
Human intent
    │
    ▼
AI client / ChatGPT
    │  MCP tool calls
    ▼
RepoForge
    ├── repository allowlist
    ├── path and branch policy
    ├── isolated Git worktrees
    ├── optimistic locking
    ├── bounded change budgets
    ├── approved execution profiles
    ├── exact-tree verification receipts
    ├── audit and runtime state
    └── GitHub publication controls
    │
    ▼
ai/* branch
    │
    ├── non-force push
    └── draft pull request
```

A typical task follows this flow:

1. Inspect repository context and project instructions.
2. Read the relevant issue, plan, source files, and tests.
3. Create an isolated workspace from an allowlisted base.
4. Make bounded changes using optimistic locking.
5. Review the exact diff and change-budget metrics.
6. Run narrow approved checks while iterating.
7. Run the repository's authoritative verification profile.
8. Commit the exact verified tree.
9. Push the controlled branch without force.
10. Create or update a draft pull request.
11. Observe CI and return the exact resulting state.

---

## Safety Model

RepoForge treats safety boundaries as product behavior, not optional guidance.

### Enforced invariants

- Repository access is configured by a short allowlisted `repo_id`.
- Model-provided absolute repository paths are not accepted by MCP tools.
- File paths are canonicalized and cannot escape the repository or worktree.
- Protected branches cannot be modified.
- Writable branches must use the configured prefix, normally `ai/`.
- Every task uses an isolated Git worktree.
- Secret-bearing and protected paths are denied by policy.
- Symlinks, submodules, and gitlinks cannot bypass path restrictions.
- Writes require current file hashes or a current workspace fingerprint.
- Any source mutation invalidates prior verification evidence.
- Commit may require a receipt for the exact current tree.
- Push never uses force.
- Pull requests are created as drafts.
- Audit records exclude source bodies, patches, secrets, and full process environments.
- MCP stdio mode reserves stdout for protocol messages.

### Deliberately unsupported

RepoForge does not expose tools for:

- arbitrary shell execution;
- unrestricted filesystem access;
- direct writes to protected branches;
- force-pushing;
- merging pull requests;
- enabling auto-merge;
- managing repository secrets;
- modifying branch protection;
- repository administration;
- release creation;
- editing GitHub Actions workflows.

These omissions are part of the security model.

See [SECURITY.md](SECURITY.md) for the detailed threat model and limitations.

---

## Key Capabilities

### Repository inspection

- List configured repositories and policies.
- Inspect Git status, remotes, branch state, manifests, scripts, and project instructions.
- Read recent commits, GitHub issues, pull requests, reviews, and checks.

### Isolated workspace lifecycle

- Create one managed worktree per task.
- Track workspace identity, branch, base, HEAD, fingerprint, and change metrics.
- Resume or remove clean local workspaces safely.

### Controlled file operations

- Bounded UTF-8 file reads.
- Batched reads with configured limits.
- Literal repository search.
- Exact text replacement with optimistic locking.
- Validated unified patches.
- Path restoration.
- Exact diff review and change-budget enforcement.

### Verification and publication

- Execute explicitly named repository profiles.
- Store verification receipts for the exact resulting tree.
- Commit only after the configured gate succeeds.
- Push without force.
- Create and update draft pull requests.
- Read mergeability and compact CI status.

### Configuration and runtime operations

- Guided repository onboarding.
- Reviewed configuration generations.
- Atomic activation and rollback behavior.
- Managed tunnel lifecycle.
- Runtime status, logs, diagnostics, restart, and reload.
- Bounded and redacted operational metadata.

---

## Getting Started

### Requirements

- macOS or Linux
- Python 3.10 or newer
- Git
- GitHub CLI (`gh`)
- [`uv`](https://docs.astral.sh/uv/)
- `tunnel-client` when connecting RepoForge to ChatGPT through a secure MCP tunnel
- Node.js and `npx` only for MCP Inspector workflows

### Install

```bash
uv tool install git+https://github.com/maemreyo/repoforge.git
```

For development:

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

Installed commands:

```text
repoforge
repoforge-mcp
rf
```

### Environment overrides

| Variable | Purpose |
| --- | --- |
| `REPOFORGE_CONFIG` | Override the editable source configuration path. |
| `REPOFORGE_OUTPUT` | Select supported CLI output rendering such as JSON. |
| `REPOFORGE_TUNNEL_ID` | Supply a managed tunnel ID for legacy/runtime compatibility paths. |
| `REPOFORGE_TUNNEL_PROFILE` | Override the managed tunnel profile name. |
| `CONTROL_PLANE_API_KEY` | Authorize managed tunnel startup; never persisted by RepoForge. |

### Where RepoForge keeps files

Run `rf config path` before or after setup to see the absolute source config, generated lock directory, state root, onboarding store, audit and metrics files, runtime log, and diagnostics directory. Each entry reports whether it currently exists. See the [getting-started path chooser](docs/getting-started/README.md) for local-first and tunnel flows.

### Configure

Start with local-only stdio operation when no tunnel is required:

```bash
rf setup --local /absolute/path/to/repository
```

Review the proposal and rerun with every exact `approve:<proposal-id>` token shown by RepoForge. To configure managed tunnel operation instead:

```bash
rf setup \
  --tunnel-id tunnel_... \
  /absolute/path/to/repository
```

RepoForge stores minimal user intent and generates a reviewed configuration lock containing the resolved repository policy and approved execution profiles. Inspect every resolved location without creating files:

```bash
rf config path
```

### Start

For local MCP stdio:

```bash
rf serve
```

For the managed runtime and tunnel lifecycle:

```bash
rf start
```

Managed startup requires a configured tunnel ID, the `tunnel-client` executable, and `CONTROL_PLANE_API_KEY`. The key is not written to configuration, audit records, runtime logs, or shell history.

### Manage repositories

```bash
rf repo list
rf repo inspect /absolute/path/to/repository

rf repo propose /absolute/path/to/repository
rf repo add /absolute/path/to/repository --approve approve:PROPOSAL_ID

rf repo refresh
rf repo refresh --accept

rf repo remove repository-id
```

Repository inspection and preview operations do not execute discovered commands. Capability expansion requires explicit approval of the current proposal.

### Manage the runtime

```bash
rf runtime status
rf runtime start
rf runtime stop
rf runtime restart
rf runtime reload
rf runtime logs --tail 100

rf config history
rf config rollback 3
rf config get repositories.demo.max_diff_lines
rf config set repositories.demo.max_diff_lines 5000
rf config edit
rf show-config --origin

rf diagnostics bundle
```

`rf config get/set` address one resolved policy value with a dotted key, `repositories.<repo_id>.<field>`, for `max_changed_files`, `max_diff_lines`, `max_total_changed_bytes`, and `read_only`. `set` writes the change through the same reviewed proposal-and-generation pipeline as `rf repo refresh`, so it prints `pending_approval`/`input_required` and an approval token exactly like refresh when the change needs one. `rf config edit` opens the small hand-authored source file (tunnel and repository list) in `$EDITOR`/`$VISUAL`, validates it on save, and reports whether the accepted generation is now stale; it never touches the generated lock. `rf show-config --origin` annotates each of those four fields per repository with `file` (explicit override), `preset:<name>`, or `default`.

---

## Recommended Agent Workflow

A strong prompt for an AI agent is:

```text
Use RepoForge only.

Inspect the repository status, instructions, relevant issue or plan,
implementation, and tests before making changes.

Return:
1. exact task interpretation;
2. affected modules and contracts;
3. expected files;
4. risks and non-goals;
5. narrow tests to run while iterating;
6. final verification profile;
7. any reason the task should be split.

Stop for approval before editing.
```

After approval:

```text
Create an isolated workspace, implement the approved task using small
test-driven changes, review the final diff, run the full verification
profile, commit the exact verified tree, push without force, and create
a draft pull request. Do not merge it.
```

---

## Development

Set up the environment:

```bash
uv sync --extra dev
```

Run individual gates:

```bash
make tickets
make lint
make typecheck
make test
make build
```

`make tickets` validates `docs/roadmaps/REPOFORGE_TICKET_GRAPH.json` and prints the next deterministic Ready tickets. Add `--live-repo owner/name` to `scripts/validate_ticket_graph.py` for an optional bounded, read-only GitHub drift check.

Run the standard local gate:

```bash
make check
```

Run the production release gate:

```bash
scripts/verify-production.sh
```

During development on a dirty tree:

```bash
scripts/verify-production.sh --allow-dirty
```

The production gate validates ticket governance and release contracts, then checks formatting, linting, strict typing, tests, branch coverage, clean package builds, and installed-wheel behavior. See [the integrity policy](docs/development/INTEGRITY_POLICY.md) for ordering, scope, generated artifacts, symlinks, line endings, and failure semantics.

### Project structure

```text
src/repoforge/
├── domain/          Core models, invariants, and policy concepts
├── application/     Use cases and orchestration
├── ports/           Abstract boundaries
├── adapters/        Git, GitHub, filesystem, persistence, runtime, and execution
├── interfaces/      MCP, CLI, and runtime entry points
└── testing/         Shared test doubles and fixtures

docs/
├── architecture/
├── contracts/
├── development/
├── getting-started/
├── guides/
├── plans/
├── roadmaps/
├── superpowers/
└── testing/
```

RepoForge follows dependency inversion:

```text
Interfaces
    ↓
Application use cases
    ↓
Domain contracts
    ↑
Ports and infrastructure adapters
```

Policy decisions belong in the domain and application layers, not in MCP handlers, CLI rendering, or UI components.

---

## Documentation

- [Documentation index](docs/README.md)
- [ChatGPT setup guide](docs/getting-started/CHATGPT_SETUP.md)
- [Interactive onboarding](docs/getting-started/INTERACTIVE_ONBOARDING.md)
- [Development guide](docs/development/DEVELOPMENT.md)
- [Ticket governance](docs/development/TICKET_GOVERNANCE.md)
- [Source and release integrity policy](docs/development/INTEGRITY_POLICY.md)
- [MCP tool reference](docs/development/TOOL_REFERENCE.md)
- [Testing strategy](docs/testing/TESTING.md)
- [Full-flow testing](docs/testing/FULL_FLOW_TESTING.md)
- [Security model](SECURITY.md)
- [Master roadmap](docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md)
- [Issue-driven execution program](https://github.com/maemreyo/repoforge/issues/3)

---

## Roadmap

RepoForge is evolving from a safe local Git bridge into an evidence-driven engineering control plane for humans and AI agents.

The long-term architecture is organized around:

1. **Agent Control Plane**
   - durable task capsules;
   - resumable operations;
   - immutable execution plans;
   - workspace leases;
   - structured next actions.

2. **Unified Evidence**
   - snapshot-consistent workspace assessment;
   - impact and affected-test intelligence;
   - architecture drift detection;
   - explainable risk;
   - adaptive verification.

3. **ChatGPT-native UX**
   - MCP Apps dashboards;
   - progress and cancellation;
   - review and approval interfaces;
   - capability-aware elicitation.

4. **Reproducible Execution**
   - environment identity;
   - verification DAGs;
   - safe caches;
   - structured failure intelligence;
   - optional isolated execution adapters.

5. **Security and Trust**
   - analyzer integrations;
   - secret-safe egress;
   - explicit capability policy;
   - workload identity;
   - verification attestations.

6. **Scale**
   - behavioral agent evaluations;
   - record and replay;
   - OpenTelemetry-compatible traces;
   - multi-repository task bundles;
   - optional team, remote, and A2A adapters.

The roadmap preserves the current safety model: new intelligence may recommend, explain, or broaden verification, but it may not silently expand authority.

Delivery status is tracked by the [program issue](https://github.com/maemreyo/repoforge/issues/3) and its dependency graph. Select only tickets whose canonical status is `Ready` and whose blockers are closed; roadmap prose does not override executable policy, tests, or current issue state.

See the [RepoForge Master Roadmap](docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md) and the [program issue](https://github.com/maemreyo/repoforge/issues/3).

---

## Vision

> A future where humans and AI agents can build software at high speed while every change remains safe, explainable, verifiable, and under human control.

## Mission

> RepoForge turns engineering intent into evidence-backed software changes by orchestrating tasks, constraining capabilities, isolating execution, assessing impact, and verifying the exact source state before publication.

---

## Project Status

RepoForge is currently **Beta**.

It is intended primarily as a local, personal developer tool for repositories and machines you control. Review every diff, preserve client-side confirmation for write operations, and do not treat the current release as a multi-tenant security boundary.

Compatibility and behavior may evolve while the task, evidence, execution, and UI control-plane roadmap is implemented.

---

## Contributing

Contributions should preserve RepoForge's safety invariants and architectural boundaries.

Before opening a pull request:

1. Read [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
2. Keep the change scoped to one independently reviewable concern.
3. Add positive, negative, stale-state, and failure-path coverage where applicable.
4. Run the production verification gate.
5. Document public contract changes explicitly.
6. Open a draft pull request for review.

Please do not propose arbitrary shell access, force push, automatic merge, secret exposure, or policy bypasses as convenience features.

---

## License

RepoForge is available under the [MIT License](LICENSE).

---

<div align="center">

**RepoForge — The control plane for agentic software engineering.**

Safe execution · Exact evidence · Human control

</div>
