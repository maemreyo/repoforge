# Unified Execution Boundary and Truthful Enforcement Design

**Status:** Proposed for implementation planning  
**Date:** 2026-07-19  
**Program:** Fast, Reproducible Execution / Security and Trust

## Decision summary

RepoForge will make `ExecutionEnvironmentPort` the single internal boundary for every command that executes repository-controlled code or tooling. Profile, diagnostic, ad-hoc, formatter, hygiene, and execution-plan paths will no longer invoke the native command executor directly.

Every execution will carry a typed requested policy and receive a typed effective policy plus an explicit enforcement assessment. Environment identities, execution receipts, verification reuse, and commit eligibility will bind to the effective policy and adapter identity rather than treating requested network/filesystem labels as proof of enforcement.

This slice preserves the existing native reviewed backend and public tool roster. It does not add Docker Sandboxes, shell syntax, new credentials, or new network access. It creates the trustworthy architecture seam required for those later capabilities.

## Context and problem statement

RepoForge already has the correct architectural direction:

```text
ExecutionEnvironmentPort
  ├── NativeReviewedAdapter
  ├── DevContainerAdapter
  └── HermeticContainerAdapter
```

The current implementation is incomplete in four important ways.

1. `workspace_run_profile` uses `ExecutionEnvironmentPort`, but `workspace_run_diagnostic` and `workspace_run_adhoc` execute through `ApplicationContext.commands` directly.
2. Formatter and hygiene commands execute through a separate `CommandHygieneGateway` backed by the same native command executor, so adding a sandbox adapter would not cover those paths.
3. `NativeReviewedAdapter.identity()` copies requested network and filesystem values into `EnvironmentIdentity`, even though native subprocess execution does not enforce network isolation or workspace-only filesystem access.
4. `VerificationReceipt` stores an optional environment identity hash, but `workspace_commit` validates only the workspace fingerprint. A toolchain, adapter, effective execution policy, or backend identity change can therefore leave an apparently current verification receipt.

The resource-budget model has the same truthfulness gap. Timeout and output bounds are enforced by the current command executor, while CPU, memory, disk, subprocess-count, and network-byte fields are configuration and capability-delta data rather than runtime enforcement.

Adding a Docker or microVM backend before repairing these boundaries would create a misleading split system:

```text
profile execution      -> sandbox backend
diagnostic execution   -> native host
ad-hoc execution       -> native host
formatter/hygiene      -> native host
```

The system must first guarantee that a selected execution backend covers every repository-code execution path and that receipts describe what was actually enforced.

## Approaches considered

### Backend-first integration

Implement a Docker Sandboxes adapter against the current port and migrate callers later.

Rejected because it would sandbox only profile execution at first, leaving high-value bypass paths on the host. It would also force the backend adapter to inherit ambiguous policy and receipt contracts that need to change anyway.

### Local truthfulness patches only

Correct the current diagnostic and ad-hoc network labels without changing execution routing.

Rejected because it fixes presentation while preserving multiple execution paths, duplicated lifecycle behavior, and future backend bypasses.

### Unified boundary first

Refine the execution contracts, route all repository-code commands through one coordinator and port, make enforcement evidence truthful, and strengthen verification binding before adding a sandbox backend.

Selected because it is independently useful, preserves current behavior, minimizes public-contract churn, and gives later sandbox work one complete integration point.

## Goals

1. Route every command that executes repository-controlled code or reviewed repository tooling through one typed execution boundary.
2. Separate requested execution policy from effective backend behavior.
3. Represent enforcement honestly as enforced, advisory, observed, unsupported, or not applicable.
4. Bind environment identity, failure reuse, iteration cache, verification receipts, and commit eligibility to the effective execution policy.
5. Preserve existing profile, diagnostic, ad-hoc, formatter, plan, audit, locking, cancellation, output-bounding, and exact-tree semantics.
6. Fail closed when a future backend is configured as enforcement-required but cannot satisfy the requested policy.
7. Keep all execution evidence bounded, deterministic, secret-safe, and free of absolute user paths in public results.
8. Allow a later Docker Sandboxes adapter to cover the full system without application-layer rewrites.

## Non-goals

