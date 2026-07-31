# Nested Identity Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement issue #294 so every submodule, Git LFS, package, and release endpoint is discovered or supplied explicitly, resolved to an exact stable target, and routed through its own least-privilege operation lease without inheriting the primary repository credential.

**Architecture:** Add a pure `nested_identity` domain model, a bounded read-only Git discovery adapter behind a small port, an application coordinator that composes all child leases before the durable identity sidecar is bound, and an exact nested-publication review seam. Reuse `AuthLease`, `OperationIdentityContext`, `LeaseCapabilityRequest`, `OperationIdentityManager`, `PublicationIntent`, and existing receipt/idempotency authorities rather than creating a parallel credential or publication system.

**Tech Stack:** Python 3.12, frozen slotted dataclasses, `Enum`, `Protocol`, `urllib.parse`, existing `CommandExecutor`, durable operation identity sidecars, pytest, Ruff, strict Mypy.

## Global Constraints

- Work only in workspace `epic-284-repository-iden-9164d41f2f` on branch `ai/epic-284-repository-identity-9164d41f2f`; do not refresh/rebase while generated-contract overlap with `main` remains.
- Use the main agent and execute this plan inline. Do not create another workspace, branch, or subagent.
- Follow red → green → refactor for every public seam. Record the expected red failure before production code is added.
- Never fetch, initialize, update, smudge, install, upload, publish, or consult a credential helper during discovery.
- Never use global `gh` account switching, host-global Git config, ambient SSH/package credentials, environment tokens, or raw secrets.
- Stable repository IDs bind repository-like targets. Canonical endpoints are safe discovery evidence and are persisted only as SHA-256 digests where a full endpoint is unnecessary.
- Anonymous access is read-only, explicit, and policy-gated. It cannot produce a lease or be upgraded to a write.
- An already-bound `OperationIdentityContext` is immutable except for the existing same-identity lease refresh lifecycle. Newly discovered targets require a new operation.
- All external writes require an exact intent and immediate pre-effect revalidation. Redirects, transfers, owner-boundary changes, capability drift, or payload drift fail before effect.
- Do not add dependencies or MCP tools for this issue.
- After each slice, run its focused tests and the repository identity safety bundle: `tests/test_repository_identity_contracts.py`, `tests/test_operation_identity_leases.py`, and `tests/test_publication_guards.py`.

---

## Task 1: Pure nested target and routing contracts

**Files:**

- Create: `src/repoforge/domain/nested_identity.py`
- Modify: `src/repoforge/domain/__init__.py`
- Create: `tests/test_nested_identity_domain.py`

- [ ] **Step 1: Write failing constructor and routing matrix tests**

Cover:

- safe candidates for `submodule`, `lfs`, `package`, and `release` with read/write access;
- endpoint canonicalization and digest stability;
- rejection of control characters, URL userinfo, local/file transports, unsafe IDs, duplicate capabilities, excessive depth, and non-SHA-256 digests;
- company-primary matrices for public, company-private, and personal-private targets;
- explicit anonymous-read allow/deny;
- exact binding success and missing/ambiguous/stale/disabled/transferred binding denial;
- write without exact publication intent denial;
- cross-boundary default deny and exact-approval allow;
- proof that the primary profile is never used as fallback evidence;
- safe payloads containing no credential references, tokens, raw authorization headers, or private endpoint userinfo.

Run:

```bash
uv run pytest tests/test_nested_identity_domain.py -q
```

Expected: FAIL because `repoforge.domain.nested_identity` does not exist.

- [ ] **Step 2: Implement the minimal pure domain model**

Use these public contracts:

```python
class NestedResourceKind(str, Enum):
    SUBMODULE = "submodule"
    LFS = "lfs"
    PACKAGE = "package"
    RELEASE = "release"

class NestedAccess(str, Enum):
    READ = "read"
    WRITE = "write"

class NestedBindingState(str, Enum):
    EXACT = "exact"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    STALE = "stale"
    DISABLED = "disabled"
    TRANSFERRED = "transferred"

class NestedRoutingStatus(str, Enum):
    BOUND_PROFILE = "bound_profile"
    ANONYMOUS_READ = "anonymous_read"
    DENIED = "denied"

@dataclass(frozen=True, slots=True)
class NestedResourceCandidate:
    kind: NestedResourceKind
    access: NestedAccess
    canonical_endpoint: str
    source_location: str
    depth: int
    endpoint_digest: str

@dataclass(frozen=True, slots=True)
class NestedResourceTarget:
    kind: NestedResourceKind
    access: NestedAccess
    provider: RepositoryProvider
    provider_host: str
    target_kind: AuthTargetKind
    target_id: str
    repository_id: str | None
    owner_boundary: str
    primary_owner_boundary: str
    capability_ids: tuple[str, ...]
    endpoint_digest: str
    binding_state: NestedBindingState
    profile_id: str | None
    public_read: bool

@dataclass(frozen=True, slots=True)
class NestedRoutingDecision:
    status: NestedRoutingStatus
    target_kind: AuthTargetKind
    target_id: str
    profile_id: str | None
    capability_ids: tuple[str, ...]
    endpoint_digest: str
    failure_code: RepositoryAuthFailureCode | None
    recovery_actions: tuple[RecoveryAction, ...]

@dataclass(frozen=True, slots=True)
class NestedIdentityReceipt:
    target_kind: AuthTargetKind
    target_id: str
    repository_id: str | None
    endpoint_digest: str
    routing_status: NestedRoutingStatus
    profile_id: str | None
    lease_id: str | None
    capability_ids: tuple[str, ...]
    source_locations: tuple[str, ...]
```

Implement:

```python
def canonical_nested_endpoint(value: str, *, base_endpoint: str | None = None) -> str: ...
def nested_endpoint_digest(canonical_endpoint: str) -> str: ...
def route_nested_resource(
    target: NestedResourceTarget,
    *,
    allow_anonymous_public_read: bool,
    exact_cross_boundary_approval_id: str | None,
    publication_intent_id: str | None,
) -> NestedRoutingDecision: ...
```

Routing must derive least-privilege capabilities from the resolved target; it must not inspect a primary lease/profile. Denials use `RepositoryAuthFailureCode.NESTED_RESOURCE_BINDING_REQUIRED` or `NESTED_RESOURCE_DENIED` plus only safe recovery actions (`RECONCILE_BINDING`, `REAUTHORIZE`, `REQUEST_CAPABILITY`, `ABORT`).

- [ ] **Step 3: Export the public values lazily and run green tests**

Add the new public types/functions to `repoforge.domain._EXPORTS` without introducing eager import cycles.

Run:

```bash
uv run pytest tests/test_nested_identity_domain.py tests/test_repository_identity_contracts.py -q
uv run mypy --strict src/repoforge/domain/nested_identity.py
```

Expected: PASS.

- [ ] **Step 4: Commit the domain slice**

```bash
git add src/repoforge/domain/nested_identity.py src/repoforge/domain/__init__.py tests/test_nested_identity_domain.py
git commit -m "feat(identity): add nested target routing contracts (#294)"
```

---

## Task 2: Bounded recursive submodule and LFS discovery

**Files:**

- Create: `src/repoforge/ports/nested_identity.py`
- Modify: `src/repoforge/ports/__init__.py`
- Create: `src/repoforge/adapters/git/nested_identity.py`
- Modify: `src/repoforge/adapters/git/__init__.py`
- Create: `tests/test_nested_identity_adapter.py`

- [ ] **Step 1: Write failing public-adapter tests**

Build temporary repositories and a scripted executor to cover:

- `.gitmodules` parsed through bounded `git config --file ... --get-regexp` commands;
- absolute HTTPS, reviewed SSH, and relative submodule URLs;
- recursive discovery only for already-checked-out submodule directories;
- deterministic de-duplication evidence for repeated endpoints;
- nested path normalization, path escape rejection, duplicate path rejection, cycles, depth limit, resource-count limit, command timeout/failure, invalid UTF-8/output overflow;
- default LFS endpoint and reviewed `.lfsconfig` `lfs.url` override;
- no command containing `fetch`, `submodule update`, `git lfs`, `credential`, `smudge`, or `upload`;
- no reads from home-directory config or inherited environment.

Run:

```bash
uv run pytest tests/test_nested_identity_adapter.py -q
```

Expected: FAIL because the discovery port/adapter do not exist.

