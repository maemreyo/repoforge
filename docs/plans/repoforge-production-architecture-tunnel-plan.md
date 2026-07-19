# RepoForge Production Architecture, Smart Repository Onboarding, and Tunnel Lifecycle Plan

Status: Implemented — Phases 0–8 complete; required production gate must pass before release

Repository reviewed: `maemreyo/repoforge`

Original baseline commit: `984680f32db22a3828ab4740fcde1a753ba4c17e`

Phase 6 implementation baseline: `9c98ceb350b7d8dc6cad033d7d0bf9d9059be4a1`

Phase 7 implementation baseline: `ada5d6fca145f66a690cbe851268a65cd127cc76`

Date: 2026-07-13

## 1. Executive summary

RepoForge already has strong safety fundamentals: allowlisted repositories, isolated Git worktrees,
optimistic file locking, verification receipts, protected branches and paths, non-force pushes,
draft-only PR creation, a fake `gh` integration harness, and MCP protocol contract tests.

The main architectural constraint is that `CodingService` still owns repository discovery,
filesystem access, Git and GitHub commands, workspace lifecycle, verification, publishing,
diagnostics, audit orchestration, and response shaping. Configuration and tunnel onboarding have
also grown into large modules that directly execute subprocesses and mutate files.

Tunnel lifecycle is currently only partially managed:

- `rf start` validates the resolved lock, runs diagnostics, initializes or repairs the tunnel
  profile, and replaces itself with `tunnel-client run`.
- `rf repo add`, `rf repo remove`, and `rf repo refresh --accept` update configuration files on
  disk, but do not notify or reload the running MCP server.
- The MCP server loads configuration once at process startup. A running server therefore does not
  see a newly added, removed, or refreshed repository.
- The tunnel profile fingerprint covers tunnel ID, profile, MCP command, and runtime config path,
  but not the contents or generation of the resolved config. Adding a repository can therefore
  require restarting RepoForge even when tunnel profile reinitialization is unnecessary.
- There is no supervisor, graceful reload, restart command, active-process registry, drain protocol,
  or machine-readable `restart_required` result.
- Repository scanning generates profiles and warnings but does not run an interactive or structured
  follow-up workflow for ambiguous choices, policy review, smoke-test remediation, or activation.

The recommended implementation is incremental. Keep the current MCP schemas and `CodingService` as
a compatibility facade, establish ports and deterministic harnesses first, then migrate one vertical
slice at a time. Tunnel management should become an explicit application subsystem, not remain a
collection of CLI subprocess calls.

## 2. Current behavior and operator impact

### 2.1 What happens today when adding a repository

Current command:

```bash
rf repo add /absolute/path/to/repository
```

Current behavior:

1. Load the minimal user config.
2. Detect repository metadata and generated verification profiles.
3. Append the repository to the minimal config.
4. Regenerate and atomically replace the resolved runtime lock.
5. Print detected profiles.
6. Exit without checking whether RepoForge or the tunnel is currently running.

If `rf start` is already running, the new repository is **not active** in that process. The operator
must stop the current foreground `tunnel-client run` process and run `rf start` again.

The tunnel itself normally does not need a new tunnel ID or a new ChatGPT Plugin. The local MCP child
process must be recreated with the new resolved configuration. `rf start` may skip tunnel profile
initialization when its existing profile fingerprint is unchanged, then run tunnel doctor and start
the tunnel again.

### 2.2 What happens after repository command sources change

RepoForge fingerprints relevant files such as `Makefile`, `package.json`, lockfiles,
`pyproject.toml`, `Cargo.toml`, and `go.mod`. When those sources change:

```bash
rf repo refresh
rf repo refresh --accept
```

The first command previews a unified diff. The second writes the new resolved lock. This is a good
fail-closed security design. However, the accepted lock is still not applied to an already-running
MCP server; a restart is required but is not reported or orchestrated.

### 2.3 What happens when removing a repository

`rf repo remove <repo-id>` updates the user config and resolved lock. A running MCP process retains
the removed repository in memory until restart. This is a security-relevant revocation delay and
must be treated more strictly than adding a repository.

### 2.4 Current answer to “what must be restarted?”