- No Docker Sandboxes, Docker Engine, gVisor, Firecracker, dev-container, or hermetic-container implementation.
- No public shell tool, shell string, pipe, redirection, heredoc, command substitution, or free-form environment input.
- No change to the MCP tool roster.
- No additional network access or credential injection.
- No host-port publishing.
- No persistent sandbox lifecycle or sandbox restart reconciliation.
- No clean final-verification sandbox.
- No automatic workspace snapshot or rollback around commands.
- No change to force-push, draft-PR, protected-branch, denied-path, workflow-editing, release, merge, or secret-management policy.
- No claim that native execution is sandboxed.

## Design principles

### Requested policy is not enforcement evidence

Application code declares what it intends. The backend reports what it can and did provide. The two are stored separately.

### Backend selection never degrades silently

A backend may execute with advisory behavior only when the repository's selected execution trust mode explicitly permits that backend. A backend configured as enforcement-required must reject unsupported policy before starting a command.

### Exact source and exact environment are separate bindings

A workspace fingerprint proves source-tree identity. An environment identity proves adapter, toolchain, effective policy, and reviewed environment identity. Commit eligibility requires both to remain current.

### Public capability does not depend on tool wording

Profile, diagnostic, ad-hoc, formatter, and plan tools are different policy entry points into the same execution system. None may bypass the selected backend by invoking a lower-level subprocess adapter directly.

### Workspace policy remains outside the backend

The execution environment runs commands and reports evidence. Git path policy, fingerprinting, change budgets, expected mutation scope, verification invalidation, and commit eligibility remain application/domain responsibilities. A backend cannot authorize a workspace mutation merely because it was able to perform it.

## Terminology and typed domain model

The existing execution-environment module remains the home of backend-neutral policy and identity types. The port module remains the home of lifecycle and execution request/receipt contracts.

### Requested execution policy

```python
@dataclass(frozen=True, slots=True)
class RequestedExecutionPolicy:
    network: NetworkAccess
    filesystem: FilesystemAccess
    credentials: tuple[CredentialCapability, ...]
    resources: RequestedResourceLimits
    enforcement_requirement: EnforcementRequirement
```

Initial credential capabilities are empty for existing repository execution. The field is included now so later credential-proxy work does not require another request-contract redesign.

`NetworkAccess` is backend-neutral:

```text
offline
public_http_https
public_general
private_approved
host_inherited
```

Application callers may request the first four values. `host_inherited` is an effective native-backend fact and is never caller-selectable.

Existing reviewed network fields map conservatively:

```text
local_only / none  -> offline
restricted         -> public_http_https
external           -> public_general
```

Profiles currently have no reviewed network field, so this slice maps existing profiles to `public_general`, matching their present native execution behavior without adding a configuration field. The mapping expresses caller intent only. The native adapter normally resolves to `host_inherited`, regardless of a narrower request.

`FilesystemAccess` is backend-neutral:

```text
source_read
workspace_write
managed_state_write
host_account_access
```

Application callers request only the first three. `host_account_access` is an effective native-backend fact and is never a caller-selectable capability.

`EnforcementRequirement` has two values:

```text
advisory_backend_allowed
enforcement_required
```

Existing installations implicitly select the native reviewed backend and therefore use `advisory_backend_allowed`. A later sandbox configuration will use `enforcement_required` by default. This makes compatibility explicit without pretending native behavior satisfies isolation requests.

### Enforcement assessment

```python
class EnforcementLevel(str, Enum):
    ENFORCED = "enforced"
    ADVISORY = "advisory"
    OBSERVED = "observed"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"
```

```python
@dataclass(frozen=True, slots=True)
class EnforcementAssessment:
    network: EnforcementLevel
    filesystem: EnforcementLevel
    timeout: EnforcementLevel
    output: EnforcementLevel
    process_cleanup: EnforcementLevel
    cpu: EnforcementLevel
    memory: EnforcementLevel
    disk: EnforcementLevel
    subprocess_count: EnforcementLevel
    network_bytes: EnforcementLevel
```

For the native adapter in this slice:

```text
network             -> advisory
filesystem          -> advisory
wall-clock timeout  -> enforced
output bound        -> enforced
process-group kill  -> enforced
CPU                 -> unsupported
memory              -> unsupported
disk                -> unsupported
subprocess count    -> unsupported
network bytes       -> unsupported
```

`OBSERVED` is reserved for a backend that measures a property after execution without preventing violations. Configuration presence alone is not observation.

### Effective execution policy

