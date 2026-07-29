# GitHub Capability Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact, operation-scoped GitHub capability and enterprise-policy preflight evidence, bind it into auth leases and publication receipts, and fail closed before external effects when evidence is denied, unavailable, stale, or unobservable.

**Architecture:** Keep the existing ambient-session doctor probe unchanged. Introduce a dedicated domain model, isolated GitHub preflight adapter, and explicit port used by `GitHubApiAuthProvider` and publication revalidation. Only the exact capability set admitted by the operation is probed; safe deterministic digests are propagated through `AuthMaterial.provider_metadata`, `AuthLease`, `PublicationAuthorization`, and durable publication evidence.

**Tech Stack:** Python 3.12, frozen dataclasses, enums, SHA-256 canonical JSON digests, `CommandExecutor.run_isolated`, GitHub REST/GraphQL through `gh api`, pytest, Ruff, strict Mypy, RepoForge durable operation and publication contracts.

## Global Constraints

- No global `gh auth switch`.
- No host-global Git configuration mutation.
- No ambient credential fallback.
- No raw credential material in logs, state, diagnostics, exceptions, fixtures, receipts, or model-visible output.
- Issue #51 remains authoritative for capability admission; this implementation supplies GitHub-specific evidence only.
- Only `proven_available` authorizes an affected external write.
- Missing, unavailable, stale, or unobservable evidence denies the affected write.
- Preflight uses bounded read-only observation and never mutates provider state to test access.
- No automatic permission expansion, stronger-profile retry, or inferred access from organisation role or coarse repository write access.
- Revalidation must use the same actor, installation, repository ID, capability ceiling, policy/config revision, and operation-scoped credential material identity.

---

## File map

### New files

- `src/repoforge/domain/github_capability_preflight.py` — exact capability catalogue, requirements, evidence states, reports, digests, and authorization guard.
- `src/repoforge/ports/github_capability_preflight.py` — isolated preflight protocol.
- `src/repoforge/adapters/github/capability_preflight.py` — GitHub REST/GraphQL evidence collector and typed response classification.
- `tests/test_github_capability_preflight_domain.py` — domain matrix, digest, and fail-closed tests.
- `tests/test_github_capability_preflight_adapter.py` — isolated-command and enterprise-policy fixtures.

### Modified files

- `src/repoforge/domain/errors.py` — typed token-approval, ruleset, workflow-policy, network-policy, and unobservable-evidence errors.
- `src/repoforge/domain/github_api_identity.py` — exact operation capability IDs and optional safe preflight report on grants/proofs only where needed for validation.
- `src/repoforge/ports/github_api_token.py` — preflight dependency contract imports where required.
- `src/repoforge/adapters/github/api_identity.py` — run initial exact preflight before material creation and bind safe digests into provider metadata.
- `src/repoforge/adapters/github/__init__.py` — exports.
- `src/repoforge/ports/__init__.py` — exports.
- `src/repoforge/ports/publication.py` — make authorization revalidation capability-aware with exact process auth context.
- `src/repoforge/adapters/publication.py` — re-run preflight immediately before publication review and reject digest/state drift.
- `src/repoforge/application/publication.py` — preserve fresh preflight digest in durable request/result evidence and ordering tests.
- `src/repoforge/bootstrap.py` and `src/repoforge/application/context.py` — wire the dedicated preflight adapter without replacing the existing doctor probe.
- `tests/test_github_api_identity.py` — initial preflight, metadata, refresh, and secret-safety tests.
- `tests/test_publication_adapter.py` — write-time TOCTOU and classification tests.
- `tests/test_publication_guards.py` — coordinator ordering and effect-boundary tests.
- `tests/test_operation_identity_leases.py` — durable safe metadata and refresh drift tests.
- `tests/test-groups.toml`, `tests/coverage-map.json`, `scripts/select_affected_tests.py` — test inventory and selection.
- `docs/development/REPOSITORY_IDENTITY.md` and `docs/development/REPOSITORY_IDENTITY_SURFACES.json` — production identity surface documentation.