| Change | RepoForge MCP process | `tunnel-client run` | Tunnel profile `init` | ChatGPT Plugin |
| --- | --- | --- | --- | --- |
| Add/remove repo | Restart required | Current foreground process must be restarted | Usually no | No |
| Accept changed profiles/policy | Restart required | Restart current run | Usually no | No |
| Change config path or MCP command | Restart required | Restart | Yes | No |
| Change tunnel ID/profile | Restart required | Restart | Yes | Select/configure tunnel only if ID changed |
| Change tool names/schemas | Restart required | Restart | Possibly no | Plugin/tool rediscovery may be required |
| Change RepoForge executable/version | Restart required | Restart | Re-init if MCP command changed | Rediscovery only if tool surface changed |

This table describes current operational reality, not the desired final UX.

## 3. Target user experience

### 3.1 Happy path: add and activate a repository

```bash
rf repo add /path/to/repo
```

Target flow:

1. Validate path and Git repository identity.
2. Detect ecosystem, package manager, default branch, remote, instruction files, and candidate
   commands.
3. Classify confidence and risks.
4. Ask or return structured follow-up questions only for ambiguous or security-relevant decisions.
5. Render a reviewable policy/profile proposal.
6. Require explicit approval before expanding executable command capability.
7. Atomically write minimal config and a versioned resolved snapshot.
8. Run doctor and a bounded isolated-worktree smoke test for the new repository.
9. Activate the new snapshot using graceful MCP reload when supported, otherwise supervisor-managed
   restart.
10. Confirm the active generation, repository ID, profiles, tunnel health, and safe next action.

Example result:

```json
{
  "status": "active",
  "repo_id": "work-frontier",
  "config_generation": 12,
  "profiles": ["quick", "test", "preflight", "full"],
  "smoke_test": "passed",
  "activation": "graceful_restart",
  "tunnel": "healthy",
  "next_action": "Open ChatGPT and call repo_list."
}
```

### 3.2 Safe preview path

All capability-expanding changes support preview:

```bash
rf repo propose /path/to/repo
rf repo refresh
```

Preview output must distinguish:

- unchanged settings;
- auto-safe changes;
- capability-expanding changes requiring approval;
- unsupported or ambiguous items requiring user input;
- activation impact;
- whether existing sessions or workspaces are affected.

### 3.3 Non-interactive path

Automation must never hang waiting for prompts:

```bash
rf repo add /path/to/repo --non-interactive --approve approve:PROPOSAL_ID
```

When decisions are missing, return a structured `INPUT_REQUIRED` error with choices and no writes.

## 4. Target architecture

```text
interfaces/
  cli/                  argument parsing and terminal rendering
  mcp/                  MCP schemas, annotations, and response rendering
          |
          v
application/
  repository/           inspect, propose, enroll, refresh, remove
  workspace/            create, read, modify, verify, commit
  publishing/           push, create/update/read draft PR
  runtime/              status, activate, reload, restart, stop
          |
          v
domain/
  repository_policy.py
  repository_detection.py
  config_generation.py
  workspace.py
  verification.py
  publishing.py
  runtime.py
  errors.py
          ^
          |
ports/
  git.py
  github.py
  filesystem.py
  process.py
  configuration.py
  state.py
  locking.py
  clock.py
  runtime_control.py
          ^
          |
adapters/
  git_cli/
  github_cli/
  local_filesystem/
  subprocess/
  json_state/
  fcntl_locking/
  tunnel_client/
          |
          v
bootstrap.py             composition root
```

Dependency rules:

- Domain imports only standard-library types and other domain modules.
- Application imports domain and port protocols, never concrete adapters.
- Adapters implement ports and may import vendor/process/filesystem details.
- Interfaces call application use cases and map stable DTOs to CLI/MCP responses.
- `bootstrap.py` is the only place that constructs concrete adapters.
- Existing `CodingService` delegates to use cases until all callers are migrated.

## 5. Core contracts

### 5.1 Repository proposal

```python
@dataclass(frozen=True)
class RepositoryProposal:
    repository: RepositoryIdentity
    detected: DetectedRepositoryFacts
    policy: RepositoryPolicy
    profiles: tuple[VerificationProfile, ...]
    findings: tuple[DetectionFinding, ...]
    decisions: tuple[RequiredDecision, ...]
    capability_delta: CapabilityDelta
    fingerprint: str
```