```python
@dataclass(frozen=True, slots=True)
class EffectiveExecutionPolicy:
    network: NetworkAccess
    filesystem: FilesystemAccess
    credential_capabilities: tuple[CredentialCapability, ...]
    resource_limits: EffectiveResourceLimits
    enforcement: EnforcementAssessment
    degraded: bool
    degradation_reasons: tuple[str, ...]
```

The model is bounded and deterministic. Degradation reasons are stable enums or bounded safe messages, not backend logs.

### Execution scope

Repository tooling runs against more than one root. Formatter baseline inspection materializes a committed snapshot in a temporary directory, while normal commands run against a workspace.

```python
class ExecutionScopeKind(str, Enum):
    WORKSPACE = "workspace"
    SNAPSHOT_READ_ONLY = "snapshot_read_only"
```

```python
@dataclass(frozen=True, slots=True)
class ExecutionScope:
    kind: ExecutionScopeKind
    root: Path
    command_cwd: Path
    workspace_id: str | None
    working_directory_policy: str
```

Paths are internal values and are not serialized into public results. The coordinator validates that `command_cwd` is inside `root`. Workspace roots have already passed repository/workspace allowlisting. Snapshot roots must be materialized by RepoForge-owned infrastructure, not supplied by the model. A generic managed-temporary scope has no consumer in this slice and is deliberately deferred rather than added speculatively.

### Execution request

```python
@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    scope: ExecutionScope
    reviewed_commands: tuple[tuple[str, ...], ...]
    requested_policy: RequestedExecutionPolicy
    timeout_seconds: int
    output_limit: int
    artifact_paths: tuple[str, ...]
    cancel_token: CancellationToken | None
```

`reviewed_commands` is the closed command set used for identity and session admission. The exact argv for one process is passed only to the coordinator-owned session's `execute(argv)` method, which rejects any argv not present in this set. The model never supplies backend flags, mounts, images, devices, users, credentials, or environment values.

### Prepared environment session

The lifecycle changes from separate `prepare()` and `identity()` calls with no shared value to one typed session:

```python
@dataclass(frozen=True, slots=True)
class PreparedEnvironmentSession:
    session_id: str
    identity: EnvironmentIdentity
    requested_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    effective_policy_hash: str
```

`session_id` is a bounded opaque backend reference safe for internal operation correlation. It is not authorization and is not exposed as an arbitrary backend handle to MCP clients.

### Environment inspection

Commit validation and cache/reuse admission need a current identity without starting the reviewed command.

```python
@dataclass(frozen=True, slots=True)
class EnvironmentInspection:
    identity: EnvironmentIdentity
    requested_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    effective_policy_hash: str
```

Inspection may perform bounded backend metadata and reviewed tool-version probes, but it must not run repository command bodies, mutate the source root, install dependencies, or create external state.

### Execution receipt

```python
@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    argv: tuple[str, ...]
    session_start_identity_hash: str
    requested_policy_hash: str
    effective_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    result: CommandResult
    artifacts: tuple[ArtifactResult, ...]
```

The coordinator constructs this per-command receipt from the prepared session, command result, and collected artifacts. `session_start_identity_hash` is audit evidence for the environment admitted before the command; it is not the identity used for final commit eligibility. Workspace fingerprint and mutation evidence remain in the enclosing workspace use-case result because they require Git/repository policy and differ by caller.

## Environment identity version 2

`EnvironmentIdentity` advances to schema version 2. Schema-v1 records remain readable as historical evidence, while new identities replace the ambiguous requested-policy fields with explicit requested/effective bindings:

```text
requested_policy_hash
effective_policy_hash
effective_network
effective_filesystem
enforcement_assessment
backend_capability_hash
```

The identity continues to include:

```text
adapter kind and version
platform and architecture
runtime version
reviewed tool versions or digests
lockfile digests
manifest digests
approved environment names and value hashes
working-directory policy hash
```

The identity must describe effective behavior. The native adapter must no longer copy requested `offline` or `source_read` values into identity fields as though they were enforced. It records effective `host_inherited` network and `host_account_access` filesystem behavior with advisory enforcement.

Unknown tool versions keep the identity incomplete and non-cacheable. This slice introduces no policy requiring complete identity, so incompleteness does not block native execution or commit when all required hashes are present and stable. A reviewed completeness requirement belongs to a later sandbox/configuration design.

The version-2 identity is secret-safe and deterministic. It excludes absolute host paths, source bodies, command output bodies, raw environment values, raw credentials, backend logs, and unbounded package inventories.