---

### Task 1: Exact capability domain and deterministic evidence

**Files:**
- Create: `src/repoforge/domain/github_capability_preflight.py`
- Create: `tests/test_github_capability_preflight_domain.py`
- Modify: `src/repoforge/domain/__init__.py`

**Interfaces:**
- Produces: `GitHubOperationCapability`, `GitHubPermissionRequirement`, `GitHubCapabilityEvidenceState`, `GitHubCapabilityResult`, `GitHubCapabilityPreflightRequest`, `GitHubCapabilityPreflightReport`, `github_capability_requirements()`, `authorize_github_capabilities(report)`.
- Consumes: no new implementation interfaces.

- [ ] **Step 1: Write RED tests for the complete matrix and request validation**

```python
@pytest.mark.parametrize(
    ("capability", "permission_id"),
    [
        (GitHubOperationCapability.CONTENTS_READ, "contents:read"),
        (GitHubOperationCapability.CONTENTS_WRITE, "contents:write"),
        (GitHubOperationCapability.ISSUES_READ, "issues:read"),
        (GitHubOperationCapability.ISSUES_WRITE, "issues:write"),
        (GitHubOperationCapability.PULL_REQUESTS_READ, "pull_requests:read"),
        (GitHubOperationCapability.PULL_REQUESTS_WRITE, "pull_requests:write"),
        (GitHubOperationCapability.WORKFLOWS_READ, "actions:read"),
        (GitHubOperationCapability.WORKFLOWS_WRITE, "workflows:write"),
        (GitHubOperationCapability.RELEASES_READ, "contents:read"),
        (GitHubOperationCapability.RELEASES_WRITE, "contents:write"),
        (GitHubOperationCapability.PROJECTS_READ, "organization_projects:read"),
        (GitHubOperationCapability.PROJECTS_WRITE, "organization_projects:write"),
        (GitHubOperationCapability.PACKAGES_READ, "organization_packages:read"),
        (GitHubOperationCapability.PACKAGES_WRITE, "organization_packages:write"),
    ],
)
def test_capability_matrix_is_exact(capability, permission_id):
    assert github_capability_requirements()[capability].permission_id == permission_id
```

Add tests that empty capability tuples, duplicates, unknown strings, duplicate results, missing results, extra results, invalid SHA-256 values, and invalid stable IDs raise `ValueError`.

- [ ] **Step 2: Run RED selectors**

Run:

```bash
pytest -q tests/test_github_capability_preflight_domain.py
```

Expected: collection/import failure because the domain module and types do not exist.

- [ ] **Step 3: Implement the minimal immutable domain**

Use exact enum values:

```python
class GitHubOperationCapability(str, Enum):
    CONTENTS_READ = "github.contents.read"
    CONTENTS_WRITE = "github.contents.write"
    ISSUES_READ = "github.issues.read"
    ISSUES_WRITE = "github.issues.write"
    PULL_REQUESTS_READ = "github.pull_requests.read"
    PULL_REQUESTS_WRITE = "github.pull_requests.write"
    WORKFLOWS_READ = "github.workflows.read"
    WORKFLOWS_WRITE = "github.workflows.write"
    RELEASES_READ = "github.releases.read"
    RELEASES_WRITE = "github.releases.write"
    PROJECTS_READ = "github.projects.read"
    PROJECTS_WRITE = "github.projects.write"
    PACKAGES_READ = "github.packages.read"
    PACKAGES_WRITE = "github.packages.write"
```

Use exact evidence states:

```python
class GitHubCapabilityEvidenceState(str, Enum):
    PROVEN_AVAILABLE = "proven_available"
    PROVEN_DENIED = "proven_denied"
    LIKELY_POLICY_DENIED = "likely_policy_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNOBSERVABLE = "unobservable"
```

Canonical digest input must sort capability IDs, permission IDs, and result payloads by capability ID. It must contain safe metadata only.