- [ ] **Step 2: Define the discovery port and bounds**

Use these contracts:

```python
@dataclass(frozen=True, slots=True)
class NestedDiscoveryRequest:
    root: Path
    primary_endpoint: str
    submodule_access: NestedAccess = NestedAccess.READ
    lfs_access: NestedAccess = NestedAccess.READ
    max_depth: int = 8
    max_resources: int = 64
    max_output_bytes: int = 262_144
    command_timeout_seconds: int = 10

class NestedResourceDiscovery(Protocol):
    def discover(self, request: NestedDiscoveryRequest) -> tuple[NestedResourceCandidate, ...]: ...

class NestedTargetResolver(Protocol):
    def resolve(self, candidate: NestedResourceCandidate) -> NestedResourceTarget: ...

class NestedLeaseProvider(Protocol):
    def acquire(
        self,
        *,
        operation_id: str,
        actor_class: ActorClass,
        target: NestedResourceTarget,
        profile_id: str,
        capability_ids: tuple[str, ...],
        config_revision: str,
        policy_revision: str,
        now: str,
    ) -> AuthLease: ...
```

Validate positive integer bounds and an absolute root in `NestedDiscoveryRequest`.

- [ ] **Step 3: Implement `GitNestedResourceDiscovery`**

The adapter may use only `CommandExecutor.run`/`run_bytes`, `pathlib`, and the pure canonicalizer. Each command must set the request timeout and output bound. Parse `.gitmodules` as paired `submodule.<name>.path`/`.url` keys, resolve each path under the current reviewed root, and recurse only when the path is a real directory containing a checked-out Git worktree marker. Track resolved path ancestry plus `(kind, endpoint_digest)` ancestry for cycles.

For LFS, inspect only repository-local reviewed config and `.lfsconfig`; represent the effective endpoint as a candidate and never invoke Git LFS itself. Return candidates sorted by `(depth, kind.value, source_location, endpoint_digest)`.

Raise `RepoForgeError` with existing `SECURITY_POLICY_VIOLATION`, `COMMAND_FAILED`, or `COMMAND_TIMEOUT` codes, safe endpoint digests/source locations, and `unchanged_state=("No nested credentialed action was admitted.",)`.

- [ ] **Step 4: Run focused adapter and safety tests**

```bash
uv run pytest tests/test_nested_identity_adapter.py tests/test_repository_identity_contracts.py -q
uv run mypy --strict src/repoforge/ports/nested_identity.py src/repoforge/adapters/git/nested_identity.py
```

Expected: PASS.

- [ ] **Step 5: Commit the discovery slice**

```bash
git add src/repoforge/ports/nested_identity.py src/repoforge/ports/__init__.py src/repoforge/adapters/git/nested_identity.py src/repoforge/adapters/git/__init__.py tests/test_nested_identity_adapter.py
git commit -m "feat(identity): discover bounded nested resources (#294)"
```

---

## Task 3: Compose and bind child leases before effects

**Files:**

- Create: `src/repoforge/application/nested_identity.py`
- Create: `tests/test_nested_identity_coordinator.py`
- Modify: `tests/test_operation_identity_leases.py`

- [ ] **Step 1: Write failing coordinator tests**

Use fake discovery/resolver/lease-provider implementations and the real `OperationIdentityManager` with in-memory/JSON stores. Cover:

- mixed public/company-private/personal-private targets;
- one child lease per distinct `(target_kind, target_id)` and no lease for anonymous read;
- repeated endpoint de-duplication while preserving all source locations;
- unchanged primary leases and their original capability requests;
- exact child target/profile/provider/repository/capability/config/policy validation;
- binding the complete context once before returning;
- idempotent retry/resume with the same discovery result;
- a new target, changed endpoint, changed profile, or changed capability after bind fails with `OPERATION_IDENTITY_MISMATCH`;
- missing/ambiguous/stale/disabled/transferred targets fail before lease acquisition;
- lease expiry/revocation and exact child capability enforcement through `require_write`;
- restart from JSON sidecar and secret-free result/receipt payloads.

Run:

```bash
uv run pytest tests/test_nested_identity_coordinator.py -q
```

Expected: FAIL because `NestedIdentityCoordinator` does not exist.

- [ ] **Step 2: Implement preparation request/result contracts**