## Execution environment port

The internal protocol becomes:

```python
class ExecutionEnvironmentPort(Protocol):
    def doctor(self, request: ExecutionRequest) -> EnvironmentDoctorResult: ...
    def inspect(self, request: ExecutionRequest) -> EnvironmentInspection: ...
    def prepare(self, request: ExecutionRequest) -> PreparedEnvironmentSession: ...
    def execute(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
    ) -> CommandResult: ...
    def inspect_session(
        self,
        session: PreparedEnvironmentSession,
        request: ExecutionRequest,
    ) -> EnvironmentInspection: ...
    def collect_artifacts(
        self,
        session: PreparedEnvironmentSession,
        artifact_paths: Sequence[str],
    ) -> tuple[ArtifactResult, ...]: ...
    def cleanup(self, session: PreparedEnvironmentSession) -> None: ...
```

`inspect()` resolves current effective policy and identity without running repository command bodies. `prepare()` performs the same policy resolution and creates a session suitable for execution. `inspect_session()` re-inspects the same prepared environment after one or more commands and before cleanup; a future persistent or ephemeral sandbox adapter must inspect that exact sandbox generation rather than a newly prepared replacement. If `enforcement_required` cannot be satisfied, inspection and preparation raise a structured policy error before command start or source/external mutation.

For a stable backend and unchanged reviewed inputs, `inspect(request)` and the session returned by `prepare(request)` must produce the same initial identity and policy hashes. A mismatch is a backend drift error and command execution does not begin. Post-command `inspect_session()` may legitimately differ when a command changes reviewed lockfiles, manifests, toolchain state, or other identity inputs.

`cleanup()` is idempotent. Cleanup failure is recorded as bounded execution evidence and can make an operation incomplete, but it does not rewrite the command result. Persistent sandbox lifecycle and restart reconciliation are deferred to a later design.

## Execution coordinator

A new application service owns the shared lifecycle:

```python
class ExecutionSession(Protocol):
    @property
    def prepared(self) -> PreparedEnvironmentSession: ...

    def execute(self, argv: tuple[str, ...]) -> ExecutionReceipt: ...

    def inspect_current(self) -> EnvironmentInspection: ...


class ExecutionCoordinator:
    def inspect(self, request: ExecutionRequest) -> EnvironmentInspection: ...

    @contextmanager
    def session(self, request: ExecutionRequest) -> Iterator[ExecutionSession]: ...

    def run(
        self,
        request: ExecutionRequest,
        argv: tuple[str, ...],
    ) -> tuple[ExecutionReceipt, EnvironmentInspection]: ...
```

`session()` validates the request, calls backend doctor/prepare, checks initial inspection stability, and yields a coordinator-owned wrapper. `ExecutionSession.execute()` verifies exact membership in `reviewed_commands`, invokes the port, and collects declared artifacts. `inspect_current()` delegates to `inspect_session()` for the same prepared environment. The context manager performs cleanup exactly once in `finally`.

`run()` is only a single-command convenience implemented in terms of `session()`: execute one admitted argv, perform post-run inspection, then close. Multi-step callers use one context-managed session and retain fail-stop control between steps without gaining direct access to `ExecutionEnvironmentPort` or its backend session handle.

Suggested location:

```text
src/repoforge/application/execution/coordinator.py
```

The coordinator does not acquire workspace locks or calculate Git fingerprints. Callers retain their existing lock scope so background operation and mutation semantics do not change.

The coordinator is the only application service allowed to invoke `ExecutionEnvironmentPort`. `ApplicationContext` receives a required, non-optional `ExecutionCoordinator`; the raw execution-environment port is private to coordinator/bootstrap wiring rather than an optional application dependency. Bootstrap fails closed if the coordinator cannot be constructed, and all test contexts must supply a real or recording coordinator. Existing `None` checks and raw-command fallback branches are removed. Workspace use cases call the coordinator; adapters and Git/GitHub/runtime infrastructure may continue using their own lower-level ports for non-repository-code operations.

## Caller migration

### Profiles

`WorkspaceProfileRunner` already has the closest lifecycle. It will:

1. compile a single requested policy for the reviewed profile;
2. open one coordinator-owned environment session for all profile steps;
3. execute each reviewed step through `ExecutionSession.execute()` with existing fail-stop behavior;
4. retain per-step failure evidence and telemetry;
5. after the final successful step, call `inspect_current()` before cleanup;
6. store the post-run identity and policy hashes in the final verification receipt;
7. clean up exactly once through the context manager.