### 5.2 Configuration generation

Every accepted configuration becomes an immutable generation:

```python
@dataclass(frozen=True)
class ConfigGeneration:
    generation: int
    source_sha256: str
    resolved_sha256: str
    repository_fingerprints: Mapping[str, str]
    created_at: datetime
    reason: str
```

The active runtime records its loaded generation. Disk state and active state can then be compared
without guessing.

### 5.3 Runtime status

```python
@dataclass(frozen=True)
class RuntimeStatus:
    state: Literal["stopped", "starting", "healthy", "degraded", "reloading", "stopping"]
    pid: int | None
    tunnel_profile: str
    tunnel_id_fingerprint: str
    active_generation: int | None
    disk_generation: int
    restart_required: bool
    health: tuple[HealthCheck, ...]
```

### 5.4 Stable result envelope

Every write operation should provide:

```json
{
  "status": "active | pending_approval | restart_required | failed",
  "what_happened": "...",
  "unchanged_state": ["..."],
  "safe_next_action": "...",
  "retryable": false,
  "correlation_id": "...",
  "config_generation": 12,
  "active_generation": 11
}
```

## 6. Smart repository scanning and follow-ups

### 6.1 Detection sources

Scanning should collect facts, not silently grant capability:

- Git root, common directory, remote names/URLs, default branch, detached state, shallow clone.
- Ecosystem manifests and lockfiles.
- Declared toolchain versions.
- Make targets and package scripts.
- Workspace/monorepo structure.
- Existing CI commands and required checks when locally available.
- Instruction files such as `AGENTS.md`, `CONTRIBUTING.md`, and architecture documentation.
- Existing test, lint, typecheck, build, preflight, and security commands.
- Protected/sensitive paths and repository-specific overrides.
- Submodules, symlinks, LFS, generated files, large files, and binary-heavy repositories.
- Existing RepoForge metadata or policy files.

### 6.2 Confidence levels

Each proposed profile carries a confidence level:

- `high`: explicitly declared standard command with compatible lockfile/toolchain.
- `medium`: inferred from a conventional script or Make target.
- `low`: heuristic or conflicting declarations.
- `blocked`: unsafe, free-form, interactive, network-sensitive, destructive, or unresolved.

Only high-confidence, non-expanding metadata can be accepted automatically. Executable command
profiles always remain reviewable.

### 6.3 Required follow-up decisions

Ask only when the answer changes behavior materially:

1. Multiple remotes: which remote is publishable?
2. Ambiguous base branch: which branches are allowed as bases?
3. Multiple package managers/lockfiles: which toolchain is authoritative?
4. Monorepo: root-wide verification or scoped package profiles?
5. Missing full verification command: compose one from detected commands or require manual config?
6. Commands with `--fix`, deploy, release, database, network, or destructive semantics: exclude or
   explicitly allow as non-verification actions?
7. Existing worktrees/submodules/LFS: enable supported constrained behavior or block enrollment?
8. Missing `gh` authentication or remote push access: enroll read-only or stop?
9. Large repository exceeding default budgets: keep defaults, choose scoped paths, or request an
   explicit budget override?
10. Existing custom policy: preserve, merge, or replace?

Follow-up answers are transformed into an explicit proposal; they are never applied directly to the
running service without review and acceptance.

### 6.4 Capability delta classification

Changes are classified as:

- `metadata_only`: display name or non-security description.
- `restriction`: removes a repository, command, allowed branch, path, or budget.
- `equivalent`: formatting/order changes without semantic delta.
- `expansion`: adds repository access, commands, paths, branches, environment variables, budgets, or
  publishing permissions.
- `incompatible`: schema/version/tool-surface change.

Activation policy:

- Restrictions should activate immediately or fail closed until restart.
- Expansions require explicit approval and successful smoke tests.
- Equivalent/metadata changes may hot reload.
- Incompatible changes require controlled restart and possibly Plugin rediscovery.

## 7. Tunnel and runtime lifecycle design

### 7.1 Separate tunnel profile from RepoForge runtime config

Maintain two fingerprints:

1. `tunnel_profile_fingerprint`: tunnel ID, profile name, MCP executable/arguments.
2. `runtime_config_generation`: exact accepted resolved configuration.

