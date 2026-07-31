# Auth Profile UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or the project `implement` skill task-by-task. This EPIC session is explicitly main-agent-only; do not delegate or create another workspace.

**Goal:** Implement issue #295 so operators and MCP callers can deterministically inspect, bind, select, diagnose, import, migrate, and revoke repository identities without changing global GitHub CLI, Git, or SSH state.

**Architecture:** Add a small pure selector domain over the identity primitives from #286–#294, strict root-level auth-profile configuration, isolated named-account/SSH discovery adapters, an `AuthUxService` application facade, a focused `rf auth` module, and selector fields on effectful MCP contracts. Identity is resolved and bound before a durable operation starts; running operation sidecars remain immutable.

**Tech Stack:** Python 3.12, frozen slotted dataclasses, `Enum`, `Protocol`, strict TOML parsing/rendering, existing immutable configuration generations, existing repository binding/operation identity stores, Pydantic v2 contracts, argparse CLI, pytest, Ruff, strict Mypy.

## Global constraints

- Work only in workspace `epic-284-repository-iden-9164d41f2f` on branch `ai/epic-284-repository-identity-9164d41f2f`.
- Do not refresh/rebase while generated contract overlap with `main` remains. Preserve the sequential EPIC commit chain.
- Use the main agent only. Do not create a workspace, branch, subagent, or parallel agent.
- Follow RED → verify RED → GREEN → refactor for every production behavior. No production edit before the corresponding failing test is observed.
- Never invoke `gh auth switch`, `git config --global`, `git config --system`, SSH configuration writes, or ambient credential-helper fallback.
- Never persist or render raw tokens, private keys, authorization headers, credential-helper payloads, or signing-key references.
- `auth_profile=auto` succeeds only for one exact deterministic eligible profile. Ambiguity and missing/disabled candidates fail closed.
- Explicit selection passes the same repository binding, role, capability, transport, author, signer, and publication checks as automatic selection.
- A selector is consumed before durable operation admission. An existing `OperationIdentityRecord` cannot change profile, actor class, repository, target, or capability set.
- Keep API identity, transport, repository, author, committer, signer, and publication evidence separate.
- Do not add a 29th MCP tool. Update generated contract artifacts only with the repository generator and review the resulting digests.
- After each task, run focused tests plus `tests/test_repository_identity_contracts.py` and `tests/test_operation_identity_leases.py` where relevant.

---

## Task 1: Public selector and explicit deterministic resolution

**Files:**

- Create: `src/repoforge/domain/auth_profile.py`
- Modify: `src/repoforge/domain/repository_identity_resolution.py`
- Modify: `src/repoforge/domain/__init__.py`
- Create: `tests/test_auth_profile_selection.py`

- [ ] **Step 1: Write RED selector and resolution tests**

Name the protected breaks explicitly:

- changing the public default from `auto`/`human` must break compatibility tests;
- selecting a second eligible profile under `auto` must produce `PROFILE_AMBIGUOUS` rather than picking by order;
- an explicit profile not present, disabled, wrong provider, wrong role, wrong repository boundary, or conflicting with an exact binding must fail before material acquisition;
- a unique unbound profile must return `proposal_required`, not persist a binding;
- an exact binding must select its role slot and preserve the binding revision;
- `human` accepts `HUMAN_OPERATED` and `DELEGATED_HUMAN`; `agent` accepts only `AUTONOMOUS_AGENT`;
- selector payloads and failures contain only safe metadata and typed recovery actions.

Run:

```bash
uv run pytest tests/test_auth_profile_selection.py -q
```

Expected: FAIL because `repoforge.domain.auth_profile` does not exist.

- [ ] **Step 2: Implement minimal selector contracts**

Use:

```python
class RequestedActorClass(str, Enum):
    HUMAN = "human"
    AGENT = "agent"

@dataclass(frozen=True, slots=True)
class AuthProfileSelector:
    auth_profile: str = "auto"
    actor_class: RequestedActorClass = RequestedActorClass.HUMAN

    @property
    def role(self) -> CredentialRole: ...

@dataclass(frozen=True, slots=True)
class AuthSelectionRequest:
    observation: RepositoryIdentityObservation
    selector: AuthProfileSelector
    bindings: tuple[RepositoryBindingSnapshot, ...]
    profiles: tuple[CredentialProfileEligibility, ...]
    expected_binding_revision: Revision | None = None
```