`authorize_github_capabilities(report)` returns the report only when every requested result is `PROVEN_AVAILABLE`; otherwise it raises the typed error stored on the first non-available result.

- [ ] **Step 4: Run GREEN tests and strict type check for the new module**

Run:

```bash
pytest -q tests/test_github_capability_preflight_domain.py
mypy --strict src/repoforge/domain/github_capability_preflight.py
```

Expected: all tests pass and Mypy reports no issues.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/repoforge/domain/github_capability_preflight.py src/repoforge/domain/__init__.py tests/test_github_capability_preflight_domain.py
git commit -m "feat(identity): model exact GitHub capabilities (#293)"
```

---

### Task 2: Typed enterprise-policy failures and narrow recovery

**Files:**
- Modify: `src/repoforge/domain/errors.py`
- Modify: `src/repoforge/domain/github_capability_preflight.py`
- Modify: `tests/test_github_capability_preflight_domain.py`
- Test: `tests/test_failure_intelligence.py`

**Interfaces:**
- Consumes: `GitHubCapabilityResult.error_code` from Task 1.
- Produces: new `ErrorCode` values and secret-safe `RepoForgeError` details/recovery actions.

- [ ] **Step 1: Add RED parameterized error tests**

Test these exact mappings:

```python
(
    GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
    "sso",
    ErrorCode.GITHUB_SSO_AUTHORIZATION_REQUIRED,
)
(
    GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
    "token_approval",
    ErrorCode.GITHUB_TOKEN_APPROVAL_REQUIRED,
)
(
    GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
    "ruleset",
    ErrorCode.GITHUB_RULESET_POLICY_DENIED,
)
(
    GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
    "workflow_policy",
    ErrorCode.GITHUB_WORKFLOW_POLICY_DENIED,
)
(
    GitHubCapabilityEvidenceState.LIKELY_POLICY_DENIED,
    "network_policy",
    ErrorCode.GITHUB_NETWORK_POLICY_DENIED,
)
(
    GitHubCapabilityEvidenceState.UNOBSERVABLE,
    "enterprise_evidence",
    ErrorCode.GITHUB_ENTERPRISE_EVIDENCE_UNOBSERVABLE,
)
```

Assert `retryable=False` for policy/unobservable errors, `retryable=True` only for `GITHUB_PROVIDER_UNAVAILABLE`, and safe next actions never contain “switch profile”, “broader token”, or “admin token”.

- [ ] **Step 2: Run RED selectors**

```bash
pytest -q tests/test_github_capability_preflight_domain.py -k "typed or recovery or retry"
```

Expected: failures because the new error codes do not exist.

- [ ] **Step 3: Add exact error codes and error constructor**

Add:

```python
GITHUB_TOKEN_APPROVAL_REQUIRED = "GITHUB_TOKEN_APPROVAL_REQUIRED"
GITHUB_RULESET_POLICY_DENIED = "GITHUB_RULESET_POLICY_DENIED"
GITHUB_WORKFLOW_POLICY_DENIED = "GITHUB_WORKFLOW_POLICY_DENIED"
GITHUB_NETWORK_POLICY_DENIED = "GITHUB_NETWORK_POLICY_DENIED"
GITHUB_ENTERPRISE_EVIDENCE_UNOBSERVABLE = "GITHUB_ENTERPRISE_EVIDENCE_UNOBSERVABLE"
```

The domain error builder must set:

```python
unchanged_state=("No GitHub external write was admitted.",)
```

and details limited to capability ID, repository ID, installation ID, evidence state, and policy category.

- [ ] **Step 4: Run GREEN and failure-intelligence compatibility tests**

```bash
pytest -q tests/test_github_capability_preflight_domain.py tests/test_failure_intelligence.py
```

Expected: pass with no secret-bearing details.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/repoforge/domain/errors.py src/repoforge/domain/github_capability_preflight.py tests/test_github_capability_preflight_domain.py
git commit -m "feat(identity): type GitHub enterprise denials (#293)"
```