A repository change should not re-run `tunnel-client init` unless the tunnel profile fingerprint
changes. It must still reload or restart the MCP child when runtime generation changes.

### 7.2 Supervisor model

Replace direct `os.execvpe(tunnel-client run, ...)` ownership with a small runtime supervisor:

- Holds a single-instance process lock per config/profile.
- Starts `tunnel-client run` as a managed child.
- Records PID, start time, active generation, executable version, and redacted tunnel identity.
- Handles SIGINT/SIGTERM and propagates shutdown to the child process group.
- Implements bounded graceful stop followed by forced termination.
- Restarts with backoff only for explicitly retryable failures.
- Never loops indefinitely on invalid config/authentication.
- Exposes local status and control through a protected Unix-domain socket or equivalent local IPC.
- Does not expose secrets or arbitrary command execution.

Commands:

```bash
rf runtime status
rf runtime start
rf runtime reload
rf runtime restart
rf runtime stop
rf runtime logs --tail 100
```

Keep `rf start` as a compatibility alias for `rf runtime start --foreground`.

### 7.3 Reload strategy

Implement in two stages:

#### Stage A: supervisor-managed restart

This is the first production-safe implementation:

1. Accept and persist a new config generation.
2. Ask the existing runtime to enter `draining` state.
3. Reject new mutating operations with `RUNTIME_RELOADING` while allowing bounded read completion.
4. Wait for active operations up to a configured timeout.
5. Stop the tunnel child process group.
6. Validate and start with the new generation.
7. Run runtime health checks and `repo_list` self-check.
8. If startup fails, roll back to the previous accepted generation and restart it.
9. Report active generation and rollback status.

#### Stage B: in-process config hot reload

Add only after the application/adapters split makes it safe:

- Build a complete immutable service container for the new generation.
- Validate it without mutating the active container.
- Atomically swap the container reference for new requests.
- Existing requests retain the old immutable container until completion.
- Removed repositories become unavailable to new requests immediately.
- Workspaces belonging to removed repositories become `orphaned_read_only` until explicitly cleaned
  up or the repository is re-enrolled.
- Never mutate shared config dictionaries in place.

Stage A delivers reliable UX sooner; Stage B is an optimization, not a prerequisite.

### 7.4 Activation behavior by command

| Command | Default activation behavior |
| --- | --- |
| `rf repo add` | Preview/approve, write generation, smoke test, restart/reload if runtime active |
| `rf repo remove` | Write restrictive generation, immediately drain/restart; fail closed meanwhile |
| `rf repo refresh` | Preview only; no activation |
| `rf repo refresh --accept` | Write generation, smoke affected repos, activate |
| `rf setup` | Create generation, doctor, smoke, then offer/start runtime |
| `rf runtime restart` | Reuse tunnel profile unless its fingerprint changed |
| `rf upgrade` | Validate compatibility, controlled restart, rediscovery hint if schema changed |

Flags:

```text
--activate=auto|always|never
--wait / --no-wait
--foreground
--rollback-on-failure
--non-interactive
```

`auto` activates when a managed runtime is detected; otherwise it returns the exact start command.

### 7.5 ChatGPT Plugin rediscovery

Repository additions do not change MCP tool schemas, so the existing Plugin should continue to work
after the local runtime restarts. A new chat may be useful to avoid conversation-level stale context,
but the Plugin itself should not need recreation.

When tool names, descriptions, annotations, or schemas change, return:

```json
{
  "plugin_rediscovery_recommended": true,
  "reason": "MCP tool surface changed from hash A to hash B"
}
```

Track a deterministic `tool_surface_hash` in runtime state.

## 8. Detailed implementation phases

### Phase 0 — Baseline, CI, and characterization

Implementation status: **Complete** through the Phase 8 release-gate closure. The required workflow,
frozen contracts, compatibility fixtures, and local clean-wheel gate are now versioned with the code.

Goal: make every later refactor measurable and reversible.

Tasks:

- [x] Add CI for Python 3.10–3.13 running Ruff, mypy, pytest with branch coverage, package build, and
  install-from-wheel smoke and real Git/worktree lifecycle tests, including a macOS Python 3.13 runtime lane.
- [x] Record the MCP tool list, descriptions, annotations, input/output schemas, server instruction
  hash, and tool-surface hash in `docs/contracts/release-contract-v2.json`.
