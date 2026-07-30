# Auth Profile CLI, MCP, Import, and Migration UX

Issue: #295  
Epic: #284  
Status: approved design  
Date: 2026-07-30

## Purpose

RepoForge must let operators and agents inspect, bind, select, diagnose, and recover repository identities without changing process-global GitHub CLI, Git, SSH, author, or signer state. The feature composes the stable repository binding, API identity, transport identity, operation lease, commit identity, publication, preflight, and nested-resource primitives delivered by issues #286 through #294.

This design adds the application and interface layer that makes those primitives usable. It does not weaken the boundaries owned by workspace mutation leases (#22), capability policy (#51), workload identity (#52), repository consent (#155), connector cutover (#194), or durable receipt truth (#232).

## Locked invariants

1. Repository identity is anchored by provider host and stable provider repository ID. Names and paths are discovery hints only.
2. `auto` succeeds only when resolution has exactly one enabled, eligible, policy-authorized profile for the requested actor role and exact repository binding state.
3. Zero or multiple candidates fail closed with a typed failure and explicit recovery actions.
4. Explicit profile selection still passes binding, actor-role, capability, transport, commit, signer, and publication-policy checks.
5. Selection is captured before a durable operation starts. A running operation never changes profile or actor class; lease refresh may renew lifetime only when every locked identity field remains equal.
6. RepoForge never runs `gh auth switch`, never writes global Git configuration, never edits the user's SSH configuration, and never falls back to ambient credential helpers or active accounts.
7. Secret bodies never enter configuration, durable state, logs, CLI output, MCP payloads, diagnostics, receipts, or exceptions. Only opaque references and safe digests may cross those boundaries.
8. API actor, Git transport, repository IDs and targets, commit author/committer, signer, and publication destination are separate surfaces with separate evidence.
9. One eligible profile preserves compatibility: `auto` selects it deterministically without an interactive prompt.
10. Import and migration are inspect/plan/apply workflows. Detection alone is read-only and cannot mutate external global state.

## Chosen architecture

### AuthUxService application facade

Add a focused application facade, `AuthUxService`, rather than putting orchestration in the large CLI module or allowing handlers to call persistence adapters directly. The facade receives existing ports and narrow new read-only discovery ports, then returns safe typed view models.

Responsibilities:

- list and inspect reviewed credential profiles;
- inspect exact repository bindings and eligible roles;
- resolve `auto` or an explicit profile against a live repository observation;
- create, update, or remove the requested role slot in a repository binding with revision locking;
- assemble `whoami` surface evidence without collapsing distinct identities;
- diagnose configuration, binding, API, transport, author, signer, lease, and publication readiness;
- inspect and revoke operation-scoped auth leases through `OperationIdentityManager`;
- produce import and migration plans from named GitHub CLI accounts and SSH aliases;
- emit safe receipts and typed recovery actions for every state-changing command.

The facade must not execute publication operations. Existing publication, preflight, and nested-resource coordinators remain the admission boundary for effects.

### Configuration model

Add reviewed identity configuration at the application root instead of overloading verification profiles:

- `auth_profiles.<profile_id>` defines provider, credential kind and opaque reference, actor class, expected actor, enabled state, capability IDs, repository patterns, and boundary ID.
- API source configuration identifies either a named stored `gh` account or a GitHub App installation reference.
- Transport configuration pins HTTPS isolation or a concrete SSH identity file. A source SSH alias may be recorded as provenance, but runtime transport uses the imported concrete host and identity file with `ssh -F /dev/null`.
- Commit identity remains repository-owned and references the same profile ID. Existing author, committer, signing, delegation, and worktree-scoped enforcement stays authoritative.
- Repository bindings remain durable stable-ID records with independent human and agent role slots.

Resolved configuration rendering and loading must reject unknown fields, duplicate profile IDs, cross-boundary patterns, role/profile mismatches, secret-shaped values, invalid paths, and references that cannot be represented safely.

### Selector contract

Define a shared request selector:

- `auth_profile`: `auto` or an exact profile ID; default `auto` for backward compatibility.
- `actor_class`: public value `human` or `agent`; default `human` for existing human-operated workflows.

Public `human` maps to the existing human credential role and permits `human_operated` or `delegated_human` profiles. Public `agent` maps only to `autonomous_agent`.

The selector is added to MCP inputs that can begin or perform repository effects, including workspace creation/refresh where remote access occurs, commit, push, pull-request writes, and issue writes. Read-only calls do not accept it. Local tree mutation and read-only verification do not select a new external identity; they consume the workspace/operation context already established when applicable.

For multiplexed tools such as `repo_issue` and `workspace_pr`, validators reject selector fields on purely read/watch branches and accept them only on effectful branches. The 28-tool roster remains unchanged.

### Selection and operation flow

1. Resolve the exact repository from configured repository ID and live provider observation.
2. Convert `actor_class` to a credential role.
3. Resolve `auth_profile`:
   - `auto`: use the existing exact binding when present; for an unbound repository require exactly one eligible candidate and return a binding proposal rather than silently persisting it;
   - explicit ID: locate exactly one enabled profile and run the same provider, role, repository, boundary, binding, and policy checks.
4. Acquire target-bound API and/or transport material through the existing broker.
5. Verify live API actor and repository identity and transport target evidence.
6. Create operation leases and bind one immutable `OperationIdentityContext` sidecar before the durable operation is admitted.
7. Downstream commit/publication/nested-resource effects consume the sidecar and receipts; they never re-resolve ambient identity.

An explicit selection cannot replace a conflicting durable binding without an explicit `rf auth bind` update using the current binding revision.

## CLI surface

Add `rf auth` with a focused parser/handler module registered by `interfaces/cli/main.py`.

### Profile commands

- `rf auth profile list [--actor-class human|agent] [--enabled-only]`
- `rf auth profile inspect <profile-id>`

Outputs contain safe metadata, capability IDs, repository eligibility, credential reference scheme/ID, transport kind and fingerprints. They never contain tokens, private-key bodies, helper commands containing secrets, or signing-key material.

### Binding and resolution commands

- `rf auth bind --repo-id <id> --actor-class human|agent --profile <id> --expected-revision <revision>`
- `rf auth unbind --repo-id <id> --actor-class human|agent --expected-revision <revision>`
- `rf auth resolve --repo-id <id> [--auth-profile auto|<id>] [--actor-class human|agent]`

`resolve` is read-only. For an unbound exact candidate it returns `proposal_required`, the proposed safe binding, and the apply command. Bind/unbind use optimistic concurrency and return durable safe receipts.

Unbinding the final role is allowed only when the repository can remain enrolled without effectful auth; effectful operations then fail closed until rebound. It must not delete credential material or alter external accounts.

### Identity and diagnostics commands

- `rf auth whoami --repo-id <id> [--auth-profile auto|<id>] [--actor-class human|agent] [--check api|transport|author|signer|publication|all]`
- `rf auth doctor [--repo-id <id>] [--auth-profile auto|<id>] [--actor-class human|agent]`

`whoami --check all` reports independent rows for:

- repository binding: provider host, stable repository ID, canonical name, binding revision;
- API: profile, actor class, expected and observed actor, target repository ID, capabilities and freshness;
- transport: kind, provider host, target, credential fingerprint, allowed access, and actor observability limitation;
- author and committer: configured values and worktree-scoped evidence;
- signer: mode, fingerprint or attestation status, without key references;
- publication: source repository, destination repository, remote, exact target/ref and policy/preflight readiness.

A surface can be `verified`, `configured`, `unobservable`, `blocked`, or `unavailable`; one surface cannot imply another is verified. The overall result is `ready` only when every required surface is acceptable for the requested check.

`doctor` additionally reports disabled/ambiguous profiles, stale bindings, missing opaque references, conflicting ambient configuration, expired/revoked leases, transport mismatch, author/signer drift, and publication-policy denial. Each finding carries an error code, surface, retryability, unchanged state, and typed recovery actions.

### Lease commands

- `rf auth lease inspect --operation-id <id>`
- `rf auth lease revoke --operation-id <id> (--lease-id <id> | --profile <id>) --expected-revision <revision>`

Inspection exposes only safe lease payloads and operation identity digests. Revocation uses the existing operation identity manager and optimistic revision locking. It cannot change profile, actor, repository, target, or capability scope.

## Named account and SSH import

### GitHub CLI accounts

`rf auth import gh --hostname <host> [--user <login>]` asks the GitHub CLI only for named-account metadata and, during explicit verification, a token for that exact host/login using `gh auth token --hostname ... --user ...`. It does not inspect or trust the active account and never invokes `gh auth switch`.

Detection returns a bounded candidate with provenance, actor metadata, opaque reference, and conflicts. Applying an import writes only reviewed RepoForge configuration through the immutable generation workflow. A wrong globally active account is irrelevant to selection and must be covered by regression tests.

### SSH aliases

`rf auth import ssh --alias <name>` parses the named alias through a read-only discovery adapter. Only deterministic single-valued `HostName`, `User`, and absolute `IdentityFile` values are importable. Wildcards, `Match` blocks, proxy commands, identity agents, multiple identity files, token expansion that cannot be resolved safely, or recursive/includes with ambiguous precedence produce typed conflicts.

The apply plan stores a concrete pinned transport spec and safe source provenance. Runtime commands continue to use `ssh -F /dev/null`, `IdentitiesOnly=yes`, no identity agent, no prompts, and the imported absolute identity file. RepoForge never edits `~/.ssh/config`.

## Migration UX

Add `rf auth migrate inspect` and `rf auth migrate apply --plan-id ... --plan-hash ...`.

Inspection detects:

- a single existing RepoForge repository with no auth profile;
- multiple or conflicting named `gh` accounts;
- a different active `gh` account than the reviewed candidate;
- ambient `GH_TOKEN`/GitHub token variables;
- global or local Git credential helpers;
- global/local/worktree author and signer values that conflict with the reviewed profile;
- SSH aliases with ambiguous or unsupported semantics;
- legacy repository remotes whose host/owner/repository do not match the stable binding.

The plan is deterministic, bounded, secret-free, hash-bound, and classifies every change as `create_profile`, `create_binding`, `pin_transport`, `set_commit_identity`, or `manual_remediation`. Apply revalidates the plan hash, exact configuration generation, repository observation, and source metadata before accepting a new immutable configuration generation.

Single-profile compatibility may generate a no-prompt plan, but it still records the exact selected profile and evidence. Conflicts never trigger best-effort fallback.

## Errors, recovery, and receipts

Reuse `RepositoryAuthFailure`, `RecoveryAction`, the unified public error envelope, operation outcome receipts, and identity receipts. Extend recovery kinds only where the existing set cannot express import/migration actions.

Representative failures:

- profile not found, ambiguous, disabled, or unauthorized;
- binding missing, ambiguous, stale, or repository mismatch;
- actor role or live actor mismatch;
- transport proof unavailable or identity mismatch;
- lease expired/revoked or refresh identity mismatch;
- author, committer, or signer mismatch;
- publication target mismatch or policy denial;
- ambient configuration conflict;
- import source ambiguous or unsupported;
- migration plan stale.

Every failure states that no external repository write was admitted unless durable outcome evidence proves otherwise. Effectful CLI commands return the same operation/receipt/result-reference triad used by MCP effects.

## Testing strategy

### Domain and application tests

- auto resolution: exact binding, unique unbound proposal, zero candidates, multiple candidates, disabled candidates;
- explicit selection: success plus binding, role, provider, repository, capability, and policy rejection;
- human/agent role mapping and delegated-human handling;
- immutable operation identity across profile/config changes;
- safe payload and secret-shape rejection;
- binding concurrency and lease revocation.

### CLI tests

- parser and golden JSON/human output for every command;
- `whoami --check all` keeps all identity surfaces distinct;
- typed error/recovery rendering;
- one-profile no-prompt behavior;
- import/migration preview and stale-plan rejection;
- no invocation of global-switch or global-config mutation commands.

### Adapter tests

- named `gh` account succeeds when another account is globally active;
- token and probe commands always include exact host/login or isolated token environment;
- SSH alias import accepts only deterministic safe aliases;
- transport runtime remains pinned with `ssh -F /dev/null`;
- conflicting ambient environment/config is reported but never consumed.

### MCP contract tests

- schema golden updates for relevant effectful inputs;
- defaults preserve one-profile callers;
- selector fields rejected on read-only branches;
- invalid profile/actor values fail validation;
- static 28-tool identity remains stable aside from the intentional contract digest update.

### Verification

Run affected identity, configuration, CLI, MCP contract, publication/preflight, operation lease, and shipping tests; then the repository safety bundle, strict type checking, formatting/linting, manifest capability partitions, and wheel build/install verification required by the project.

## Out of scope

- changing workspace mutation lease semantics;
- redefining capability policy or provider-neutral workload identity;
- implementing repository consent prompts from #155;
- changing connector cutover behavior from #194;
- adding new public MCP tools or changing the 28-tool roster;
- storing raw tokens or private keys;
- global account switching, global Git mutation, or SSH config mutation;
- treating transport success as proof of API actor identity.