Existing no-regression accepted steps remain synthetic receipts but inherit the prepared session's policy evidence and do not trigger a command. They do not replace the mandatory post-run inspection. This mirrors the existing post-run workspace fingerprint: if verification updates a lockfile or manifest, the resulting receipt binds the updated state and can converge once the mutation becomes idempotent.

Profiles have no reviewed network field in the current configuration model. This slice therefore assigns existing profiles a requested `public_general` intent, matching current native behavior, and reports the effective native policy as advisory `host_inherited`. Adding per-profile network policy belongs to the later sandbox/configuration design and must go through capability-delta review.

### Diagnostics

`WorkspaceDiagnosticRunner` will stop calling `self.ctx.commands.run()`.

It will build one `ExecutionRequest` from the reviewed diagnostic profile:

```text
argv                -> resolved typed selector argv
network             -> reviewed diagnostic network policy
filesystem          -> source_read or workspace_write from mutability
artifacts            -> reviewed artifact paths
timeout/output       -> reviewed diagnostic bounds
enforcement mode    -> selected repository backend trust mode
```

The existing pre/post fingerprint, unexpected-mutation, parser, TDD intent, reusable-failure, and verification-invalidation behavior remains unchanged. Effective execution evidence becomes part of the result/audit projection.

### Ad-hoc execution

`WorkspaceAdhocRunner` will stop calling `self.ctx.commands.run()`.

It keeps all current restrictions:

- repository must be in relaxed execution mode;
- argv is a bounded list;
- `argv[0]` is an allowlisted bare runner;
- working directory stays inside the workspace;
- result is evidence only;
- commit eligibility is never granted;
- fingerprint changes invalidate prior verification;
- repeated argv shape produces an enrollment nudge.

The existing ad-hoc `advisory_local_only` label maps to requested `offline`; the native backend reports effective advisory `host_inherited` network and `host_account_access` filesystem behavior. No shell executable is added by this slice.

### Formatter and hygiene

`CommandHygieneGateway` must not retain a direct `CommandExecutor` path for repository tooling.

Workspace inspection, formatter remediation, and committed-baseline inspection use `ExecutionCoordinator` with different scopes:

```text
workspace inspection/remediation -> WORKSPACE
committed archive inspection      -> SNAPSHOT_READ_ONLY
```

The existing fixed argv, selected-path validation, archive bounds, parser, baseline cache, and unexpected-mutation checks remain authoritative.

The formatter's current custom environment digest is replaced by the common environment identity and effective policy hashes. Public formatter results retain a stable environment identity field and gain bounded execution-enforcement evidence additively.

### Execution plans

`WorkspacePlanExecutor` continues to delegate execution to profile and diagnostic runners. It must stop independently synthesizing a platform/toolchain environment digest when a real execution receipt exists.

Stage receipts and iteration-cache keys use the post-run environment identity and effective policy hash returned by the delegated runner. Cache hits remain impossible for incomplete identities, enforcement-required degradation, mutating stages, or final-verification stages.

Reuse admission is deliberately separate from enforcement truthfulness. The current `EnvironmentIdentity.cache_eligible` property is removed rather than extended: identity exposes structural completeness, while a typed `ReuseEligibility` decision combines completeness, stage mutability/finality, repository enforcement requirement, requested policy, effective policy, and backend capability. Network broadness is not hidden inside the identity hash.

In this compatibility slice, an advisory native backend is not automatically non-reusable when the repository accepts `advisory_backend_allowed`: existing read-only iteration-cache and deterministic-failure reuse remain eligible when all current exact bindings match, the identity is complete, and the requested/effective policy hashes are identical to the stored evidence. The effective policy hash is part of every key, preventing reuse across backend or policy changes. Such a hit is reported as advisory reuse and does not claim hermeticity. A later reviewed policy may require enforced isolation and thereby disable native reuse.

### Other command paths

During implementation, a repository-wide search must classify every `CommandExecutor.run`, subprocess, and hygiene command call.

Allowed direct command users are limited to infrastructure whose command does not execute repository-controlled code, such as:

```text
Git adapter, including bounded exact-commit snapshot materialization
GitHub CLI adapter
runtime/tunnel process management
reviewed executable version discovery inside an execution adapter
onboarding host preflight
```