- [x] Add deterministic minimal-v2 and legacy-v1 config compatibility fixtures.
- [x] Preserve characterization tests for restart/reload behavior after repository mutation.
- [x] Add a release gate that fails when documentation, MCP/runtime/config contracts, and
  implementation disagree.
- [x] Require narrow scoped Conventional Commits in `CONTRIBUTING.md`.

Acceptance:

- [x] A clean checkout can run the complete source, contract, coverage, package, and wheel-smoke gate.
- [x] The exact HEAD is printed; tracked, staged, and untracked cleanliness is required and the gate must leave no artifacts behind.
- [x] Characterization remains explicit while later phases add behavior behind versioned contracts.

### Phase 1 — Deterministic harness and injectable foundations

Goal: remove hard-wired time, IDs, process execution, and state persistence.

Tasks:

- Add ports for `Clock`, `IdGenerator`, `CommandExecutor`, `WorkspaceStore`, `LockManager`, and
  `AuditSink`.
- Adapt current runner/state/audit implementations behind these ports.
- Add fixed clock, deterministic ID, scripted command executor, in-memory store, and failure injector.
- Add cleanup assertions for worktrees, branches, lock files, temporary files, child processes, and
  registry records.
- Preserve `CodingService(config)` construction while allowing optional injected dependencies.
- Add dependency-boundary tests.

Acceptance:

- Existing MCP schemas unchanged.
- Existing integration tests pass.
- Unit tests run without real Git/GitHub for application decisions.
- Temporary Git integration remains available for adapter verification.

### Phase 2 — Configuration generations and semantic diffs

Goal: distinguish disk state, approved state, and active runtime state.

Tasks:

- Introduce `ConfigGeneration`, monotonic generation number, and immutable snapshots.
- Store source/resolved hashes, repository fingerprints, proposal ID, approval event, and reason.
- Implement semantic config diff and capability delta classification.
- Preserve old resolved lock reading during migration.
- Add atomic write with fsync of file and parent directory where supported.
- Add optimistic generation guard to all config mutations.
- Retain the last known-good generations for rollback.

Acceptance:

- Concurrent config mutations reject stale writers.
- Partial writes never become active.
- Semantic no-op refresh does not create a generation.
- Removing capability is distinguishable from expanding capability.

### Phase 3 — Smart repository proposal workflow

Goal: replace one-shot heuristic generation with inspect → decide → approve → enroll.

Tasks:

- Move detection into pure facts plus adapter-backed probes.
- Add confidence, findings, required decisions, and capability delta.
- Add monorepo, multiple lockfile, ambiguous remote/base, submodule/LFS, and large-repo detection.
- Add `rf repo inspect`, `rf repo propose`, and `rf repo enroll` while preserving `repo add` as a
  guided alias.
- Support JSON output for UI/MCP consumers and concise terminal rendering for humans.
- Require explicit approval for new command capability.
- Add policy templates and repository-specific overrides without silently broadening global defaults.

Acceptance:

- Ambiguity never silently chooses a dangerous option.
- Non-interactive mode returns actionable structured decisions.
- Same repository state produces byte-identical proposal output.
- Unsupported ecosystems enroll read-only or fail clearly; they never receive invented commands.

### Phase 4 — Runtime supervisor and tunnel lifecycle

Goal: make activation and restart deterministic and observable.

Tasks:

- Add runtime state machine and local single-instance lock.
- Implement managed child process groups and signal handling.
- Split tunnel profile fingerprint from runtime config generation.
- Add status/start/stop/restart commands and foreground compatibility mode.
- Record active generation and tool-surface hash.
- Implement bounded drain and supervisor-managed restart.
- Implement rollback to last known-good generation on failed activation.
- Redact runtime secrets from state, logs, errors, and process diagnostics.
- Add correlation IDs across config mutation, activation, tunnel doctor, and health checks.

Acceptance:

- Adding a repo while runtime is active makes it visible without manual process hunting.
- Removing a repo revokes new access immediately or moves runtime into a fail-closed state.
- Tunnel profile is not unnecessarily reinitialized for repository-only changes.
- Failed activation restores the previous healthy generation.
- Duplicate starts fail with a clear `ALREADY_RUNNING` result.