---

### Task 3: Isolated GitHub preflight adapter

**Files:**
- Create: `src/repoforge/ports/github_capability_preflight.py`
- Create: `src/repoforge/adapters/github/capability_preflight.py`
- Create: `tests/test_github_capability_preflight_adapter.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/github/__init__.py`

**Interfaces:**
- Consumes: Task 1 domain request/report and `ProcessAuthContext`.
- Produces:

```python
class GitHubCapabilityPreflightGateway(Protocol):
    def preflight(
        self,
        cwd: Path,
        request: GitHubCapabilityPreflightRequest,
        auth_context: ProcessAuthContext,
    ) -> GitHubCapabilityPreflightReport: ...
```

- [ ] **Step 1: Write RED adapter tests for exact endpoint selection and isolation**

Create a fake executor recording `run_isolated` calls. For a request containing only `github.pull_requests.write`, assert calls are limited to:

```text
GET repositories/{repository_id}
GET repositories/{repository_id}/pulls?per_page=1
GET repositories/{repository_id}/rulesets?includes_parents=true
```

Assert every command includes the exact `--hostname`, receives only `ProcessAuthContext.environment_dict()`, receives `auth_context.secret_values`, and ambient `run()` is never called.

Add tests proving issue, workflow, release, project, and package requests select only their own bounded probes.

- [ ] **Step 2: Write RED classification fixtures**

Use structured fake responses for:

- 200 with expected payload -> `PROVEN_AVAILABLE`.
- 403 plus `X-GitHub-SSO` -> SSO policy denial.
- 403 payload identifying fine-grained PAT approval -> token approval denial.
- installation repository missing -> installation/repository scope denial.
- 403 ruleset payload -> ruleset denial.
- Actions-disabled or selected-actions policy payload -> workflow policy denial.
- 403 IP allowlist or network policy payload -> network policy denial.
- 500/timeout -> provider unavailable.
- successful read with no safe write evidence -> unobservable.

- [ ] **Step 3: Run RED adapter suite**

```bash
pytest -q tests/test_github_capability_preflight_adapter.py
```

Expected: import failure because the port and adapter do not exist.

- [ ] **Step 4: Implement bounded isolated probes**

The adapter constructor is:

```python
class CommandGitHubCapabilityPreflight:
    def __init__(self, executor: CommandExecutor, server: ServerConfig) -> None: ...
```

Its internal runner must call only:

```python
self._executor.run_isolated(
    argv,
    cwd=cwd,
    environment=auth_context.environment_dict(),
    secrets=auth_context.secret_values,
    check=False,
    timeout=self._server.default_command_timeout_seconds,
    output_limit=min(max(self._server.max_tool_output_chars, 500_000), 5_000_000),
)
```

Do not retain raw response bodies in the report. Store bounded reason codes and SHA-256 response-shape digests only.

- [ ] **Step 5: Run GREEN adapter and secret-canary tests**

```bash
pytest -q tests/test_github_capability_preflight_adapter.py
mypy --strict src/repoforge/adapters/github/capability_preflight.py src/repoforge/ports/github_capability_preflight.py
```

