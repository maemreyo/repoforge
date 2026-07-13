# Guided Local Repository Onboarding Design

Status: Approved for planning  
Date: 2026-07-14  
Target repository: `maemreyo/repoforge`  
Baseline: `main@2a3d6af1a1cb5102ce3469e71d483bdb1a037ac6`

## 1. Problem

RepoForge already has safe low-level primitives for repository inspection, proposal generation, explicit decisions, approval tokens, immutable configuration generations, smoke tests, and runtime activation. The operator experience remains fragmented, however.

Today, onboarding several local repositories requires the operator to:

1. discover Git repositories manually;
2. filter linked worktrees and generated directories manually;
3. determine whether the RepoForge config already exists;
4. choose between `setup` and repeated `repo propose`/`repo add` commands;
5. parse JSON output, often using `jq` and shell-specific loops;
6. carry required decisions and exact approval tokens between commands;
7. detect duplicate repository IDs;
8. resume manually after interruption;
9. activate or restart the runtime separately.

This flow is safe but operator-hostile. It is especially error-prone when an existing virtual environment shadows the `uv tool` installation, when a config already exists, or when repository discovery includes linked worktrees such as `.claude/worktrees/*`.

## 2. Goals

Add a first-class guided onboarding workflow that preserves every existing safety boundary while reducing the normal path to one command:

```bash
rf onboard /Users/trung.ngo/Documents/zaob-dev
```

The workflow must:

- discover candidate local Git repositories under one or more roots;
- exclude generated, bare, linked-worktree, and RepoForge-managed workspace paths by default;
- recognize whether configuration already exists;
- skip already-enrolled repositories;
- produce deterministic repository proposals using existing proposal logic;
- gather required decisions one repository at a time;
- require explicit human approval for every executable capability expansion;
- enroll approved repositories transactionally;
- preserve immutable generation, smoke-test, activation, rollback, and audit behavior;
- persist resumable non-secret session state;
- support both interactive and fully specified non-interactive operation;
- explain environment problems, including executable shadowing, before mutation.

## 3. Non-goals

The guided workflow will not:

- grant wildcard filesystem access;
- automatically approve detected verification commands;
- execute arbitrary shell commands;
- silently broaden repository policy;
- enroll linked worktrees as independent repositories by default;
- replace existing `setup`, `repo inspect`, `repo propose`, `repo enroll`, or `repo add` primitives;
- manage the user's shell environment or deactivate a virtual environment automatically;
- persist `CONTROL_PLANE_API_KEY`, GitHub tokens, repository contents, patches, or command output;
- create one tunnel per repository;
- scan the entire machine unless the operator supplies such a root explicitly.

## 4. Command surface

### 4.1 Primary command

```bash
rf onboard ROOT [ROOT ...]
```

Options:

```text
--max-depth N
--include GLOB
--exclude GLOB
--template read_only|standard|strict
--activate auto|always|never
--plan-only
--resume SESSION_ID
--non-interactive
--decision CODE=CHOICE
--decision REPO_ID.CODE=CHOICE
--policy-override KEY=VALUE
--policy-override REPO_ID.KEY=VALUE
--approve approve:PROPOSAL_ID
--wait / --no-wait
--rollback-on-failure / --no-rollback-on-failure
```

Defaults:

- `--template standard`;
- `--activate auto`;
- `--wait`;
- `--rollback-on-failure`;
- interactive mode when stdin and stderr are TTYs;
- fail closed when interaction is required but unavailable.

### 4.2 Supporting commands

```bash
rf repo discover ROOT [ROOT ...]
rf onboard status SESSION_ID
rf onboard resume SESSION_ID
rf onboard cancel SESSION_ID
```

`rf repo discover` is read-only. It prints discovery decisions without generating proposals or writing configuration.

`rf onboard resume SESSION_ID` is equivalent to `rf onboard --resume SESSION_ID`.

## 5. User experience

### 5.1 Environment preflight

Before repository discovery, onboarding reports:

- current `rf` executable path;
- current Python executable and active virtual environment;
- a separately installed `uv tool` executable when discoverable;
- Git availability and version;
- GitHub CLI availability and authentication status;
- `tunnel-client` availability and version;
- config path and whether it exists;
- current runtime state;
- whether `CONTROL_PLANE_API_KEY` is available when activation may require runtime startup.

Executable shadowing is a warning, not an automatic mutation. Example:

```text
Current rf executable:
  /path/to/repository/.venv/bin/rf

A uv-tool installation also exists:
  ~/.local/bin/rf

The active virtual environment shadows the uv-tool installation.
Continue with the current executable, or exit and deactivate the environment first?
```