Use:

```python
@dataclass(frozen=True, slots=True)
class NestedIdentityPreparationRequest:
    identity_context: OperationIdentityContext
    identity_context_id: str
    primary_capability_requests: tuple[LeaseCapabilityRequest, ...]
    discovery: NestedDiscoveryRequest
    explicit_candidates: tuple[NestedResourceCandidate, ...] = ()
    allow_anonymous_public_read: bool = False
    cross_boundary_approvals: tuple[tuple[str, str], ...] = ()
    publication_intent_ids: tuple[tuple[str, str], ...] = ()

@dataclass(frozen=True, slots=True)
class NestedIdentityPreparation:
    record: OperationIdentityRecord
    context: OperationIdentityContext
    capability_requests: tuple[LeaseCapabilityRequest, ...]
    receipts: tuple[NestedIdentityReceipt, ...]
```

Approval and intent maps are keyed by exact `target_id`, must be unique, and may not contain entries for undiscovered targets.

- [ ] **Step 3: Implement coordinator orchestration**

```python
class NestedIdentityCoordinator:
    def __init__(
        self,
        *,
        discovery: NestedResourceDiscovery,
        resolver: NestedTargetResolver,
        leases: NestedLeaseProvider,
        identities: OperationIdentityManager,
        clock: Clock,
    ) -> None: ...

    def prepare(self, request: NestedIdentityPreparationRequest) -> NestedIdentityPreparation: ...
```

Algorithm:

1. Discover and append explicit reviewed candidates.
2. Group by `(kind, endpoint_digest)` and retain sorted source locations.
3. Resolve each representative independently.
4. Route with only exact approval/intent entries for that target.
5. Abort on the first deterministic denied decision before acquiring any later lease.
6. Acquire and validate one child lease for each bound decision.
7. Append child leases/requests deterministically to the unchanged primary context.
8. Call `OperationIdentityManager.bind` once with the complete context.
9. Return safe receipts tied to the returned record.

If any acquired lease differs in target kind/ID, stable repository ID, provider, profile, config revision, or policy revision, raise `CREDENTIAL_SCOPE_MISMATCH` before binding. Never recover by substituting the primary lease.

- [ ] **Step 4: Run coordinator, lifecycle, and safety tests**

```bash
uv run pytest tests/test_nested_identity_coordinator.py tests/test_operation_identity_leases.py tests/test_repository_identity_contracts.py -q
uv run mypy --strict src/repoforge/application/nested_identity.py
```

Expected: PASS.

- [ ] **Step 5: Commit the coordinator slice**

```bash
git add src/repoforge/application/nested_identity.py tests/test_nested_identity_coordinator.py tests/test_operation_identity_leases.py
git commit -m "feat(identity): bind exact nested resource leases (#294)"
```

---

## Task 4: Exact package, LFS, and release write review

**Files:**

- Create: `src/repoforge/domain/nested_publication.py`
- Modify: `src/repoforge/domain/__init__.py`
- Modify: `src/repoforge/application/publication.py`
- Create: `tests/test_nested_publication.py`
- Modify: `tests/test_publication_guards.py`

- [ ] **Step 1: Write failing exact-intent and TOCTOU tests**

Cover:

- package and LFS writes require explicit exact `NestedPublicationIntent`;
- release writes use `PublicationIntent(kind=PublicationKind.RELEASE)` and exact destination repository/target/ref/object digests;
- lease target, endpoint digest, capability digest, permission digest, config revision, policy revision, and payload digest must match review evidence;
- target redirect, repository transfer, endpoint change, capability/permission drift, payload drift, approval drift, expired/revoked lease, and operation mismatch fail before effect;
- existing push and PR validation remains unchanged;
- release request validation rejects PR body data and requires release target kind/capability;
- safe review payloads contain separate lease IDs and no secrets.

Run:

```bash
uv run pytest tests/test_nested_publication.py tests/test_publication_guards.py -q
```

Expected: FAIL because nested publication contracts/review do not exist and release requests are not admitted.

- [ ] **Step 2: Implement nested publication contracts and pure review**

Use:

```python
@dataclass(frozen=True, slots=True)
class NestedPublicationIntent:
    publication_id: str
    operation_id: str
    resource_kind: NestedResourceKind
    source_repository_id: str
    destination_target_id: str
    endpoint_digest: str
    payload_digest: str
    capability_digest: str
    permission_digest: str
    cross_boundary_approval_id: str | None = None

@dataclass(frozen=True, slots=True)
class ReviewedNestedPublication:
    intent: NestedPublicationIntent
    lease_id: str
    profile_id: str
    repository_id: str
    target_kind: AuthTargetKind
    target_id: str
    config_revision: str
    policy_revision: str
    review_digest: str

def review_nested_publication(
    intent: NestedPublicationIntent,
    *,
    target: NestedResourceTarget,
    lease: AuthLease,
    capability_request: LeaseCapabilityRequest,
    observed_capability_digest: str,
    observed_permission_digest: str,
    now: str,
) -> ReviewedNestedPublication: ...

def revalidate_nested_publication(
    reviewed: ReviewedNestedPublication,
    *,
    intent: NestedPublicationIntent,
    target: NestedResourceTarget,
    lease: AuthLease,
    capability_request: LeaseCapabilityRequest,
    observed_capability_digest: str,
    observed_permission_digest: str,
    now: str,
) -> ReviewedNestedPublication: ...
```

Restrict `NestedPublicationIntent.resource_kind` to `LFS` or `PACKAGE`; releases continue to use `PublicationIntent`. Review must require `NestedAccess.WRITE`, exact target/lease match, active unexpired lifecycle at the consuming application boundary, and at least one exact write capability.

- [ ] **Step 3: Admit exact release intent without creating a general release tool**

Update `PublicationRequest.__post_init__` and `PublicationCoordinator._action` so `PublicationKind.RELEASE` is structurally valid only when:

- `pull_request is None`;
- the pinned authorization lease is `AuthTargetKind.RELEASE`;
- the request capability is the exact release-write capability;
- `PublicationIntent` source/destination IDs, exact tag ref, commit, and tree continue through existing inspect/revalidate/idempotency/receipt logic.

Use internal action `workspace_publish_release`; do not add an MCP surface or implement asset generation. The injected `PublicationGateway` remains responsible for the reviewed provider effect, which keeps this issue at the contract/revalidation seam.

- [ ] **Step 4: Run focused publication and regression tests**

```bash
uv run pytest tests/test_nested_publication.py tests/test_publication_guards.py tests/test_publication_adapter.py -q
uv run mypy --strict src/repoforge/domain/nested_publication.py src/repoforge/application/publication.py
```

Expected: PASS, including the existing 40 publication adapter/guard tests.

- [ ] **Step 5: Commit the publication slice**

```bash
git add src/repoforge/domain/nested_publication.py src/repoforge/domain/__init__.py src/repoforge/application/publication.py tests/test_nested_publication.py tests/test_publication_guards.py
git commit -m "feat(identity): guard nested publication targets (#294)"
```

---

## Task 5: Production composition, inventories, documentation, and final gates

**Files:**

- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `docs/development/REPOSITORY_IDENTITY.md`
- Modify: `docs/development/REPOSITORY_IDENTITY_SURFACES.json`
- Modify: `tests/test_repository_identity_contracts.py`
- Modify: `tests/test-groups.toml`
- Modify: `tests/coverage-map.json`

- [ ] **Step 1: Add a failing composition/architecture test**

Assert:

- production context exposes explicit `NestedResourceDiscovery`, `NestedTargetResolver`, and `NestedLeaseProvider` dependencies;
- bootstrap provides `GitNestedResourceDiscovery` by default but provides no ambient target resolver or lease-provider fallback;
- private/write preparation fails closed when resolver/provider dependencies are unavailable;
- public anonymous reads can still be represented without a credential provider;
- no Git/provider/subprocess adapter may issue a nested fetch/write without importing or receiving the reviewed coordinator contracts;
- the identity surface inventory contains the new Git discovery adapter with accurate executable, target, credential, risk, and owner fields.

Run:

```bash
uv run pytest tests/test_repository_identity_contracts.py -q
```

Expected: FAIL on missing composition and inventory entries.

- [ ] **Step 2: Wire explicit dependencies without ambient authority**