Expected: pass; canary strings absent from report payloads and exception text.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/repoforge/ports/github_capability_preflight.py src/repoforge/adapters/github/capability_preflight.py src/repoforge/ports/__init__.py src/repoforge/adapters/github/__init__.py tests/test_github_capability_preflight_adapter.py
git commit -m "feat(identity): probe GitHub capabilities in isolation (#293)"
```

---

### Task 4: Bind preflight evidence into GitHub auth material and leases

**Files:**
- Modify: `src/repoforge/adapters/github/api_identity.py`
- Modify: `src/repoforge/domain/github_api_identity.py`
- Modify: `src/repoforge/ports/github_api_token.py`
- Modify: `tests/test_github_api_identity.py`
- Modify: `tests/test_operation_identity_leases.py`

**Interfaces:**
- Consumes: `GitHubCapabilityPreflightGateway.preflight` from Task 3.
- Produces: `GitHubApiAuthProvider(..., capability_preflight=..., config_revision=..., policy_revision=...)` and safe provider metadata keys.

- [ ] **Step 1: Add RED provider tests**

Construct a fake preflight gateway and assert `GitHubApiAuthProvider.resolve()` calls it after actor/repository verification and before returning material.

Assert the request capability IDs exactly equal the selected `StoredGhAccountSpec.capability_ids` or `GitHubAppInstallationSpec.capability_ids`.

Assert returned material metadata contains exactly:

```python
{
    "github_kind": "app_installation",
    "github_host": "github.com",
    "repository_id": "123456",
    "installation_id": "installation-84",
    "github_preflight_evidence_digest": report.evidence_digest,
    "github_capability_digest": report.capability_digest,
    "github_permission_digest": report.permission_digest,
    "github_preflight_observed_at": report.observed_at,
    "config_revision": report.config_revision,
    "policy_revision": report.policy_revision,
}
```

- [ ] **Step 2: Add RED lease and refresh tests**

Assert `github_api_auth_lease()` persists all safe metadata and no token.

Assert refresh succeeds only when actor, installation, repository, requested capability set, config revision, policy revision, capability digest, and permission digest remain identical. A changed evidence timestamp and credential material digest are allowed; changed capability/permission/policy digest fails with `CREDENTIAL_REFRESH_IDENTITY_MISMATCH`.

- [ ] **Step 3: Run RED selectors**

```bash
pytest -q tests/test_github_api_identity.py -k preflight
pytest -q tests/test_operation_identity_leases.py -k "metadata or refresh"
```

Expected: failures because the provider has no preflight dependency or metadata.

- [ ] **Step 4: Implement initial preflight integration**

Build a temporary `ProcessAuthContext` from the issued grant without persisting it:

```python
process_auth = ProcessAuthContext(
    profile_id=profile_id,
    material_id=grant.grant_id,
    target_kind=AuthTargetKind.REPOSITORY,
    target_id=grant.repository_id,
    environment=(("GH_TOKEN", grant.token.reveal()),),
    _secret_values=(grant.token.reveal(),),
)
```

Call `gateway.preflight(self._cwd, request, process_auth)`, then `authorize_github_capabilities(report)`, then create `AuthMaterial`. Release/revoke the grant on every preflight failure.

- [ ] **Step 5: Run GREEN identity suites**

```bash
pytest -q tests/test_github_api_identity.py tests/test_operation_identity_leases.py
```

Expected: pass with token canaries absent from all durable/safe payloads.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/repoforge/adapters/github/api_identity.py src/repoforge/domain/github_api_identity.py src/repoforge/ports/github_api_token.py tests/test_github_api_identity.py tests/test_operation_identity_leases.py
git commit -m "feat(identity): bind GitHub preflight to auth leases (#293)"
```

---

### Task 5: Write-time publication revalidation and TOCTOU denial

**Files:**
- Modify: `src/repoforge/ports/publication.py`
- Modify: `src/repoforge/adapters/publication.py`
- Modify: `src/repoforge/application/publication.py`
- Modify: `tests/test_publication_adapter.py`
- Modify: `tests/test_publication_guards.py`

**Interfaces:**
- Consumes: Task 3 gateway and Task 4 AuthLease metadata.
- Produces: capability-aware `PublicationAuthorizationGateway.revalidate`:

```python
def revalidate(
    self,
    intent: PublicationIntent,
    expected: PublicationAuthorization,
    *,
    requested_capability_ids: tuple[str, ...],
    auth_context: ProcessAuthContext,
) -> PublicationAuthorization: ...
```

- [ ] **Step 1: Add RED TOCTOU and ordering tests**

Update coordinator events to require:

```python
[
    "inspect",
    "idempotent",
    "bind",
    "require_write",
    "capability_preflight",
    "revalidate",
    "publish",
]
```

