# EPIC 284 Completion Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` task-by-task. This session is explicitly main-agent-only; do not create subagents, another workspace, or another branch.

**Goal:** Make PR #303 truthfully complete EPIC #284 by turning the existing repository-identity foundations into the default production path, making public selectors control the admitted identity, closing broker/race/restart gaps, and passing the exact production release gate.

**Architecture:** Keep the existing domain contracts and issue-specific modules. Add one application-level identity runtime that resolves a selector against the stable repository observation and durable binding, then opens a bounded `RepositoryAuthBroker` session for the selected configured profile. Effectful commands carry the immutable `AuthProfileSelector` to their first identity-aware boundary. Workspace publication executes synchronously inside that broker session, constructs the exact `AuthLease`, `OperationIdentityContext`, `PublicationAuthorization`, transport/API evidence and `PublicationRequest`, binds the operation sidecar once, and calls the existing `PublicationCoordinator`. Bootstrap constructs this stack by default whenever reviewed auth profiles are configured; test overrides remain explicit seams, not the only working path.

**Tech Stack:** Python 3.12, frozen/slotted dataclasses, Protocol boundaries, JSON durable state, GitHub CLI isolated execution, exact Git transport, pytest, Ruff, strict Mypy, RepoForge reviewed verification profiles.

## Global Constraints

- Work only in workspace `epic-284-repository-iden-9164d41f2f` on branch `ai/epic-284-repository-identity-9164d41f2f`.
- Preserve the existing sequential EPIC commit chain. Do not refresh/rebase while generated-path overlap remains; the branch is behind the live base by zero commits.
- Use the main agent only.
- Follow RED → observe the intended failure → GREEN → refactor for every behavior change.
- Do not use `gh auth switch`, ambient credential fallback, host-global Git configuration, inferred active accounts, raw-secret persistence, or secret-bearing diagnostics.
- A selector is consumed before external-effect admission. An already-bound operation identity cannot change profile, actor class, repository, target, or capability set.
- Keep repository observation, API actor, Git transport, commit identity, signer, and publication evidence independent.
- No new MCP tool and no public result field for credential references.
- Preserve existing test overrides, but add tests proving the standard `build_application()` path works without a publication or identity override.
- Run the focused tests listed in each task before committing that task. Do not defer all testing to the end.

---

### Task 1: Restore the authoritative durable-record gate

**Issues:** #286, #290, #291, #296

**Files:**
- Modify: `tests/test_durable_record_round_trip.py`
- Test: `tests/test_operation_identity_leases.py`
- Test: `tests/test_task_capsule_v2_persistence.py`

**Interfaces and invariants:**
- Every `StateCodec`-backed record type has one fully populated `RoundTripCase`.
- `OperationWorkerBinding.identity_context_id` and `.identity_context_digest` are populated in the generic fixture.
- `TaskCapsule.identity_contexts` is populated in the generic fixture.
- `RepositoryIdentityBinding` and `OperationIdentityRecord` survive create/read equality through their real JSON stores.

- [ ] **Step 1: Capture the current RED gate**

Run:

```bash
uv run pytest -q tests/test_durable_record_round_trip.py
```

Expected: failure reporting missing round-trip cases for `RepositoryIdentityBinding` and `OperationIdentityRecord`, plus unexercised required identity fields on worker/task fixtures.

- [ ] **Step 2: Add fully populated real-store fixtures**

Add:

```python
def _repository_identity_binding(tmp_path: Path) -> tuple[object, object]: ...
def _operation_identity_record(tmp_path: Path) -> tuple[object, object]: ...
```

Use `JsonRepositoryBindingStore` and `JsonOperationIdentityStore`, not direct codec calls. Build one exact stable GitHub binding and one operation record containing an active repository lease plus a non-empty `LeaseCapabilityRequest`.

Update `_worker_binding()` to carry a valid context ID/digest and `_task_capsule()` to carry the same `OperationIdentityReference`. Register both new record types in `_register()`.