Add optional keyword-only fields to `ApplicationContext`/`AdapterOverrides` for nested discovery, target resolution, and lease acquisition. Construct only `GitNestedResourceDiscovery(command)` by default. Leave resolver/lease provider unavailable unless a reviewed provider composition supplies them; coordinator construction must reject private/write routing when either is missing.

Do not alter tool count, MCP schemas, generated contracts, or operator configuration in this issue.

- [ ] **Step 3: Update documentation and maintained test metadata**

Document discovery bounds, anonymous reads, exact child leases, immutable operation binding, nested write intents, failure recovery, and explicit production dependency behavior in `REPOSITORY_IDENTITY.md`.

Add `src/repoforge/adapters/git/nested_identity.py` to `REPOSITORY_IDENTITY_SURFACES.json` with:

- executable: `git`;
- surfaces: `submodule`, `lfs`, `nested_resource_discovery`;
- credential inputs: none during discovery;
- target inputs: reviewed primary endpoint, repository-local `.gitmodules`/`.lfsconfig`, explicit bounds;
- risk: `reviewed_read_only`;
- owner: `#294`.

Add the four new test files exactly once under `groups.github_provider`; add new source globs for the domain, port, adapter, coordinator, and nested publication modules. Add direct source-to-test entries to `tests/coverage-map.json`.

- [ ] **Step 4: Run format, metadata, focused, and quick gates**

```bash
uv run ruff format --check src/repoforge/domain/nested_identity.py src/repoforge/domain/nested_publication.py src/repoforge/ports/nested_identity.py src/repoforge/adapters/git/nested_identity.py src/repoforge/application/nested_identity.py tests/test_nested_identity_domain.py tests/test_nested_identity_adapter.py tests/test_nested_identity_coordinator.py tests/test_nested_publication.py
uv run python scripts/select_affected_tests.py --check-completeness
uv run pytest tests/test_nested_identity_domain.py tests/test_nested_identity_adapter.py tests/test_nested_identity_coordinator.py tests/test_nested_publication.py tests/test_repository_identity_contracts.py tests/test_operation_identity_leases.py tests/test_publication_adapter.py tests/test_publication_guards.py -q
```

Then run RepoForge's `quick` verification profile against the exact workspace fingerprint. Expected: PASS.

- [ ] **Step 5: Review the full issue diff on both axes**

Standards review:

- architecture layering and dependency direction;
- bounded I/O/timeouts and deterministic ordering;
- type completeness and stable payloads;
- no duplicated policy, ambient credentials, raw-secret persistence, or implicit write target;
- test isolation and metadata completeness.

Spec review:

- every #294 acceptance criterion and locked decision has direct code/test evidence;
- all public/company/private, submodule/LFS/package/release, anonymous, cross-boundary, lifecycle, TOCTOU, and leak matrices are represented;
- no #295 CLI migration or #296 release-gate scope has leaked into this patch.

Fix every valid finding and rerun the affected focused tests.

- [ ] **Step 6: Run authoritative final verification once**

Run `git diff --check`, the exact focused bundle above, then `./scripts/test-all.sh` once through the durable RepoForge verification path. If the runner hits `COMMAND_TIMEOUT` without test failure, partition only the unfinished manifest groups and preserve successful operation evidence instead of rerunning completed suites.

Expected: all relevant checks terminal-success with exact HEAD/fingerprint evidence.

- [ ] **Step 7: Commit implementation and publish issue evidence**

```bash
git add src/repoforge docs/development tests
git commit -m "feat(identity): route nested resource identities (#294)"
```

Post one #294 evidence comment containing commit SHA, exact changed-file scope, focused/quick/full gate results, threat-model outcome, live checks not run, and confirmation that no MCP tool/config migration was introduced. Do not close the issue when repository policy does not expose issue closure.

---

## Post-#294 sequence

After #294 has a clean committed tree and published evidence:

1. Re-read #295, its comments, blockers, current code, and recent commits; run brainstorming/design approval before implementation.
2. Implement and verify #295 (`rf auth` CLI/MCP/account import/migration UX) on the same EPIC branch with its own design, plan, TDD slices, review, commit, and evidence comment.
3. Re-read #296 only after #294 and #295 evidence is complete; implement cross-profile release gates last, verify the EPIC-wide matrix, commit, and publish final EPIC evidence.