Add cases where the initial authorization is available but write-time preflight returns permission drift, SSO denial, ruleset denial, provider unavailable, or unobservable evidence. Assert `publish` is absent and `IdempotencyEffectBoundary.started is False`.

- [ ] **Step 2: Add RED digest and reconciliation tests**

Assert fresh report actor, installation, repository, config revision, policy revision, capability digest, and permission digest must equal the values pinned in `PublicationAuthorization` and AuthLease metadata.

Assert lost-response reconciliation retains the original capability and permission digests and cannot substitute a report for another capability set.

- [ ] **Step 3: Run RED publication selectors**

```bash
pytest -q tests/test_publication_adapter.py -k "preflight or capability or permission or ruleset or sso"
pytest -q tests/test_publication_guards.py -k "coordinator or boundary or revalidation"
```

Expected: failures because write-time capability preflight is not invoked.

- [ ] **Step 4: Implement write-time revalidation before the effect boundary**

Pass the exact operation capability request into authorization revalidation. The adapter must call `authorize_github_capabilities(fresh_report)` and compare all pinned fields before returning a fresh `PublicationAuthorization`.

The coordinator must not call `boundary.begin()` until both operation lease validation and capability-aware publication revalidation have succeeded.

Add the fresh evidence digest to `_PublicationResult` and its safe payload so the durable result records which exact preflight authorized the effect.

- [ ] **Step 5: Run GREEN publication suites**

```bash
pytest -q tests/test_publication_adapter.py tests/test_publication_guards.py tests/test_service_tools.py tests/test_v2_shipping.py
```

Expected: all pass; no external effect starts on any denied or uncertain preflight.

- [ ] **Step 6: Commit Task 5**

```bash
git add src/repoforge/ports/publication.py src/repoforge/adapters/publication.py src/repoforge/application/publication.py tests/test_publication_adapter.py tests/test_publication_guards.py
git commit -m "feat(identity): revalidate GitHub capabilities before publish (#293)"
```

---

### Task 6: Production composition without replacing doctor diagnostics

**Files:**
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_github_capability_probe.py`
- Modify: `tests/test_github_capability_preflight_adapter.py`

**Interfaces:**
- Consumes: `CommandGitHubCapabilityPreflight` from Task 3.
- Produces: `ApplicationContext.github_capability_preflight` and `AdapterOverrides.github_capability_preflight` while retaining `ApplicationContext.github_capabilities` for the legacy doctor probe.

- [ ] **Step 1: Add RED composition tests**

Assert production bootstrap creates both objects and they are different types:

```python
assert isinstance(ctx.github_capabilities, CommandGitHubCapabilityProbe)
assert isinstance(ctx.github_capability_preflight, CommandGitHubCapabilityPreflight)
```

Assert doctor still uses the old probe and publication/auth paths use only the new preflight gateway.

- [ ] **Step 2: Run RED composition selectors**

```bash
pytest -q tests/test_integration.py -k capability_preflight
pytest -q tests/test_github_capability_probe.py
```

Expected: the new context/override field is absent; existing doctor tests remain green.

- [ ] **Step 3: Wire the new adapter**

Append the new optional context field near `github_capabilities` without shifting positional construction unexpectedly. Add the override and build default:

```python
github_capability_preflight = (
    o.github_capability_preflight
    or CommandGitHubCapabilityPreflight(command, config.server)
)
```

Do not alter `CommandGitHubCapabilityProbe` or doctor behavior.

- [ ] **Step 4: Run GREEN composition and lifecycle suites**

```bash
pytest -q tests/test_integration.py tests/test_github_capability_probe.py tests/test_phases1_5_full_lifecycle.py
```

Expected: pass with explicit test preflight fixtures where publication services are composed.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/repoforge/application/context.py src/repoforge/bootstrap.py tests/conftest.py tests/test_integration.py tests/test_github_capability_probe.py tests/test_github_capability_preflight_adapter.py
git commit -m "refactor(identity): wire dedicated GitHub preflight (#293)"
```