Any ambiguous path is treated as repository-code execution and routed through the coordinator or explicitly documented with a negative test proving why it cannot execute repository-controlled input. The current hygiene gateway's direct `git archive` call is moved behind the Git repository port/adapter; archive extraction and path/byte validation may remain in hygiene, but hygiene no longer owns a raw executor.

## Public result and audit projection

The public tool roster does not change. Existing fields remain compatible.

Execution-capable results gain one additive bounded object:

```python
@dataclass(frozen=True, slots=True)
class EnforcementEvidence:
    network: str
    filesystem: str
    timeout: str
    output: str
    process_cleanup: str
    cpu: str
    memory: str
    disk: str
    subprocess_count: str
    network_bytes: str


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    adapter_kind: str
    environment_identity_hash: str
    requested_policy_hash: str
    effective_policy_hash: str
    requested_network: str
    effective_network: str
    requested_filesystem: str
    effective_filesystem: str
    degraded: bool
    enforcement: EnforcementEvidence
    warnings: tuple[str, ...]
```

Every string field has a closed enum or explicit length bound. `warnings` is deterministically ordered, limited to ten entries, and each entry is bounded to 500 characters. For a completed single- or multi-command use case, `ExecutionEvidence.environment_identity_hash` comes from the final post-run inspection; per-command receipts separately retain `session_start_identity_hash` for lifecycle audit.

Closed Forge v2 output schemas and release goldens must be updated for additive projection. Compatibility aliases must return identical execution evidence.

Legacy fields such as diagnostic `network_policy` retain their current meaning as the requested reviewed policy. Documentation must explicitly say that the new execution evidence is the source of truth for effective backend behavior.

Audit events record only bounded safe metadata:

```text
adapter kind
requested/effective policy hashes
degraded flag
enforcement levels
command count
runner or reviewed profile/diagnostic identifier
duration and exit code
fingerprint-changed flag where applicable
```

Audit does not record raw policy credentials, environment values, source bodies, patches, backend logs, full process trees, or unbounded command output.

## Verification receipt and commit gate

`VerificationReceipt` advances additively with required fields for newly created receipts:

```python
execution_identity_schema_version: int
environment_identity_hash: str
requested_policy_hash: str
effective_policy_hash: str
adapter_kind: str
profile_target_hash: str
config_identity_hash: str
```

Existing persisted records remain readable. A legacy receipt missing the new binding fields is not silently upgraded. In a repository that requires verification before commit, `workspace_commit` returns an actionable stale-verification error and requires one fresh full verification after the software upgrade.

Before commit, RepoForge reconstructs the selected verification profile's current execution request and calls `ExecutionCoordinator.inspect()` to obtain current identity and effective-policy evidence without executing the profile command. Commit eligibility requires:

```text
current workspace fingerprint == verification fingerprint
current environment identity   == receipt environment identity
current requested policy hash  == receipt requested policy hash
current effective policy hash  == receipt effective policy hash
current profile target hash    == receipt profile target hash
current config identity        == receipt config identity
```

If identity inspection is unavailable or resolves to a different identity/effective policy, commit fails closed and instructs the caller to rerun final verification. Identity incompleteness alone does not block commit in this slice because no completeness requirement is introduced; it only disables cache/reuse admission.

A future persistent sandbox backend may include sandbox generation/state in its identity. The commit gate therefore needs no application-contract redesign when that backend arrives.

## Failure, cancellation, and cleanup semantics

### Policy-resolution failure

Failure occurs before process start. The structured error includes:

```text
requested capability
backend capability
enforcement requirement
unchanged-state statement
safe operator action
```

No automatic fallback to native execution is permitted.

### Command failure

Existing structured `CommandError` behavior, exit code, timeout classification, output bounding, redaction, retry guidance, and failure intelligence remain intact. The execution receipt may be attached as bounded failure details when a process started. Before cleanup, the coordinator attempts `inspect_current()` even after a started command fails, times out, or is cancelled; deterministic failure reuse binds to that post-attempt identity. If post-failure inspection is unavailable or incomplete, the failure remains reportable but is not reusable.

### Cancellation

The same cancellation token reaches every backend through the coordinator. Native execution retains process-group termination. Diagnostic, ad-hoc, profile, formatter, and plan paths must have consistent cancellation tests.

### Cleanup failure

Cleanup runs in `finally`. A cleanup failure is represented separately from command success/failure. The current native adapter cleanup is a no-op. Later persistent backends may return an incomplete operation state requiring reconciliation.