- [ ] **Step 3: Run GREEN durability tests**

```bash
uv run pytest -q tests/test_durable_record_round_trip.py tests/test_operation_identity_leases.py tests/test_task_capsule_v2_persistence.py
```

Expected: pass.

- [ ] **Step 4: Commit the durability repair**

```text
test(identity): cover durable identity records (#296)
```

---

### Task 2: Close bind races and preserve typed broker failures

**Issues:** #287, #290

**Files:**
- Modify: `tests/test_operation_identity_leases.py`
- Modify: `tests/test_repository_identity_contracts.py`
- Modify: `src/repoforge/application/operations/identity.py`
- Modify: `src/repoforge/domain/repository_auth_broker.py`

**Interfaces and invariants:**
- One private equality predicate compares `reference`, full `context`, and exact `capability_requests` in both the pre-existing and `ALREADY_EXISTS` race branches.
- `RepositoryAuthBroker.session()` and `.refresh()` re-raise existing `RepoForgeError` values unchanged.
- Only unknown provider exceptions become `CREDENTIAL_BROKER_UNAVAILABLE`.
- Same-identity refresh ignores only the allowlisted renewable metadata key `github_preflight_observed_at`; capability, permission, repository, actor, installation, config and policy metadata remain locked.

- [ ] **Step 1: Write RED bind-race tests**

Create a store fake whose first `read()` returns `None`, whose `create()` raises `ALREADY_EXISTS`, and whose second `read()` returns a raced record. Assert a different capability request fails with `OPERATION_IDENTITY_MISMATCH`; assert an exact raced record is returned.

Run:

```bash
uv run pytest -q tests/test_operation_identity_leases.py -k "race"
```

Expected: the changed-capability race is incorrectly accepted.

- [ ] **Step 2: Write RED broker error and refresh tests**

Add provider fakes that raise a typed SSO/installation/actor error from `resolve()` and `refresh()`. Assert the exact error code, retryability and safe recovery survive. Add a refresh where only `github_preflight_observed_at` changes and another where a locked digest changes.

Run:

```bash
uv run pytest -q tests/test_repository_identity_contracts.py -k "broker and (typed or refresh)"
```

Expected: typed errors collapse to `CREDENTIAL_BROKER_UNAVAILABLE` and renewable metadata is rejected.

- [ ] **Step 3: Implement one shared exact-record comparison and immutable metadata projection**

Refactor `OperationIdentityManager.bind()` so both paths call the same helper. In the broker, use:

```python
_RENEWABLE_PROVIDER_METADATA = frozenset({"github_preflight_observed_at"})

def _provider_identity_metadata(...): ...
```

Catch `RepoForgeError` separately before the unknown-exception fallback.

- [ ] **Step 4: Run GREEN focused tests**

```bash
uv run pytest -q tests/test_operation_identity_leases.py tests/test_repository_identity_contracts.py tests/test_github_api_identity.py
```

Expected: pass.

- [ ] **Step 5: Commit the race/broker repair**

```text
fix(identity): lock operation and broker identity (#290)
```

---

### Task 3: Make selectors part of every effectful command

**Issue:** #295