---

### Task 7: Inventory, documentation, and affected-test selection

**Files:**
- Modify: `tests/test-groups.toml`
- Modify: `tests/coverage-map.json`
- Modify: `scripts/select_affected_tests.py`
- Modify: `docs/development/REPOSITORY_IDENTITY.md`
- Modify: `docs/development/REPOSITORY_IDENTITY_SURFACES.json`

**Interfaces:**
- Consumes: all Task 1–6 source and test paths.
- Produces: complete release-gate inventory and operator-facing identity documentation.

- [ ] **Step 1: Add new tests and sources to manifests**

Add both new test modules to the repository-identity and always-on safety groups. Add every new source file to `coverage-map.json` with focused consumers and add direct import consumers to the selector detector.

- [ ] **Step 2: Document the two distinct capability surfaces**

Document:

- ambient doctor/discovery probe: informational only;
- operation-scoped preflight: authorization evidence;
- exact capability catalogue;
- evidence states and deny rules;
- safe lease/publication metadata;
- typed recovery without broader credentials.

Update the JSON surface inventory with stable IDs for the new domain, port, adapter, AuthLease metadata, and publication revalidation surfaces.

- [ ] **Step 3: Run manifest and documentation checks**

```bash
pytest -q tests/test_test_group_manifest.py tests/test_coverage_map.py tests/test_repository_identity_contracts.py
```

Expected: all new source/test files are explicitly inventoried.

- [ ] **Step 4: Commit Task 7**

```bash
git add tests/test-groups.toml tests/coverage-map.json scripts/select_affected_tests.py docs/development/REPOSITORY_IDENTITY.md docs/development/REPOSITORY_IDENTITY_SURFACES.json
git commit -m "docs(identity): inventory GitHub capability preflight (#293)"
```

---

### Task 8: Final regression gates and issue evidence

**Files:**
- Review only unless a failure proves a correction is required.

**Interfaces:**
- Consumes: complete implementation.
- Produces: clean exact fingerprint, authoritative verification receipt, and issue #293 completion evidence.

- [ ] **Step 1: Format changed files**

Run the reviewed changed-file formatter and verify no unrelated files change.

- [ ] **Step 2: Run focused identity/publication bundle**

```bash
pytest -q \
  tests/test_github_capability_preflight_domain.py \
  tests/test_github_capability_preflight_adapter.py \
  tests/test_github_api_identity.py \
  tests/test_operation_identity_leases.py \
  tests/test_publication_adapter.py \
  tests/test_publication_guards.py \
  tests/test_service_tools.py \
  tests/test_v2_shipping.py \
  tests/test_phase6_operational_hardening.py
```

Expected: zero failures.

- [ ] **Step 3: Run quick gate**

```bash
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy --strict src/repoforge
```

Expected: formatting, Ruff, and strict Mypy pass.

- [ ] **Step 4: Review exact diff and whitespace**

```bash
git diff --check
git status --short
git diff --stat main...HEAD
```

Expected: no whitespace errors, no temporary artifacts, and only #293 scope.

- [ ] **Step 5: Run authoritative RepoForge `verify`**

Run the configured final profile on the clean exact fingerprint. Wait for the durable operation to reach a terminal state and record its operation/result reference.

Expected: `succeeded`, 8/8 steps, verification current for the exact HEAD/fingerprint.

- [ ] **Step 6: Add issue #293 completion evidence**

Comment with:

- exact final HEAD;
- focused test commands and results;
- authoritative verify operation/result reference;
- acceptance-criteria mapping;
- confirmation that #51 policy authority and #52 scope boundaries were preserved;
- confirmation that no global auth switch, ambient fallback, permission broadening, or secret persistence was introduced.

Close #293 only if repository issue-write policy permits close. Otherwise leave the verified completion comment and report the policy limitation without weakening policy.