### Mutation after failure

Execution failure does not imply unchanged workspace state. Existing callers must still compute post-command fingerprints and enforce mutation/path rules even when the command exits nonzero, times out, or is cancelled.

## Compatibility and migration

### Configuration

No new required source-configuration field is introduced in this slice. Existing installations implicitly use:

```text
backend = native_reviewed
enforcement_requirement = advisory_backend_allowed
```

The effective configuration and repository overview may expose these derived values additively. A later sandbox spec will add reviewed backend selection and backend-specific settings through immutable generations and capability-delta approval.

### Stored state

- Environment identity schema v1 remains readable for historical evidence.
- Newly generated identities use schema v2.
- Verification receipts missing the new binding fields are readable but cannot satisfy a future commit after upgrade; one re-verification repairs the state.
- Iteration-cache persistence advances to schema version 2 and adds `CacheMissReason.ENVIRONMENT_IDENTITY_SCHEMA_CHANGED`. The adapter retains a bounded legacy-key decoder, distinguishes schema-v1 envelopes from corruption, and returns that reason only when a legacy entry matches every current key dimension except the environment-identity schema/hash. Unrelated legacy entries do not mask a normal `not_found`; old records are ignored for reuse and are not rewritten in place.
- Reusable deterministic failures bound to an old environment identity are not reused.
- No in-place mutation of old receipt/cache records is required.
- Commit-time inspection performs bounded tool-version probes plus lockfile, manifest, and approved-environment hashing. A server restart or operator environment change can therefore make a previously successful receipt stale even when the source fingerprint is unchanged. The actionable recovery is one fresh full verification under the current environment; this behavior is intentional and must be documented in operator guidance.

### Public contracts

No tool name, input schema, annotation, or authorization behavior changes. Output schemas receive additive execution evidence only. Contract generation, tolerant-reader tests, legacy alias parity, documentation, and golden prompts must be updated together.

## Security properties

After this design is implemented:

1. Selecting a future sandbox backend covers all repository-code command paths.
2. Native execution is explicitly identified as host-account execution with advisory network/filesystem controls.
3. Requested `offline` or `source_read` intent cannot be mistaken for OS enforcement.
4. Backend inability cannot silently fall back to native execution when enforcement is required.
5. Exact-tree verification cannot survive an execution adapter, effective-policy, profile, or config-identity change.
6. Ad-hoc evidence cannot satisfy the commit gate.
7. Formatters and baseline hygiene cannot bypass the selected execution backend.
8. Public and audit evidence remain bounded and secret-safe.
9. Git, GitHub, runtime, and publication policy remain separate typed control-plane capabilities.

This slice does not claim containment of native commands. It makes that limitation explicit and prepares the system to replace native execution with an actually isolated backend.

## Test strategy

### Pure domain tests

- deterministic requested/effective policy hashes;
- structural identity completeness and typed reuse-eligibility decisions remain separate;
- stable enforcement serialization and bounds;
- invalid capability combinations;
- advisory versus enforcement-required resolution;
- environment identity schema-v2 hashing;
- secret/path exclusion from identity and public evidence;
- schema-v1 compatibility and non-reuse behavior.

### Port and adapter tests

- native policy resolution reports host-inherited network and host-account filesystem access;
- native timeout, output, and process cleanup are marked enforced;
- unsupported CPU/memory/disk/PID/network-byte limits are not marked enforced;
- prepare rejects enforcement-required isolation before process start;
- session execution rejects argv outside the closed reviewed command set;
- multi-step sessions prepare once, fail-stop between steps, post-inspect the same session, and clean up once;
- execute preserves exact argv, cwd, timeout, output bounds, and cancellation;
- cleanup is idempotent;
- artifact collection retains escape, symlink, regular-file, and byte-limit checks.

### Application routing tests

Use recording/failing execution-environment fakes to prove:

- profile steps use one coordinator-owned multi-step session with no raw-port access;
- diagnostic execution uses the coordinator and port;
- ad-hoc execution uses the coordinator and port;
- workspace formatter inspection/remediation uses the coordinator and port;
- committed-baseline hygiene uses snapshot-read-only scope through the coordinator;
- plan execution consumes delegated environment receipts instead of synthesizing a second identity;
- `ApplicationContext` cannot be constructed without a coordinator and no `None` fallback reaches the raw command executor;
- hygiene snapshot materialization reaches Git only through the Git repository port/adapter;
- no repository-code path reaches the raw command executor.