### Phase 5 — Application use cases and compatibility facade

Goal: break up `CodingService` without breaking clients.

Migration order:

1. `CreateWorkspace` — exercises Git, store, lock, clock, ID, and cleanup.
2. Workspace read/write operations — exercises filesystem policy and optimistic locks.
3. `VerifyWorkspace` and `CommitWorkspace` — protects exact verified-tree invariant.
4. `PushWorkspace` and draft PR operations — introduces GitHub gateway and idempotency.
5. Repository inspection and doctor.
6. Onboarding and runtime activation.

For every migrated method:

- Add typed command/result DTOs.
- Keep the existing `CodingService` method as a delegating facade.
- Keep MCP handlers thin and schemas stable.
- Add application unit tests and adapter contract tests.
- Remove legacy implementation only after all callers are migrated.

Acceptance:

- No application use case imports subprocess, `gh`, `fcntl`, or concrete filesystem state.
- Domain code remains pure.
- Each public tool maps to one application use case.

### Phase 6 — Structured UX, observability, and operational hardening

Implementation status: **Complete** on the implementation branch based on
`9c98ceb350b7d8dc6cad033d7d0bf9d9059be4a1`. The implementation contract and operator behavior are
documented in `docs/architecture/phase6-operational-hardening.md`.

Goal: make failures actionable and production operation diagnosable.

Tasks:

- [x] Add stable error codes, retryability, remediation, and unchanged-state fields.
- [x] Add structured logging with secret redaction and correlation IDs.
- [x] Add operation duration and failure-category metrics.
- [x] Add bounded local log retention, including redaction and rotation while the tunnel child runs.
- [x] Add `rf diagnostics bundle` that excludes secrets, file content, patches, PR bodies, runtime log
  content, and full env.
- [x] Add startup capability discovery for Git, `gh`, authentication, tunnel client, toolchains, and
  filesystem features.
- [x] Add cross-process idempotency keys to create/push/create-draft-PR/update-draft-PR workflows.
- [x] Define automatic retry policy only for keyed, reconciliable operations and transient failures.

Acceptance:

- [x] CLI and MCP boundaries return stable redacted error envelopes instead of raw subprocess noise.
- [x] Every failed user-facing write reports unchanged state and a safe next action.
- [x] Audit, diagnostics, idempotency receipt, and live tunnel-log fixtures prove secret redaction.
- [x] Cross-process concurrency proves a keyed external effect executes once.
- [x] Ruff, strict Mypy, full pytest with branch coverage, and package build pass.

### Phase 7 — Optional atomic hot reload

Implementation status: **Complete** based on
`ada5d6fca145f66a690cbe851268a65cd127cc76`. The runtime transaction, fallback behavior, and
removed-repository policy are documented in `docs/architecture/phase7-atomic-hot-reload.md`.

Goal: reduce restart interruption after Stage A is stable.

Tasks:

- [x] Build immutable generation-scoped service containers.
- [x] Atomically commit the active generation and swap the container for new requests.
- [x] Track and pin active request counts per generation.
- [x] Drain and dispose obsolete containers only after their last request completes.
- [x] Define integrity-protected `orphaned_read_only` behavior for removed-repository workspaces.
- [x] Keep supervisor restart as the fallback for incompatible or unsupported reloads.
- [x] Reconcile supervisor child restarts to a successfully hot-reloaded generation.
- [x] Preserve MCP schemas while adding a versioned, allowlisted `RELOAD` control command.

Acceptance:

- [x] Concurrent requests see one complete generation, never a partial mixture.
- [x] Removed repositories are unavailable to new requests immediately.
- [x] Failed candidate construction or activation commit leaves active runtime untouched.
- [x] Existing requests complete on their pinned generation while new requests use the candidate.
- [x] Repository-only reload preserves the tunnel process and tunnel profile.
- [x] Child crash after hot reload restarts against the new committed generation.
- [x] Supervisor-managed restart remains available for incompatible generations and recovery.

### Phase 8 — Program completion and release gates

Implementation status: **Complete** based on
`1a9043ab2a926ca69d005cb459879a64b39d7e44`. This phase closes the remaining plan-wide release,
compatibility, CI, and verification gaps without adding a new runtime capability.