Implement:

```python
def resolve_auth_profile(request: AuthSelectionRequest) -> RepositoryIdentityResolution: ...
```

For `auto`, delegate to the existing exact deterministic resolver. For an explicit ID, first require exactly one declaration with that ID, then run the same enabled/provider/role/pattern/binding/revision checks. Do not simulate explicit selection by silently removing competing profiles from the candidate set when an exact binding conflicts.

- [ ] **Step 3: Export lazily and run GREEN**

```bash
uv run pytest tests/test_auth_profile_selection.py tests/test_repository_identity_contracts.py -q
uv run mypy --strict src/repoforge/domain/auth_profile.py src/repoforge/domain/repository_identity_resolution.py
```

Expected: PASS.

- [ ] **Step 4: Commit Task 1**

```text
feat(identity): add deterministic auth selectors (#295)
```

---

## Task 2: Strict auth-profile configuration and source round trip

**Files:**

- Modify: `src/repoforge/config.py`
- Modify: `src/repoforge/application/configuration/source.py`
- Modify: `src/repoforge/application/configuration/document.py`
- Modify: `src/repoforge/application/configuration/acceptance.py` if source-to-resolved composition requires it
- Modify: `config.example.toml`
- Create: `tests/test_auth_profile_config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_configuration_source.py`

- [ ] **Step 1: Write RED parsing, validation, and round-trip tests**

Use literal TOML fixtures for one named `gh` account over HTTPS, one named `gh` account with pinned SSH transport, and one GitHub App profile. Cover:

- exact source → resolved → `AppConfig` round trip;
- stable ordering and preservation through unrelated repository refreshes;
- one profile usable without an explicit selector;
- duplicate IDs, unknown fields, secret-shaped references, raw token fields, invalid hosts, unsafe repository IDs, missing role data, cross-owner patterns, disabled profiles, invalid capability IDs, invalid app permissions, relative SSH identity files, multiple transport sources, and actor/source-kind mismatches;
- root `auth_profiles` remains distinct from repository verification `profiles`;
- commit identity profile IDs must refer to a compatible declared auth profile when auth profiles are configured;
- legacy configuration with no auth profiles still loads and yields a migration-required state rather than changing behavior silently.

Run:

```bash
uv run pytest tests/test_auth_profile_config.py tests/test_config.py tests/test_configuration_source.py -q
```

Expected: FAIL because auth-profile source/resolved configuration is unsupported.

- [ ] **Step 2: Add strict source declarations**

Add immutable source models:

```python
@dataclass(frozen=True, slots=True)
class SourceAuthProfile:
    profile_id: str
    provider: str
    credential_kind: str
    credential_reference: str
    actor_class: str
    expected_actor_id: str
    enabled: bool
    repository_id: str
    repository_patterns: tuple[str, ...]
    boundary_id: str
    capability_ids: tuple[str, ...]
    github_host: str
    github_login: str | None
    github_app_id: str | None
    github_installation_id: str | None
    github_permissions: tuple[str, ...]
    transport_kind: str
    ssh_identity_file: str | None
    https_token_environment: str | None
    credential_fingerprint: str
    allowed_access: tuple[str, ...]
    source_ssh_alias: str | None = None
    lease_seconds: int = 300
```

Extend `SourceConfiguration` with `auth_profiles`, parse `[auth_profiles.<id>]`, reject unknown fields, and render stable TOML. Update every constructor/copy helper so profiles cannot be dropped during repository add/remove/refresh.

- [ ] **Step 3: Add runtime configuration models**

Add `AuthProfileConfig` to `config.py`. It must expose constructed existing primitives rather than parallel loose dictionaries:

- `CredentialProfile`;
- `CredentialProfileEligibility`;
- exactly one of `StoredGhAccountSpec` or `GitHubAppInstallationSpec`;
- `GitTransportSpec`.

Store only opaque references and fingerprints. Do not read environment variables or credential files while parsing.

- [ ] **Step 4: Preserve auth profiles in resolved documents**

Update source-to-resolved composition and `render_resolved()` to include deterministic `[auth_profiles.<id>]` tables. Unknown or unsupported nested data must fail instead of being dropped by renderer allowlists.

- [ ] **Step 5: Run GREEN and strict type checks**