### Workspace integrity tests

- read-only diagnostic mutation is still rejected;
- mutating diagnostic artifact scope is still enforced;
- ad-hoc mutation still reports changed paths and invalidates verification;
- formatter unexpected mutation still fails closed;
- failed, timed-out, and cancelled commands still produce post-execution fingerprint checks;
- change budgets and denied-path checks remain authoritative.

### Verification and cache tests

- a profile that updates a lockfile or manifest binds post-run identity and converges after an idempotent rerun;
- a failed command with unavailable post-failure inspection is not entered into deterministic failure reuse;
- environment identity change invalidates commit eligibility;
- effective policy change invalidates commit eligibility;
- adapter kind/version change invalidates commit eligibility;
- profile/config target change invalidates commit eligibility;
- legacy receipt requires re-verification;
- schema-v1 cache entries report `environment_identity_schema_changed`, and schema-v1 deterministic-failure entries are not reused;
- advisory native reuse remains available only under exact requested/effective-policy and identity bindings;
- final verification remains non-cacheable;
- ad-hoc and iteration evidence never opens the commit gate.

### MCP and release tests

- output schemas add bounded execution evidence;
- current and compatibility aliases return identical evidence;
- tool count and annotations do not change;
- release contract and generated schema drift checks pass;
- server guidance describes requested versus effective policy accurately;
- negative prompts cannot select backend flags, mounts, credentials, or environment values.

### Integration tests

- real local Git/worktree profile, diagnostic, ad-hoc, and formatter runs through the native adapter;
- cancellation terminates descendant processes;
- toolchain/version change requires re-verification before commit;
- command failure that mutates source invalidates prior verification;
- no live credentials, real user repositories, real pushes, or external writes are used.

## Rollout and delivery boundaries

Implementation should land in dependency order, each with focused RED/GREEN tests and one independently reviewable commit:

1. typed policy, enforcement, session, identity-v2, and receipt domain models;
2. execution coordinator plus native adapter migration;
3. profile and diagnostic routing;
4. ad-hoc routing and background-operation parity;
5. formatter/hygiene and plan routing;
6. verification-receipt/commit-gate strengthening and cache migration behavior;
7. public evidence, audit, contracts, documentation, and full integration verification.

The implementation plan may split these into separate GitHub tickets or one stacked initiative, but all slices must remain on one deliberate dependency chain. A backend adapter must not start before slices 1-6 are merged or available in the same reviewed worktree.

## Follow-up designs

This design intentionally unlocks, but does not absorb, the following specs:

1. **Docker Sandboxes Execution Adapter** — `sbx` lifecycle, public HTTP/HTTPS, host/private-network isolation, workspace mounts, CPU/memory enforcement, version compatibility, and effective policy inspection.
2. **Clean Final Verification Sandbox** — persistent iteration environment plus fresh pinned final-gate environment.
3. **Credentials, Egress, and External-Write Separation** — read/fetch credentials, package pulls, egress evidence, and continued typed control-plane ownership of push/publish/cloud writes.
4. **Sandbox Mutation Recovery and Durable Lifecycle** — pre-command checkpoints, ambiguous completion, orphan processes, restart reconciliation, cleanup, and storage hygiene.

## Acceptance criteria

The design is complete when implementation can demonstrate all of the following:

- every repository-code command path is routed through a required `ExecutionCoordinator` and its private `ExecutionEnvironmentPort`;
- no future sandbox backend can be bypassed by diagnostic, ad-hoc, formatter, hygiene, or plan execution;
- native results explicitly report advisory host network/filesystem behavior;
- unsupported resource limits are never represented as enforced;
- enforcement-required policy mismatch fails before process start with no native fallback;
- all execution-capable results and audit events carry bounded requested/effective policy evidence;
- final verification receipts bind post-run environment identity, effective policy, profile target, and config identity;
- commit rejects source, environment, policy, adapter, profile, or config drift;
- one fresh verification replaces an incomplete legacy receipt;
- existing strict/relaxed execution-mode behavior, exact-tree mutation invalidation, output bounds, cancellation, audit redaction, non-force push, and draft-only publication remain intact;
- focused tests, formatter, strict typing, full test/build gates, release-contract checks, and real local worktree integration all pass on the exact final tree.