In non-interactive mode, executable shadowing is included in structured output. It is fatal only when the current executable is incompatible with the requested operation.

### 5.2 Discovery review

The operator receives three groups:

1. eligible repositories;
2. excluded paths with stable exclusion reasons;
3. ambiguous paths requiring a decision.

Stable exclusion reasons include:

```text
already_enrolled
linked_worktree
repoforge_managed_workspace
bare_repository
generated_worktree_directory
nested_duplicate_checkout
outside_allowed_root
invalid_git_repository
unreadable_path
```

### 5.3 Repository proposal review

For each eligible repository, onboarding displays:

- repository ID and canonical path;
- remote and default base branch;
- detected ecosystem and package manager;
- instruction files;
- findings and confidence;
- proposed policy template;
- allowed and denied paths;
- publish capability;
- exact verification profiles and argv arrays;
- required decisions;
- proposal ID and approval token.

The interactive choices are:

```text
y  approve the displayed proposal
s  regenerate using strict template
r  regenerate using read_only template
d  show full proposal details
k  skip this repository
q  save the session and exit
```

There is no blanket approval for unseen executable profiles. A final batch confirmation is allowed only after each proposal has been individually displayed and marked ready.

### 5.4 Completion summary

The command reports:

- discovered, excluded, skipped, enrolled, unchanged, and failed counts;
- accepted generation;
- active generation;
- activation method: no-op, hot reload, supervisor restart, or stopped;
- runtime health;
- exact next action;
- resumable session ID when incomplete.

## 6. Repository discovery rules

Discovery uses Git metadata, not only directory-name matching.

### 6.1 Candidate identity

A candidate must resolve to a valid non-bare Git worktree. Canonical identity uses:

- resolved working-tree root;
- resolved Git common directory;
- primary versus linked-worktree status;
- configured RepoForge workspace roots;
- canonical filesystem path.

### 6.2 Default exclusions

Exclude paths matching or contained within:

```text
**/.claude/worktrees/**
**/.worktrees/**
**/worktrees/** when confirmed as linked worktree storage
**/node_modules/**
**/.venv/**
**/venv/**
**/vendor/**
**/.cache/**
~/.local/share/repoforge/workspaces/**
configured server.workspace_root/**
```

A name match alone is not enough for a Git-specific decision. Git metadata confirms linked-worktree status.

### 6.3 Linked worktrees

Keep the primary checkout and exclude linked worktrees whose Git common directory points to the same repository.

Explicit linked-worktree enrollment is out of scope because RepoForge creates and manages isolated worktrees itself.

### 6.4 Nested repositories

A real nested repository remains eligible when it has a distinct Git common directory and is not generated or ignored. The UI shows the parent-child relationship so the operator can choose whether to enroll both.

### 6.5 Duplicate repository IDs

Repository IDs use existing safe slug rules. Duplicate IDs block mutation until explicit IDs are supplied.

Interactive mode prompts for replacements. Non-interactive mode returns `DUPLICATE_REPOSITORY_ID` with all conflicting paths.

## 7. Config-aware behavior

### 7.1 No existing configuration

Onboarding builds the same approved source and resolved configuration that `rf setup` creates, using existing proposal, generation, smoke-test, and activation services.

The first configuration is written only after every selected repository has:

- no unresolved decisions;
- a non-blocked proposal;
- a matching exact approval token;
- a successful candidate smoke test.

### 7.2 Existing configuration

Onboarding loads current source and generation metadata, then:

- skips canonical paths already enrolled;
- reports repository-ID/path conflicts;
- proposes only missing repositories;
- applies approved additions through the same enrollment use case as `repo enroll`;
- activates once after the batch rather than once per repository.

The batch uses expected source hash and generation guards. If configuration changes during the session, mutation stops with `CONFIG_CHANGED`; the session remains resumable after review.

## 8. Session model and resume semantics

### 8.1 Location and permissions

```text
~/.local/state/repoforge/onboarding/<session-id>.json
```

Directory mode: `0700`. File mode: `0600`.

### 8.2 Persisted data

The session stores metadata only:

```text
schema_version
session_id
created_at
updated_at
status
roots
options
config_path
expected_source_sha256
expected_generation
discovered repository identities
exclusion decisions
proposal IDs and facts fingerprints
selected templates
decision answers
approval-token hashes
per-repository progress
activation preference
last stable error envelope
```

It never stores:

- API keys or environment snapshots;
- GitHub tokens;
- repository file bodies;
- patches, diffs, PR bodies, stdout, or stderr;
- raw verification output.