```bash
uv run pytest tests/test_auth_profile_config.py tests/test_config.py tests/test_configuration_source.py tests/test_config_admin.py -q
uv run mypy --strict src/repoforge/config.py src/repoforge/application/configuration/source.py src/repoforge/application/configuration/document.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```text
feat(identity): configure reviewed auth profiles (#295)
```

---

## Task 3: Isolated named-account/SSH discovery and migration plans

**Files:**

- Create: `src/repoforge/domain/auth_migration.py`
- Create: `src/repoforge/ports/auth_discovery.py`
- Modify: `src/repoforge/ports/__init__.py`
- Create: `src/repoforge/adapters/github/account_discovery.py`
- Modify: `src/repoforge/adapters/github/__init__.py`
- Create: `src/repoforge/adapters/git/ssh_alias_discovery.py`
- Modify: `src/repoforge/adapters/git/__init__.py`
- Create: `src/repoforge/application/auth_migration.py`
- Create: `tests/test_auth_import_adapters.py`
- Create: `tests/test_auth_migration.py`

- [ ] **Step 1: Write RED GitHub named-account tests**

Use a real scripted command executor boundary and complete `gh` JSON fixtures. Assert:

- every token request is exactly `gh auth token --hostname <host> --user <login>`;
- live actor verification uses that token through an isolated environment and cannot observe a different globally active account;
- missing/duplicate named accounts, actor mismatch, repository mismatch, SSO/policy denial, malformed JSON, timeout, and output overflow return typed safe conflicts;
- recorded commands never contain `auth switch`;
- token canaries are absent from candidates, plans, errors, reprs, and durable payloads.

- [ ] **Step 2: Write RED SSH alias tests**

Drive `ssh -G <alias>` with controlled output. Accept one concrete lowercase hostname, optional user, and one absolute identity file. Reject wildcards, multiple identity files, proxy commands/jumps, identity agents, unresolved `%`/`${}` expansion, relative paths, local/file transports, control characters, oversized output, and missing values.

Assert discovery is read-only and runtime output is a concrete pinned spec; no SSH config write command exists.

- [ ] **Step 3: Write RED migration-plan tests**

Cover detection of:

- legacy one-repository/no-profile configuration;
- one exact named-account candidate producing a no-prompt plan;
- multiple candidates producing manual remediation;
- globally active account differing from the named candidate without changing selection;
- ambient GitHub token variables;
- global/local credential helpers;
- global/local/worktree author or signer conflicts;
- ambiguous SSH configuration;
- remote host/owner/repository mismatch;
- deterministic plan ID/hash, bounded findings, source/config generation binding, and stale apply rejection.

Run:

```bash
uv run pytest tests/test_auth_import_adapters.py tests/test_auth_migration.py -q
```

Expected: FAIL because discovery ports/adapters and migration contracts do not exist.

- [ ] **Step 4: Implement safe migration domain**

Use:

```python
class AuthMigrationChangeKind(str, Enum):
    CREATE_PROFILE = "create_profile"
    CREATE_BINDING = "create_binding"
    PIN_TRANSPORT = "pin_transport"
    SET_COMMIT_IDENTITY = "set_commit_identity"
    MANUAL_REMEDIATION = "manual_remediation"

@dataclass(frozen=True, slots=True)
class AuthMigrationFinding: ...

@dataclass(frozen=True, slots=True)
class AuthMigrationChange: ...

@dataclass(frozen=True, slots=True)
class AuthMigrationPlan:
    plan_id: str
    plan_hash: str
    source_sha256: str
    config_generation: int
    findings: tuple[AuthMigrationFinding, ...]
    changes: tuple[AuthMigrationChange, ...]
    ready: bool
```

Canonical hashes contain safe metadata only. Every conflict has typed recovery and unchanged-state evidence.

- [ ] **Step 5: Implement narrow ports/adapters and planner**

Define discovery protocols that return candidates, not secrets. Use existing `EphemeralSecret` and isolated executor methods for exact token handling. Use bounded `git config --show-origin --get-all ...` reads for ambient conflict detection; never mutate Git config.

`AuthMigrationService.inspect()` creates a hash-bound plan. `apply()` rechecks source digest, generation, live repository observation, named account, and SSH alias evidence before producing a new source/resolved configuration mutation. It refuses plans containing manual remediation.

- [ ] **Step 6: Run GREEN and safety scans**

```bash
uv run pytest tests/test_auth_import_adapters.py tests/test_auth_migration.py tests/test_github_api_identity.py tests/test_git_transport_identity.py -q
uv run mypy --strict src/repoforge/domain/auth_migration.py src/repoforge/ports/auth_discovery.py src/repoforge/application/auth_migration.py src/repoforge/adapters/github/account_discovery.py src/repoforge/adapters/git/ssh_alias_discovery.py
```

Expected: PASS; no secret canary appears.

- [ ] **Step 7: Commit Task 3**

```text
feat(identity): import named accounts safely (#295)
```

---

## Task 4: AuthUxService, bindings, surfaces, doctor, and leases

**Files:**

- Create: `src/repoforge/application/auth_ux.py`
- Create: `src/repoforge/ports/auth_inspection.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/application/operations/identity.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Create: `tests/test_auth_ux_service.py`
- Modify: `tests/test_operation_identity_leases.py`
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Write RED profile, binding, and resolution facade tests**

With real JSON binding/identity stores where practical, cover list/inspect, enabled filtering, human/agent filtering, exact resolution, proposal-required output, bind create, one-role update, unbind one role, final-role behavior, stale revisions, idempotent repeats, and secret-free receipts.

The production mutation whose absence each test catches is the real store state change; do not assert only that a mock was called.

- [ ] **Step 2: Write RED identity-surface tests**

`whoami(check=all)` must return independent surfaces in stable order:

```text
repository_binding
api
transport
commit_author
commit_committer
commit_signer
publication
```

Cover `verified`, `configured`, `unobservable`, `blocked`, and `unavailable`. Assert transport success never upgrades API actor evidence, unsigned attestation never claims a signer, and overall readiness depends on every requested required surface.

- [ ] **Step 3: Write RED doctor and lease tests**

Cover disabled/ambiguous profiles, stale binding, missing reference, ambient conflict, expired/revoked lease, transport mismatch, author/signer drift, publication denial, typed recovery actions, safe unchanged state, operation identity inspection, exact revision revocation by lease ID/profile ID, and immutable non-selected leases.

Run:

```bash
uv run pytest tests/test_auth_ux_service.py tests/test_operation_identity_leases.py -q
```

Expected: FAIL because `AuthUxService` and inspection ports do not exist.

- [ ] **Step 4: Implement safe view contracts and facade**

Use frozen result types with `safe_payload()` methods:

```python
class AuthSurfaceState(str, Enum):
    VERIFIED = "verified"
    CONFIGURED = "configured"
    UNOBSERVABLE = "unobservable"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True, slots=True)
class AuthSurfaceEvidence: ...

@dataclass(frozen=True, slots=True)
class AuthWhoamiResult: ...

@dataclass(frozen=True, slots=True)
class AuthDoctorFinding: ...
```

`AuthUxService` consumes `AppConfig`, repository binding store, operation identity store/manager, live repository observer, narrow API/transport/commit/publication inspectors, migration service, clock, and IDs. It coordinates existing authorities; it does not publish.

- [ ] **Step 5: Wire production inspection dependencies**

Add optional keyword-only context fields to avoid positional shifts. Build repository bindings and operation identity manager from existing stores. Compose the named GitHub provider and pinned transport inspector only for configured profiles. Missing production dependencies produce `unavailable`, never ambient fallback.

- [ ] **Step 6: Run GREEN and integration tests**

```bash
uv run pytest tests/test_auth_ux_service.py tests/test_operation_identity_leases.py tests/test_integration.py -q
uv run mypy --strict src/repoforge/application/auth_ux.py src/repoforge/ports/auth_inspection.py src/repoforge/application/context.py src/repoforge/bootstrap.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```text
feat(identity): compose auth inspection UX (#295)
```

---

## Task 5: `rf auth` CLI and migration UX

**Files:**

- Create: `src/repoforge/interfaces/cli/auth.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Create: `tests/test_auth_cli.py`
- Modify: `tests/test_cli_surface_coverage.py`
- Modify: `tests/test_cli_command_bodies.py`

- [ ] **Step 1: Write RED parser and JSON/human golden tests**

Cover every command:

```text
rf auth profile list
rf auth profile inspect <profile>
rf auth bind
rf auth unbind
rf auth resolve
rf auth whoami --check all
rf auth doctor
rf auth lease inspect
rf auth lease revoke
rf auth import gh
rf auth import ssh
rf auth migrate inspect
rf auth migrate apply
```

Assert defaults are `auth_profile=auto`, `actor_class=human`; exactly one eligible profile requires no prompt; mutating commands require exact revisions or plan hashes; failure output uses the standard typed envelope; human output preserves separate surface labels.

- [ ] **Step 2: Write RED command-isolation tests**

Run handlers with recording executors and real temp stores. Assert no argv contains `gh auth switch`, no Git command contains `--global` or `--system` mutation, no SSH configuration path is opened for writing, and no token canary reaches captured stdout/stderr.

- [ ] **Step 3: Run RED CLI tests**

```bash
uv run pytest tests/test_auth_cli.py tests/test_cli_surface_coverage.py tests/test_cli_command_bodies.py -q
```

Expected: FAIL because `rf auth` is unregistered.

- [ ] **Step 4: Implement focused parser/dispatcher module**

Expose:

```python
def add_auth_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None: ...
def run_auth_command(args: argparse.Namespace, *, service: AuthUxService, render: Renderer) -> int: ...
```

Keep auth-specific parsing and command mapping out of the large `main.py`; register/import only at the existing CLI composition seam. Reuse the global JSON/human renderer and standard exception envelope.

- [ ] **Step 5: Run GREEN and CLI regression**

```bash
uv run pytest tests/test_auth_cli.py tests/test_cli_surface_coverage.py tests/test_cli_command_bodies.py -q
uv run mypy --strict src/repoforge/interfaces/cli/auth.py src/repoforge/interfaces/cli/main.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```text
feat(identity): add rf auth workflows (#295)
```

---

## Task 6: MCP selectors and immutable operation admission

**Files:**

- Modify: `src/repoforge/contracts/v2.py`
- Modify: `src/repoforge/contracts/common.py` if a shared selector model is used
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: relevant `src/repoforge/application/service.py` effectful methods
- Modify: relevant effectful command DTOs under `src/repoforge/application/workspace/` and `src/repoforge/application/repository/`
- Modify: `src/repoforge/contracts/generated_contract_identity.py`
- Modify: `docs/contracts/tool-schemas-v2.json`
- Modify: `docs/contracts/release-contract-v2.json`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Create: `tests/test_auth_mcp_selectors.py`
- Modify: `tests/test_v2_contract_models.py`
- Modify: `tests/test_mcp_contract_v2.py`
- Modify: `tests/test_v2_schema_golden.py`
- Modify: `tests/test_service_tools.py`
- Modify: `tests/test_operation_identity_leases.py`

- [ ] **Step 1: Write RED contract validation tests**

Add `auth_profile` and `actor_class` to effectful inputs only:

- workspace creation and remote refresh branches;
- workspace commit;
- workspace push;
- effectful workspace PR branches;
- effectful repo issue branches, including governed manage apply where applicable.

Defaults preserve existing callers. Read/watch branches reject selectors. Invalid/empty profile IDs and unknown actor classes fail closed. The public tool roster remains exactly 28 entries.

Run:

```bash
uv run pytest tests/test_auth_mcp_selectors.py tests/test_v2_contract_models.py tests/test_mcp_contract_v2.py -q
```

Expected: FAIL because selectors are forbidden extra fields.

- [ ] **Step 2: Write RED operation admission tests**

Cover:

- selector forwarded exactly from MCP to application command;
- identity resolved before the durable effect operation is admitted;
- unique one-profile default binds an operation context;
- ambiguity/missing/disabled/explicit conflict denies before effect boundary;
- switching configured profile between separate operations selects the new exact profile without global mutation;
- changing configuration/profile after an operation starts cannot alter its `OperationIdentityRecord`;
- lease refresh with changed profile/actor/repository/capability fails;
- read-only branches do not create identity sidecars.

- [ ] **Step 3: Implement shared contract selector and validators**

Use public values only:

```python
class AuthActorClass(str, Enum):
    HUMAN = "human"
    AGENT = "agent"

class AuthSelectionInput(StrictModel):
    auth_profile: Identifier | Literal["auto"] = "auto"
    actor_class: AuthActorClass = AuthActorClass.HUMAN
```

If inheritance would alter schema shape unexpectedly, use the constrained aliases directly on each effectful input. Multiplexed validators must explicitly accept selectors only on effectful modes/actions.

- [ ] **Step 4: Integrate admission before durable effects**

Map public selectors to `AuthProfileSelector`, call the application admission seam, and bind `OperationIdentityContext` exactly once before invoking push/PR/issue/publication effects. Commit uses the selected profile's commit identity and worktree-scoped gateway. Existing operations resume from their sidecar and never re-resolve selectors.

Do not add optional ambient fallback when the auth service is unavailable; return a typed unavailable/migration-required failure for configured multi-profile mode. Preserve legacy one-profile compatibility through deterministic synthesized selection only when exact evidence exists.

- [ ] **Step 5: Regenerate and review contract artifacts**

Use the repository's existing contract generator/validation command. Review the generated schema diff to ensure:

- only intended input schemas/digests changed;
- output schemas and tool count remain stable;
- no selectors appear on read-only tools/branches;
- generated identity matches runtime registry.

- [ ] **Step 6: Run GREEN contract and operation suites**

```bash
uv run pytest tests/test_auth_mcp_selectors.py tests/test_v2_contract_models.py tests/test_mcp_contract_v2.py tests/test_v2_schema_golden.py tests/test_service_tools.py tests/test_operation_identity_leases.py -q
uv run mypy --strict src/repoforge/contracts/v2.py src/repoforge/interfaces/mcp/server.py src/repoforge/application/service.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```text
feat(identity): bind MCP writes to auth profiles (#295)
```

---

## Task 7: Documentation, inventories, review, and final verification

**Files:**

- Modify: `docs/development/REPOSITORY_IDENTITY.md`
- Modify: `docs/development/REPOSITORY_IDENTITY_SURFACES.json`
- Modify: `tests/test-groups.toml`
- Modify: `tests/coverage-map.json`
- Modify: `scripts/select_affected_tests.py`
- Modify: any generated docs already owned by Task 6

- [ ] **Step 1: Add RED inventory/affected-selection tests if new files are not covered**

Assert every new source file maps to focused tests and capability groups, and every new test is assigned exactly as required by repository inventory validation.

- [ ] **Step 2: Update identity documentation and inventories**

Document CLI examples, configuration schema, exact selector behavior, separate identity surfaces, import/migration remediation, operation immutability, and prohibited global mutations. Keep docs secret-free.

- [ ] **Step 3: Run focused #295 suites**

```bash
uv run pytest \
  tests/test_auth_profile_selection.py \
  tests/test_auth_profile_config.py \
  tests/test_auth_import_adapters.py \
  tests/test_auth_migration.py \
  tests/test_auth_ux_service.py \
  tests/test_auth_cli.py \
  tests/test_auth_mcp_selectors.py -q
```

- [ ] **Step 4: Run identity/publication safety bundle**

```bash
uv run pytest \
  tests/test_repository_identity_contracts.py \
  tests/test_github_api_identity.py \
  tests/test_git_transport_identity.py \
  tests/test_operation_identity_leases.py \
  tests/test_commit_identity_governance.py \
  tests/test_publication_adapter.py \
  tests/test_publication_guards.py \
  tests/test_nested_identity_domain.py \
  tests/test_nested_identity_adapter.py \
  tests/test_nested_identity_coordinator.py \
  tests/test_nested_publication.py -q
```

- [ ] **Step 5: Run project gates**

Run Ruff format/check, strict Mypy, quick profile, manifest capability partitions, shipping tests, full regression partitions, wheel build, and isolated wheel install/e2e using allowed repository commands. Reuse durable operation receipts and do not rerun a lost-response operation until its terminal state is resolved.

- [ ] **Step 6: Main-agent review**

Review the exact full diff along both axes:

- standards: security boundaries, maintainability, test quality, error/receipt conventions, no secret/global mutation paths;
- specification: every #295 acceptance criterion, locked decision, compatibility case, and required test.

Fix every finding through a fresh RED/GREEN cycle.

- [ ] **Step 7: Run fresh `git diff --check`, commit, and post issue evidence**

```text
docs(identity): document auth profile workflows (#295)
```

Post #295 evidence with commit SHAs, exact test counts/operation receipts, schema/tool-count confirmation, build/install evidence, and explicit confirmation that global GitHub/Git/SSH state was untouched.