Goal: make the complete program reproducible from a clean checkout and prevent undocumented public
contract drift.

Tasks:

- [x] Close the fast-child-exit race so lifecycle completion is not reported before the bounded tunnel
  log pump reaches EOF and persists output.
- [x] Add required Linux Python 3.10–3.13 and macOS Python 3.13 CI lanes.
- [x] Freeze MCP schemas/annotations and configuration/runtime/diagnostics versions in one reviewed
  machine-readable release contract.
- [x] Add deterministic minimal and legacy configuration fixtures.
- [x] Add `scripts/verify-production.sh` for exact-HEAD, contract, Ruff, strict Mypy, coverage, build,
  and clean-wheel verification.
- [x] Add isolated wheel-install smoke plus a real temporary Git/worktree lifecycle and retain `scripts/run-tunnel.sh` as a foreground
  compatibility entry point.
- [x] Add scoped Conventional Commit and contract-update contribution guidance.
- [x] Replace stale rollout instructions with the actual release-candidate procedure.

Acceptance:

- [x] Fast child exit cannot produce a false `is_alive() == false` before log finalization.
- [x] Public MCP or protocol/config schema drift fails locally and in CI until explicitly reviewed.
- [x] Supported Python versions run the complete test gate; Darwin-specific runtime behavior runs on
  macOS CI.
- [x] The built wheel installs in a fresh environment, exposes the expected CLI/MCP contract, and completes a real create/modify/verify/commit/push/cleanup lifecycle.
- [x] The full plan has a traceable test/requirement completion matrix.

## 9. Test plan

Implementation coverage for every category below is mapped in
`docs/testing/program-completion-matrix.md`. The release workflow runs the complete suite rather than
selecting only phase-specific tests.

### 9.1 Unit tests

- Semantic config delta classification.
- Detection confidence and required-decision generation.
- Runtime state transitions.
- Restart/reload decision matrix.
- Error envelope rendering.
- Idempotency and retry decisions.
- Secret redaction.

### 9.2 Port contract tests

- Git repository/worktree adapter.
- GitHub gateway.
- Command executor timeout, cancellation, output bounds, and process-group cleanup.
- Workspace/config stores with corruption and stale-version handling.
- Lock manager mutual exclusion and timeout behavior.
- Tunnel client adapter init/doctor/run behavior.

### 9.3 Integration tests

- Temporary source clone and bare remote.
- Add repo while runtime is stopped.
- Add repo while runtime is healthy.
- Remove repo while requests are active.
- Refresh command sources with preview and acceptance.
- Tunnel profile unchanged but config generation changed.
- Tunnel profile changed.
- Child crash, restart backoff, and rollback.
- Commit succeeds but state save fails.
- Push succeeds but receipt update fails.
- Worktree creation succeeds but registry save fails.
- Crash during atomic config replacement.

### 9.4 Concurrency tests

- Two simultaneous repo additions.
- Refresh racing with remove.
- Workspace mutation racing with runtime drain.
- Hot-reload generation swap under concurrent reads.
- Stale config generation and stale workspace fingerprint rejection.

Use real subprocesses/processes where locking semantics matter; threads alone are insufficient.

### 9.5 MCP contract tests

- Tool names, annotations, and schemas remain unchanged during internal refactor.
- Runtime/config additions are additive and versioned.
- Golden success and error responses.
- Removed repository becomes unavailable after activation.
- Tool-surface hash changes only when public MCP metadata changes.

### 9.6 End-to-end tests

- Install wheel into a clean environment.
- Configure two temporary repositories.
- Start fake tunnel client and MCP server.
- Add a third repository and auto-activate it.
- Verify it appears through `repo_list`.
- Remove it and verify access is revoked.
- Modify a command source, preview, accept, activate, and verify new profiles.
- Simulate activation failure and verify rollback.
- Verify no worktree, lock, child process, or config temp file leaks.

## 10. Security requirements

- Repository scanning never executes discovered repository commands.
- Command profiles are data, reviewed before execution.
- Expanding capability always requires explicit approval.
- Removed capability fails closed until activation completes.
- Runtime IPC accepts only enumerated control messages and validates peer ownership/permissions.
- PID files are not trusted without process identity validation.
- Config snapshots and runtime state use restrictive file permissions.
- Tunnel/API secrets never enter config generations, fingerprints, logs, audit, diagnostics, or CLI
  arguments.