### 8.3 State machine

```text
created
  -> discovered
  -> awaiting_decisions
  -> awaiting_approval
  -> ready
  -> applying
  -> activating
  -> completed

Any non-terminal state may transition to:
  paused
  failed_recoverable
  cancelled

Irrecoverable schema corruption transitions to:
  invalid
```

### 8.4 Resume validation

Before resuming, RepoForge revalidates:

- session schema and integrity;
- config source hash and generation;
- repository canonical paths and Git identities;
- proposal facts fingerprints;
- approval validity;
- current enrollment state;
- runtime state.

Stale proposals are never silently reused. Changed repository facts require re-proposal and renewed approval.

## 9. Architecture

### 9.1 Dependency direction

```text
CLI interface
    -> onboarding application coordinator
        -> domain discovery/session/progress models
        -> existing repository proposal and config-generation use cases
            <- filesystem, Git, persistence, environment adapters
```

No domain or application module imports CLI, concrete Git, concrete filesystem, or terminal libraries.

### 9.2 Components

#### `domain/onboarding.py`

Pure models and transition rules:

- `DiscoveryCandidate`;
- `DiscoveryExclusion`;
- `OnboardingRepositoryState`;
- `OnboardingSession`;
- `OnboardingStatus`;
- duplicate-ID detection;
- completion calculations.

#### `ports/repository_discovery.py`

Bounded repository discovery and canonical identity contracts.

#### `ports/onboarding_store.py`

Private session persistence with optimistic revision checks.

#### `ports/operator_io.py`

Interactive prompting and rendering without terminal dependencies in the application layer.

#### `application/onboarding/discover.py`

Coordinates root traversal, exclusions, canonical de-duplication, and already-enrolled detection.

#### `application/onboarding/coordinator.py`

Coordinates inspect, decide, propose, approve, smoke, batch mutation, activation, pause, and resume.

It reuses:

- `RepositoryProposalService`;
- source/resolved configuration document functions;
- `ConfigurationStore` generation guards;
- candidate smoke testing;
- `GenerationActivator`;
- stable operation error envelopes.

#### `adapters/repository/discovery.py`

Bounded filesystem traversal and Git identity inspection.

#### `adapters/persistence/json_onboarding_store.py`

Private, atomic, fsynced session persistence and corruption detection.

#### `interfaces/cli/onboarding.py`

Interactive and structured non-interactive presentation. It contains no repository policy logic.

### 9.3 CLI module size

The new wizard is not implemented inline in `interfaces/cli/main.py`.

`main.py` only:

- declares parser options;
- constructs dependencies through `bootstrap.py`;
- dispatches to the onboarding interface;
- renders final stable errors.

## 10. Mutation and transaction semantics

### 10.1 Plan-first behavior

Before mutation, the coordinator builds a complete batch plan containing:

- selected repositories;
- exact proposals;
- decisions;
- approval evidence;
- candidate source text;
- candidate resolved text;
- expected source hash;
- expected generation;
- capability delta;
- activation plan.

`--plan-only` stops before mutation.

### 10.2 Atomic configuration mutation

The preferred implementation accepts the complete selected batch as one immutable generation, avoiding repeated generation churn and runtime reloads.

### 10.3 Smoke tests

Every selected proposal is smoke-tested against the complete candidate generation before acceptance. Failure leaves source config, accepted generation, active generation, and runtime unchanged.

### 10.4 Activation

Activation preserves current semantics:

- `never`: accept without activation;
- `auto`: activate when managed runtime is live; otherwise report the start command;
- `always`: require successful activation;
- compatible change: atomic hot reload;
- incompatible change: supervisor restart fallback;
- failed expansion: rollback when enabled;
- restriction: fail closed without restoring removed access.

## 11. Error handling

All failures use the existing stable error envelope and add onboarding context:

```text
status
error_code
what_happened
why
correlation_id
session_id
repository_id when applicable
unchanged_state
safe_next_action
retryable
automatic_retry_allowed
```

Required stable codes:

```text
DISCOVERY_ROOT_NOT_FOUND
DISCOVERY_PERMISSION_DENIED
DUPLICATE_REPOSITORY_ID
INTERACTION_REQUIRED
SESSION_NOT_FOUND
SESSION_CORRUPT
SESSION_STALE
CONFIG_CHANGED
REPOSITORY_FACTS_CHANGED
PROPOSAL_BLOCKED
DECISION_REQUIRED
APPROVAL_REQUIRED
APPROVAL_MISMATCH
CANDIDATE_SMOKE_FAILED
ACTIVATION_FAILED
EXECUTABLE_SHADOWED
```