**Files:**
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/application/repository/family_v2.py`
- Modify: `src/repoforge/application/repository/issue_mutation_v2.py`
- Modify: `src/repoforge/application/workspace/commit.py`
- Modify: `src/repoforge/application/workspace/push.py`
- Modify: `src/repoforge/application/workspace/pr.py`
- Modify: `src/repoforge/application/workspace/create_draft_pr.py`
- Modify: `src/repoforge/application/workspace/refresh.py`
- Modify: `src/repoforge/application/workspace/refresh_v2.py`
- Modify: `src/repoforge/application/workspace/create.py`
- Modify: `tests/test_auth_mcp_selectors.py`
- Modify: `tests/test_service_tools.py`

**Interfaces and invariants:**
- Effectful command dataclasses carry `selector: AuthProfileSelector` with the backward-compatible deterministic default.
- `CodingService` constructs the selector once and passes it into the command; it never validates and discards it.
- Read/watch branches retain no selector and cannot accidentally admit an identity.
- Nested coordinators preserve the exact selector when translating consolidated commands to specific commands.

- [ ] **Step 1: Write RED command-forwarding tests**

Use recording command handlers to assert explicit `auth_profile="personal"`, `actor_class="agent"` arrives as `AuthProfileSelector("personal", AGENT)` in workspace commit/push/PR/refresh/create and issue mutation commands. Add one consolidated PR create-draft test proving the selector reaches `WorkspaceCreateDraftPrCommand`.

Run:

```bash
uv run pytest -q tests/test_auth_mcp_selectors.py tests/test_service_tools.py -k "selector"
```

Expected: commands contain no selector or receive only the default.

- [ ] **Step 2: Add selector fields and propagate them without behavior fallback**

Add `selector: AuthProfileSelector = field(default_factory=AuthProfileSelector)` where positional compatibility matters. Replace every discarded `_auth_selector(...)` call with a local `selector` passed into the command. Carry it through consolidated command translation.

- [ ] **Step 3: Run GREEN contract and service tests**

```bash
uv run pytest -q tests/test_auth_mcp_selectors.py tests/test_service_tools.py tests/test_v2_contract_models.py tests/test_v2_shipping.py
```

Expected: pass and public tool count remains 28.

- [ ] **Step 4: Commit selector propagation**

```text
fix(identity): carry auth selectors into effects (#295)
```

---

### Task 4: Observe repository identity without the globally active `gh` account

**Issues:** #286, #295

**Files:**
- Modify: `src/repoforge/adapters/github/repository_observation.py`
- Modify: `src/repoforge/ports/auth_inspection.py`
- Modify: `src/repoforge/application/auth_ux.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_auth_repository_observation.py`
- Modify: `tests/test_auth_ux_service.py`

**Interfaces and invariants:**
- Local Git metadata supplies the configured remote URL/canonical host-owner-name without invoking `gh repo view` under ambient account state.
- Provider confirmation uses an explicitly selected profile session and its isolated `ProcessAuthContext`, or fails closed when no selected profile exists.
- No observation command inherits `GH_TOKEN`, `GH_HOST`, a gh configuration directory, SSH agent state, or an identity-bearing HOME.
- A wrong globally active account cannot alter the observed stable repository ID for a private repository.

- [ ] **Step 1: Write RED wrong-active-account tests**

Script an executor whose ambient HOME represents `wrong-user`, while the selected named account token can read repository `987654`. Assert observation uses the selected context and never calls `gh auth switch` or ambient `gh repo view`.

Run:

```bash
uv run pytest -q tests/test_auth_repository_observation.py -k "active or private or selected"
```

Expected: current observer invokes account-dependent `gh repo view` and preserves HOME.

- [ ] **Step 2: Split local discovery from selected provider confirmation**

Change the observer API to accept explicit observation authorization (selected `ProcessAuthContext` or a narrow callback that executes inside the selected broker session). Read remote URL with isolated local Git; call `gh api --hostname <host> repos/<owner>/<name>` with only selected auth context. Construct a temporary non-identity HOME/config directory where the executor contract requires HOME.

- [ ] **Step 3: Wire Auth UX observation through deterministic selection**

Bootstrap the UX observer with configured profile selection rather than a raw accountless observer. Legacy/no-profile configuration returns the existing migration-required typed failure instead of using active gh state.

- [ ] **Step 4: Run GREEN observation and UX tests**

```bash
uv run pytest -q tests/test_auth_repository_observation.py tests/test_auth_ux_service.py tests/test_auth_cli.py
```

Expected: pass.

- [ ] **Step 5: Commit account-independent observation**

```text
fix(identity): isolate repository observation (#295)
```

---

### Task 5: Add the production repository-identity runtime

**Issues:** #287, #288, #289, #293, #295

**Files:**
- Create: `src/repoforge/application/repository_identity_runtime.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/adapters/github/api_identity.py`
- Modify: `src/repoforge/adapters/git/transport.py`
- Modify: `tests/test_repository_identity_runtime.py`
- Modify: `tests/test_service_tools.py`

**Public application contracts:**

```python
@dataclass(frozen=True, slots=True)
class RepositoryIdentityAdmission:
    repo_id: str
    observation: RepositoryIdentityObservation
    profile: CredentialProfile
    binding: RepositoryIdentityBinding
    binding_revision: Revision
    selector: AuthProfileSelector

class RepositoryIdentityRuntime:
    def resolve(self, *, repo_id: str, selector: AuthProfileSelector) -> RepositoryIdentityAdmission: ...
    def session(
        self,
        admission: RepositoryIdentityAdmission,
        *,
        target_kind: AuthTargetKind,
        target_id: str,
        required_capability_ids: tuple[str, ...],
    ) -> AuthBrokerSession: ...
```

The runtime owns safe resolution and broker admission only. It does not publish and does not expose raw material.

- [ ] **Step 1: Write RED runtime selection/session tests**

Cover unique auto selection, explicit profile selection, exact binding conflict, actor-role mismatch, capability ceiling denial, typed SSO/preflight propagation, and simultaneous sessions for two profiles without global switching.

Run:

```bash
uv run pytest -q tests/test_repository_identity_runtime.py
```

Expected: module absent.

- [ ] **Step 2: Implement the minimal runtime over existing authorities**

Compose `resolve_auth_profile`, durable binding snapshots, configured `AuthProfileConfig`, `RepositoryAuthBroker`, and safe base environment allowlists. Require `RESOLVED`; a proposal is not silently persisted by an effectful operation.

- [ ] **Step 3: Build production provider and transport components in bootstrap**

For configured profiles, construct:

- `GhCliStoredAccountTokenSource` for stored accounts;
- `GhCliGitHubAppInstallationTokenIssuer` only when a configured signer is available, otherwise fail typed at use time rather than at unrelated read-only startup;
- `GhCliGitHubApiIdentityVerifier`;
- `GitHubApiAuthProvider`;
- `RepositoryAuthBroker`;
- `GitTransportRouter`;
- the existing capability-preflight gateway.

Store the runtime and transport router on `ApplicationContext`. Keep `AdapterOverrides` as exact test seams.

- [ ] **Step 4: Run GREEN runtime and identity adapter tests**

```bash
uv run pytest -q tests/test_repository_identity_runtime.py tests/test_github_api_identity.py tests/test_git_transport_identity.py tests/test_github_capability_preflight_adapter.py tests/test_service_tools.py
```

Expected: pass.

- [ ] **Step 5: Commit production identity composition**

```text
feat(identity): compose repository identity runtime (#287)
```

---

### Task 6: Implement exact production publication preparation

**Issue:** #292

**Files:**
- Create: `src/repoforge/application/workspace/publication_request_factory.py`
- Modify: `src/repoforge/application/workspace/publication.py`
- Modify: `src/repoforge/ports/workspace_publication.py`
- Modify: `src/repoforge/ports/publication.py`
- Modify: `src/repoforge/adapters/publication.py`
- Modify: `src/repoforge/adapters/github/gh_cli.py`
- Modify: `tests/test_workspace_publication_factory.py`
- Modify: `tests/test_publication_adapter.py`
- Modify: `tests/test_publication_guards.py`

**Interfaces and lifecycle:**
- Workspace publication inputs carry the exact `AuthProfileSelector`.
- The concrete service resolves admission, opens a broker session, derives a safe `AuthLease` from the active material, builds and binds the operation identity, and executes the existing coordinator before the session is released.
- The request factory resolves repository metadata by stable ID and explicit URL, derives exact topology/ref/commit/tree/remote version evidence, and builds `PublicationAuthorization` from fresh API/preflight/transport evidence.
- No `PublicationRequest` containing a secret-bearing `ProcessAuthContext` escapes the broker-session callback.

- [ ] **Step 1: Write RED factory tests**

Cover exact push and PR requests, selector-controlled profile, stable destination ID, exact refspec, active lease, capability requests, API and transport evidence, remote-version precondition, cross-boundary denial, and secret-free durable payloads.

Run:

```bash
uv run pytest -q tests/test_workspace_publication_factory.py
```

Expected: concrete factory/service absent.

- [ ] **Step 2: Add concrete repository resolver and authorization revalidator**

Implement stable-ID and URL resolution through isolated GitHub API calls bound to the active context. Implement write-time authorization revalidation by rerunning the exact capability preflight and comparing actor, installation, repository, capability, permission, config and policy evidence.

- [ ] **Step 3: Implement synchronous broker-scoped publication service**

Replace the factory interface that returns a secret-bearing request beyond session lifetime with an execution interface or scoped callback. Preserve the existing `WorkspacePublicationService` result contract and `PublicationCoordinator` as the durable effect authority.

- [ ] **Step 4: Run GREEN publication tests**

```bash
uv run pytest -q tests/test_workspace_publication_factory.py tests/test_publication_adapter.py tests/test_publication_guards.py tests/test_github_api_identity.py tests/test_git_transport_identity.py
```

Expected: pass.

- [ ] **Step 5: Commit exact publication preparation**

```text
feat(identity): prepare scoped publication identity (#292)
```

---

### Task 7: Make the production publication stack the bootstrap default

**Issues:** #292, #295

**Files:**
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/application/workspace/push.py`
- Modify: `src/repoforge/application/workspace/create_draft_pr.py`
- Modify: `src/repoforge/application/workspace/pr.py`
- Modify: `src/repoforge/application/repository/issue_mutation_v2.py`
- Modify: `tests/test_service_tools.py`
- Modify: `tests/test_v2_shipping.py`
- Modify: `tests/test_phases1_5_full_lifecycle.py`

**Interfaces and invariants:**
- Standard `build_application(config)` constructs `PublicationAdapter`, `PublicationCoordinator`, scoped workspace publication service, identity runtime and operation identity manager when reviewed auth profiles are present.
- `AdapterOverrides.publications` still replaces the full service for focused tests.
- A configured effectful push/PR/issue mutation works without manually injecting a publication or identity service.
- Legacy/no-profile configuration fails closed at the effect boundary with migration guidance, while read-only application startup remains available.

- [ ] **Step 1: Write RED default-composition integration tests**

Build an application with reviewed profiles and scripted provider/transport/API command results but no `publications` override. Assert `context.publications` and `context.repository_identity_runtime` are non-null and an explicit selector controls the profile recorded in operation/publication evidence. Add a legacy configuration test asserting a typed fail-closed effect.

Run:

```bash
uv run pytest -q tests/test_service_tools.py tests/test_v2_shipping.py -k "publication or identity_runtime or auth_profile"
```

Expected: `context.publications is None` or the selector has no effect.

- [ ] **Step 2: Compose and route the default stack**

Construct the stack before `ApplicationContext`, pass it into the context, and carry command selectors into workspace publication and issue-mutation identity admission. Do not let issue writes continue through the ambient `GhCliGateway` mutation path when an identity selector was supplied; use the selected API context.

- [ ] **Step 3: Run GREEN integration and lifecycle tests**

```bash
uv run pytest -q tests/test_service_tools.py tests/test_v2_shipping.py tests/test_phases1_5_full_lifecycle.py tests/test_auth_mcp_selectors.py
```

Expected: pass.

- [ ] **Step 4: Commit default production composition**

```text
feat(identity): activate production identity publication (#292)
```

---

### Task 8: Prove restart, nested routing and cross-profile isolation

**Issues:** #290, #291, #294, #296

**Files:**
- Modify: `tests/test_operation_identity_leases.py`
- Modify: `tests/test_task_capsule_v2_persistence.py`
- Modify: `tests/test_nested_identity_coordinator.py`
- Modify: `tests/test_nested_publication.py`
- Modify: `tests/test_phase5_failure_harness.py`
- Modify: `tests/test_phase6_operational_hardening.py`
- Modify: `scripts/live-activation-sandbox.sh`
- Modify: `tests/test-groups.toml`
- Modify: `tests/coverage-map.json`

**Required scenarios:**
- restart reads the same binding, operation identity, worker binding and task identity references;
- worker handoff refuses missing/mismatched identity context;
- two concurrent operations use distinct named accounts and never exchange contexts or leases;
- nested submodule/LFS/package/release targets never inherit the primary profile;
- lease expiry/revocation, lost response, process crash and stale CAS remain fail closed;
- every durable/log/model-visible artifact is scanned for token/private-key/authorization canaries.

- [ ] **Step 1: Add RED cross-process and fault cases**

Add focused fixtures for restart and parallel profile use. Extend the failure harness with wrong-profile, expired lease, provider unavailable, preflight drift, post-effect lost response and worker crash cases.

- [ ] **Step 2: Implement only missing lifecycle glue**

Propagate identity references into task capsules and worker bindings at admission/handoff; recover exact references on restart. Reject missing or mismatched references before a worker or nested effect runs.

- [ ] **Step 3: Run GREEN operational matrix**

```bash
uv run pytest -q tests/test_operation_identity_leases.py tests/test_task_capsule_v2_persistence.py tests/test_nested_identity_coordinator.py tests/test_nested_publication.py tests/test_phase5_failure_harness.py tests/test_phase6_operational_hardening.py
```

Expected: pass.

- [ ] **Step 4: Update test inventory and commit**

```text
test(identity): prove restart and profile isolation (#296)
```

---

### Task 9: Regenerate contracts, verify the exact release fingerprint and reconcile PR/EPIC state

**Issue:** #296 and EPIC #284

**Files:**
- Modify: `docs/development/REPOSITORY_IDENTITY.md`
- Modify: `docs/development/REPOSITORY_IDENTITY_SURFACES.json`
- Modify: `docs/contracts/tool-schemas-v2.json` only through the repository generator if changed
- Modify: `docs/contracts/release-contract-v2.json` only through the repository generator if changed
- Modify: PR #303 body and issue dispositions through RepoForge tools after evidence is green

- [ ] **Step 1: Update maintained architecture and acceptance evidence**

Document default production composition, selector admission, broker-scoped secret lifetime, exact publication flow, wrong-active-account isolation, renewable metadata rule, restart identity references and failure semantics. Ensure each child #285–#296 has concrete test/file/receipt evidence.

- [ ] **Step 2: Regenerate reviewed artifacts if source schemas changed**

Use the repository-declared generation command. Review the generated diff and verify the public tool count remains 28.

- [ ] **Step 3: Format and run affected verification**

```bash
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy --strict src/repoforge
```

Then run RepoForge profile `test-affected` on the exact tree.

- [ ] **Step 4: Run the authoritative production gate**

Run RepoForge profile `verify` on the exact current HEAD/fingerprint. Do not treat a stale receipt or a different SHA as completion. Required result: every static, business-test, security, schema, live-activation and release-fingerprint step passes.

- [ ] **Step 5: Review the full diff and reconcile PR #303**

Confirm no raw secrets, no ambient identity fallback, no unrequested refactor, and no issue acceptance gap. Update the PR body to the actual completed children, push the verified branch, watch checks, remove draft status only when supported by the available governed PR action, and apply issue closures only with exact acceptance evidence.

- [ ] **Step 6: Final completion criteria**

EPIC #284 may be reported complete only when:

1. the standard production root constructs and uses the identity/publication stack;
2. explicit and automatic selectors produce deterministic, test-observed identity choices;
3. all children #285–#296 have acceptance evidence;
4. PR #303 checks are green on the pushed merge HEAD;
5. the authoritative production gate is green on that exact fingerprint;
6. PR and issue state truthfully match the verified implementation.