- Restart never force-pushes, merges, deletes remote branches, or modifies user source clones.
- Active workspace operations are either drained or rejected; they are never silently interrupted
  midway through an external write.
- Rollback cannot restore a repository that was explicitly removed for emergency revocation unless
  the operator authorizes that rollback policy.

## 11. Migration and compatibility

- [x] Continue accepting legacy `[repositories.*]` configs during the documented compatibility
  window, with a frozen legacy fixture.
- [x] Generate an import preview before converting legacy config to minimal generations.
- [x] Preserve existing MCP tool names and parameters through a frozen release contract.
- [x] Preserve workspace registry records and verification semantics.
- [x] Keep `rf start` and `./scripts/run-tunnel.sh` working as compatibility entry points.
- [x] Add new CLI commands without changing existing defaults unexpectedly.
- [x] Version runtime-control protocol and configuration snapshots independently.
- [x] Provide `rf config rollback <generation>` and `rf config history`.

## 12. Rollout strategy

Implementation state:

1. [x] Phases 0–2 landed behind versioned compatibility boundaries.
2. [x] Proposal commands are explicit and deterministic.
3. [x] `repo add` delegates to the proposal/enrollment workflow.
4. [x] The supervisor is available through `rf runtime start`.
5. [x] `rf start` delegates to foreground supervisor start.
6. [x] Interactive repository mutations default to managed `--activate=auto` behavior.
7. [x] Non-interactive capability expansion still requires explicit approval and activation flags.
8. [x] Atomic hot reload is implemented with supervisor restart as the compatibility/recovery fallback.
9. [ ] Require the first green `production-gate` run on `dev` before tagging a release; this remote
   confirmation occurs only after the implementation commit is pushed.

## 13. Suggested pull-request sequence

1. `ci: establish required production verification gate`
2. `test: characterize config and tunnel restart behavior`
3. `refactor: introduce injectable runtime ports`
4. `feat: add immutable config generations and semantic diff`
5. `feat: add repository proposal and decision model`
6. `feat: add guided repository enrollment workflow`
7. `feat: add runtime status and single-instance supervisor`
8. `feat: activate config generations with rollback`
9. `refactor: migrate create-workspace use case`
10. `refactor: migrate workspace filesystem use cases`
11. `refactor: migrate verification and commit use cases`
12. `refactor: migrate publishing use cases`
13. `feat: add structured errors, correlation, and metrics`
14. `feat: add optional atomic configuration hot reload`

Each PR should remain independently deployable and preserve the safety invariants in `AGENTS.md`.

## 14. Definition of done

The implementation is complete when:

- [x] A repository can be inspected, proposed, approved, enrolled, smoke-tested, and activated without
  manual tunnel process discovery.
- [x] Every config mutation reports disk generation, active generation, and restart/reload status.
- [x] Removal revokes access promptly and safely.
- [x] Repository-only changes do not unnecessarily reinitialize the tunnel profile.
- [x] Activation failure rolls back without losing the previous healthy runtime.
- [x] MCP public contracts remain compatible unless an explicit versioned migration is approved.
- [x] `CodingService` is a compatibility facade over application use cases rather than the
  architectural center.
- [x] Domain and application tests do not require real GitHub or user repositories.
- [x] Adapter contracts, integration, concurrency, crash, and end-to-end suites are part of the
  required production workflow.
- [x] No secrets, arbitrary shell capability, force push, non-draft PR, protected-path write, or
  verified-tree bypass is introduced.
- [x] User-facing errors explain what happened, what remained unchanged, and the safe next action.
- [ ] The remote `production-gate` workflow passes on the pushed implementation commit before release.

## 15. Immediate next action

Apply the Phase 8 completion change, then run the exact local release gate:

```bash
scripts/verify-production.sh --allow-dirty
```

After committing, run it again without `--allow-dirty`, push to `dev`, and require the GitHub
`production-gate` workflow to pass on Linux Python 3.10–3.13 and macOS Python 3.13 before tagging or
promoting a release. Public contract changes require an explicit review of
`docs/contracts/release-contract-v1.json`; the golden file must never be regenerated merely to make
CI green.