`EXECUTABLE_SHADOWED` is normally a warning and becomes fatal only when the active executable is incompatible.

## 12. Security invariants

- No executable profile is enabled without exact proposal approval.
- Discovery never follows arbitrary symlinks outside supplied roots.
- Canonical paths are checked before enrollment.
- Linked worktrees are excluded by default.
- RepoForge-managed workspaces are always excluded.
- Session files are private and contain no secrets or repository content.
- Approval evidence is bound to proposal ID and facts fingerprint.
- Configuration mutation uses expected generation and source-hash guards.
- Candidate smoke runs in isolated temporary roots.
- Runtime activation reuses identity-validated control channels.
- Non-interactive mode never fabricates decisions or approvals.
- Cancellation reports any already completed mutation explicitly.

## 13. Compatibility

Existing commands remain supported:

```text
rf setup
rf repo inspect
rf repo propose
rf repo enroll
rf repo add
rf repo refresh
```

The wizard orchestrates existing capabilities rather than replacing them.

The source configuration and generation stores remain compatible. The onboarding session schema is independent and versioned.

## 14. Testing strategy

### 14.1 Domain

- valid and invalid session transitions;
- duplicate-ID detection;
- completion and resume calculations;
- stable serialization and schema behavior.

### 14.2 Discovery

- primary checkout retained;
- `.claude/worktrees/*` excluded;
- `.worktrees/*` excluded;
- RepoForge workspace root excluded;
- bare repository excluded;
- real nested repository retained;
- symlink escape prevented;
- unreadable directory bounded;
- max depth and include/exclude honored;
- canonical duplicates collapsed.

### 14.3 Coordinator

- no existing config;
- existing config with already-enrolled repositories;
- mixed enrolled and missing repositories;
- required decision followed by re-proposal;
- blocked proposal;
- template change;
- exact approval required;
- batch smoke failure leaves config unchanged;
- config changes between plan and apply;
- repository facts change before resume;
- interruption and resume at each state boundary;
- one batch generation and one activation;
- activation failure and rollback.

### 14.4 CLI

- interactive happy path;
- skip, details, strict, read-only, pause, cancel;
- existing config selected automatically;
- non-interactive missing decisions exits `3`;
- non-interactive missing approvals exits `3`;
- structured output contains exact next action;
- `.venv/bin/rf` shadowing a `uv tool` installation produces an actionable warning;
- no dependency on `jq` or shell loops;
- human output contains no secrets.

### 14.5 Integration and release

- discover and enroll multiple real temporary Git repositories;
- exclude linked worktrees created with `git worktree add`;
- complete setup from an installed wheel;
- resume after process termination;
- managed hot reload after batch addition;
- supervisor restart fallback;
- private session/config/state permissions;
- frozen CLI contract update;
- wheel-installed guided onboarding E2E on Linux and macOS.

## 15. Documentation

Update:

- `README.md`;
- `docs/CHATGPT_SETUP.md`;
- `docs/TOOL_REFERENCE.md`;
- `docs/FULL_FLOW_TESTING.md`;
- `docs/contracts/release-contract-v1.json`.

The documented happy path becomes:

```bash
uv tool install --force 'git+https://github.com/maemreyo/repoforge.git@main'
rf onboard /absolute/root/containing/repos
rf runtime start
```

## 16. Acceptance criteria

The feature is complete when:

1. `rf onboard ROOT` replaces manual `find`, `jq`, and shell loops.
2. Linked worktrees such as `.claude/worktrees/*` are excluded and explained.
3. Existing configuration is detected and enrolled paths are skipped.
4. Duplicate IDs are resolved before mutation.
5. Every executable capability requires exact reviewed approval.
6. Required decisions are collected per repository and regenerate proposals.
7. Interrupted sessions resume without storing secrets.
8. A selected batch produces one accepted generation and at most one activation.
9. Candidate failure leaves configuration and runtime unchanged.
10. Hot reload and supervisor fallback retain current semantics.
11. Interactive and non-interactive flows are deterministic and tested.
12. Ruff, strict Mypy, branch coverage, build, frozen-contract, and installed-wheel E2E gates pass.

## 17. Rollout

1. Add domain contracts and session persistence behind tests.
2. Add read-only discovery and `rf repo discover`.
3. Add non-interactive coordinator and plan-only mode.
4. Add interactive operator I/O.
5. Add resume/cancel/status commands.
6. Integrate batch acceptance and activation.
7. Update contracts and documentation.
8. Run installed-wheel multi-repository E2E on Linux and macOS.

Existing low-level commands remain the rollback path for unsupported cases.
