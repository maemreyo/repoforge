# Unified Execution Boundary and Truthful Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every repository-controlled command through one required execution coordinator and bind verification, reuse, and commit eligibility to truthful effective-enforcement evidence.

**Architecture:** Add backend-neutral requested/effective policy contracts, then wrap `ExecutionEnvironmentPort` in a coordinator-owned session API. Preserve current native execution behavior, but report it as advisory host execution, re-inspect the same session after commands, and use post-run identity for final verification, plan-cache, and commit-gate decisions.

**Tech Stack:** Python 3.10+, frozen dataclasses, `Protocol`, existing `SubprocessCommandExecutor`, JSON durable state, Pydantic v2 contracts, pytest, Ruff, strict Mypy, and the existing RepoForge production gate.

## Global Constraints

- Do not add a runtime dependency.
- Do not add, remove, or rename an MCP tool; the reviewed MCP roster remains unchanged.
- Do not add shell syntax, new credentials, new network access, Docker Sandboxes, or backend-specific public inputs in this slice.
- `ExecutionCoordinator` is required application wiring; there is no `None` fallback and no automatic fallback to native execution after policy-resolution failure.
- Profile, diagnostic, ad-hoc, formatter, hygiene, and plan-stage repository commands must route through `ExecutionCoordinator`.
- Native execution reports effective `host_inherited` network and `host_account_access` filesystem behavior with advisory enforcement.
- Exact argv remains a reviewed tuple; callers cannot select backend flags, mounts, images, devices, users, credentials, or environment values.
- Profile non-zero exit status raises and fail-stops. Diagnostic, ad-hoc, and hygiene execution returns a non-zero `CommandResult` for typed parsing.
- Final verification binds post-run identity from the same prepared session, not prepare-time identity.
- Unknown tool versions make identity incomplete and reuse-ineligible, but this slice does not block native execution or commit solely because identity is incomplete.
- Advisory native reuse remains possible only when the repository accepts advisory execution and every requested/effective-policy and identity binding matches exactly.
- Existing fingerprint, denied-path, mutation-budget, cancellation, redaction, non-force push, and draft-only publication invariants remain authoritative.
- Use TDD for every task: prove RED, implement the smallest passing change, run focused tests, then commit.
- Do not regenerate release contracts merely to silence drift; inspect every additive output change first.

---

## File Structure and Ownership

### New files

- `src/repoforge/application/execution/__init__.py` — exports coordinator and request compilation APIs.
- `src/repoforge/application/execution/coordinator.py` — owns sessions, argv admission, policy checks, post-run inspection, artifact collection, and cleanup.
- `src/repoforge/application/execution/requests.py` — compiles reviewed profile, diagnostic, ad-hoc, and hygiene configuration into `ExecutionRequest`.
- `src/repoforge/application/execution/hygiene.py` — implements `HygieneGateway` with the coordinator and semantic Git snapshot reads.
- `src/repoforge/adapters/hygiene/parser.py` — pure bounded formatter-output parser with no process execution.
- `tests/test_execution_coordinator.py` — coordinator lifecycle and failure semantics.
- `tests/test_execution_routing.py` — required wiring and no-bypass proof.

### Files whose responsibility changes

- `src/repoforge/domain/execution_environment.py` — policy, enforcement, identity v2, reuse eligibility, and bounded execution evidence.
- `src/repoforge/ports/execution_environment.py` — backend session protocol and execution receipts.
- `src/repoforge/adapters/execution/native.py` — truthful native policy resolution and session state.
- `src/repoforge/application/context.py` — required coordinator dependency; raw execution port removed.
- `src/repoforge/bootstrap.py` — constructs one coordinator around the selected backend.
- `src/repoforge/application/workspace/run_profile.py` — one multi-step session and post-run final identity.
- `src/repoforge/application/workspace/run_diagnostic.py` — single-command coordinated execution with reviewed diagnostic policy.
- `src/repoforge/application/workspace/run_adhoc.py` — coordinated execution while retaining evidence-only semantics.
- `src/repoforge/application/workspace/hygiene_status.py` and `format_changed.py` — consume coordinated hygiene evidence.
- `src/repoforge/application/workspace/execute_plan.py` — consumes delegated execution evidence instead of synthesizing identity.
- `src/repoforge/domain/execution_receipt.py` — stage receipt v2 with policy bindings.
- `src/repoforge/domain/verification_dag.py` and `src/repoforge/adapters/persistence/json_iteration_cache.py` — cache schema v2 and legacy miss classification.
- `src/repoforge/domain/workspace.py` and `src/repoforge/application/workspace/commit.py` — verification receipt v2 and current-environment commit gate.
- `src/repoforge/application/verification_reuse.py` and `src/repoforge/domain/retry_guidance.py` — failure-reuse schema v2.
- `src/repoforge/ports/hygiene.py` — coordinated evidence and semantic snapshot inputs.
- `src/repoforge/adapters/hygiene/command.py` — deleted after parser extraction.
- `src/repoforge/contracts/common.py`, `src/repoforge/contracts/v2.py`, and contract goldens — additive bounded execution evidence.

---

### Task 1: Define Truthful Policy, Identity-v2, Reuse, and Port Contracts

**Files:**
- Modify: `src/repoforge/domain/execution_environment.py`
- Modify: `src/repoforge/ports/execution_environment.py`
- Modify: `src/repoforge/domain/errors.py`
- Modify: `src/repoforge/domain/__init__.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `tests/test_execution_identity.py`
- Modify: `tests/test_command_executor_error_codes.py`

**Interfaces:**
- Produces: `RequestedExecutionPolicy`, `EffectiveExecutionPolicy`, `ExecutionRequest`, `PreparedEnvironmentSession`, `EnvironmentInspection`, `ExecutionReceipt`, and `ReuseEligibility`.
- Consumes: existing `CancellationToken`, `CommandResult`, and `ArtifactResult` semantics.

- [ ] **Step 1: Write failing policy and identity-v2 tests**

Add imports and tests to `tests/test_execution_identity.py`:

```python
from repoforge.domain.execution_environment import (
    EffectiveExecutionPolicy,
    EffectiveResourceLimits,
    EnforcementAssessment,
    EnforcementLevel,
    EnforcementRequirement,
    EnvironmentIdentity,
    FilesystemAccess,
    NetworkAccess,
    RequestedExecutionPolicy,
    RequestedResourceLimits,
    ReuseIneligibilityReason,
    ToolVersion,
    assess_reuse_eligibility,
)


def requested_policy() -> RequestedExecutionPolicy:
    return RequestedExecutionPolicy(
        network=NetworkAccess.OFFLINE,
        filesystem=FilesystemAccess.SOURCE_READ,
        credentials=(),
        resources=RequestedResourceLimits(
            cpu_seconds=30,
            memory_bytes=512 * 1024 * 1024,
            disk_bytes=1024 * 1024 * 1024,
            subprocesses=8,
            network_bytes=0,
        ),
        enforcement_requirement=EnforcementRequirement.ADVISORY_BACKEND_ALLOWED,
    )


def effective_native_policy() -> EffectiveExecutionPolicy:
    return EffectiveExecutionPolicy(
        network=NetworkAccess.HOST_INHERITED,
        filesystem=FilesystemAccess.HOST_ACCOUNT_ACCESS,
        credential_capabilities=(),
        resource_limits=EffectiveResourceLimits(),
        enforcement=EnforcementAssessment(
            network=EnforcementLevel.ADVISORY,
            filesystem=EnforcementLevel.ADVISORY,
            timeout=EnforcementLevel.ENFORCED,
            output=EnforcementLevel.ENFORCED,
            process_cleanup=EnforcementLevel.ENFORCED,
            cpu=EnforcementLevel.UNSUPPORTED,
            memory=EnforcementLevel.UNSUPPORTED,
            disk=EnforcementLevel.UNSUPPORTED,
            subprocess_count=EnforcementLevel.UNSUPPORTED,
            network_bytes=EnforcementLevel.UNSUPPORTED,
        ),
        degraded=True,
        degradation_reasons=("network_not_isolated", "filesystem_not_isolated"),
    )


def complete_identity() -> EnvironmentIdentity:
    requested = requested_policy()
    effective = effective_native_policy()
    return EnvironmentIdentity(
        adapter_version="2",
        platform="linux",
        architecture="arm64",
        python_version="3.13",
        runtime_version="python/3.13",
        tools=(ToolVersion("python", "3.13"),),
        requested_policy_hash=requested.policy_hash,
        effective_policy_hash=effective.policy_hash,
        effective_network=effective.network,
        effective_filesystem=effective.filesystem,
        enforcement_assessment=effective.enforcement,
        backend_capability_hash="a" * 64,
        working_directory_policy_hash="b" * 64,
    )


def test_policy_hashes_are_stable_and_distinguish_effective_behavior() -> None:
    requested = requested_policy()
    effective = effective_native_policy()
    assert requested.policy_hash == requested_policy().policy_hash
    assert effective.policy_hash == effective_native_policy().policy_hash
    assert requested.policy_hash != effective.policy_hash


def test_identity_v2_binds_effective_policy() -> None:
    identity = complete_identity()
    assert identity.schema_version == 2
    assert identity.is_complete is True
    assert len(identity.identity_hash) == 64


def test_reuse_eligibility_is_separate_from_identity_completeness() -> None:
    eligibility = assess_reuse_eligibility(
        complete_identity(),
        requested=requested_policy(),
        effective=effective_native_policy(),
        read_only=True,
        final=False,
    )
    assert eligibility.eligible is True
    assert eligibility.reasons == ()


def test_unknown_tool_version_is_incomplete_and_not_reusable() -> None:
    identity = dataclasses.replace(
        complete_identity(),
        tools=(ToolVersion("python"),),
    )
    eligibility = assess_reuse_eligibility(
        identity,
        requested=requested_policy(),
        effective=effective_native_policy(),
        read_only=True,
        final=False,
    )
    assert identity.is_complete is False
    assert eligibility.eligible is False
    assert eligibility.reasons == (ReuseIneligibilityReason.IDENTITY_INCOMPLETE,)
```

Add `import dataclasses` at the top of the test file.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --extra dev pytest tests/test_execution_identity.py -q
```

Expected: test collection fails because the new policy, enforcement, and reuse types do not exist.

- [ ] **Step 3: Add stable execution-boundary error codes**

Add these values to `ErrorCode` in `src/repoforge/domain/errors.py`:

```python
EXECUTION_POLICY_UNSUPPORTED = "EXECUTION_POLICY_UNSUPPORTED"
EXECUTION_ENVIRONMENT_DRIFT = "EXECUTION_ENVIRONMENT_DRIFT"
```

Add table-driven coverage in `tests/test_command_executor_error_codes.py` proving both codes serialize through `RepoForgeError` and default to non-retryable. Policy-resolution failures use `EXECUTION_POLICY_UNSUPPORTED`; prepare/inspection identity mismatches use `EXECUTION_ENVIRONMENT_DRIFT`.

- [ ] **Step 4: Implement the backend-neutral domain model**

In `src/repoforge/domain/execution_environment.py`, set identity schema version to `2` and define these exact enums:

```python
class NetworkAccess(str, Enum):
    OFFLINE = "offline"
    PUBLIC_HTTP_HTTPS = "public_http_https"
    PUBLIC_GENERAL = "public_general"
    PRIVATE_APPROVED = "private_approved"
    HOST_INHERITED = "host_inherited"


class FilesystemAccess(str, Enum):
    SOURCE_READ = "source_read"
    WORKSPACE_WRITE = "workspace_write"
    MANAGED_STATE_WRITE = "managed_state_write"
    HOST_ACCOUNT_ACCESS = "host_account_access"


class CredentialCapability(str, Enum):
    GITHUB_READ = "github_read"
    PACKAGE_REGISTRY_READ = "package_registry_read"


class EnforcementRequirement(str, Enum):
    ADVISORY_BACKEND_ALLOWED = "advisory_backend_allowed"
    ENFORCEMENT_REQUIRED = "enforcement_required"


class EnforcementLevel(str, Enum):
    ENFORCED = "enforced"
    ADVISORY = "advisory"
    OBSERVED = "observed"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"


class CommandFailureMode(str, Enum):
    RAISE = "raise"
    RETURN = "return"
```

Add frozen dataclasses for requested/effective resource limits, requested/effective policy, and enforcement assessment. Implement `policy_hash` with one private compact sorted-JSON SHA-256 helper. `RequestedExecutionPolicy.__post_init__` must reject `HOST_INHERITED` and `HOST_ACCOUNT_ACCESS` because they are effective backend facts, not caller requests.

Replace `EnvironmentIdentity.cache_eligible` with `is_complete`. Add these identity-v2 fields:

```python
requested_policy_hash: str = ""
effective_policy_hash: str = ""
effective_network: NetworkAccess = NetworkAccess.HOST_INHERITED
effective_filesystem: FilesystemAccess = FilesystemAccess.HOST_ACCOUNT_ACCESS
enforcement_assessment: EnforcementAssessment = NATIVE_ADVISORY_ENFORCEMENT
backend_capability_hash: str = ""
```

Validate all hashes as lowercase SHA-256 when non-empty. Include every new field in `identity_hash` and exclude absolute paths, raw environment values, credentials, and command output.

Add `ReuseIneligibilityReason`, `ReuseEligibility`, and `assess_reuse_eligibility`. Advisory native execution is eligible only when:

- enforcement requirement is `ADVISORY_BACKEND_ALLOWED`;
- stage is read-only and non-final;
- identity is complete;
- degradation reasons are limited to `network_not_isolated` and `filesystem_not_isolated`;
- requested and effective policy hashes are present.

Define typed bounded evidence in the same module so all later runners can use it:

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
    identity_schema_version: int
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

Add:

```text
build_execution_evidence(
    requested: RequestedExecutionPolicy,
    identity: EnvironmentIdentity,
    effective: EffectiveExecutionPolicy,
    warnings: Sequence[str] = (),
) -> ExecutionEvidence
```

The domain constructor does not import port types. It uses the identity's adapter kind, schema version, identity hash, and policy hashes plus the effective policy's behavior and enforcement assessment. It deduplicates and sorts warnings, limits them to ten entries, bounds each entry to 500 characters, and rejects requested/effective hashes that do not match the identity.

Add Task 1 tests for deterministic evidence serialization, positive bounded schema version, warning ordering/bounds, and absence of raw environment values.

- [ ] **Step 5: Rewrite the execution port contracts**

Replace `ApprovedExecution` and the old identity lifecycle in `src/repoforge/ports/execution_environment.py` with:

```python
class ExecutionScopeKind(str, Enum):
    WORKSPACE = "workspace"
    SNAPSHOT_READ_ONLY = "snapshot_read_only"


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    kind: ExecutionScopeKind
    root: Path
    command_cwd: Path
    workspace_id: str | None
    working_directory_policy: str


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    scope: ExecutionScope
    reviewed_commands: tuple[tuple[str, ...], ...]
    requested_policy: RequestedExecutionPolicy
    timeout_seconds: int
    output_limit: int
    artifact_paths: tuple[str, ...]
    failure_mode: CommandFailureMode
    cancel_token: CancellationToken | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentDoctorResult:
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedEnvironmentSession:
    session_id: str
    identity: EnvironmentIdentity
    requested_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    effective_policy_hash: str


@dataclass(frozen=True, slots=True)
class EnvironmentInspection:
    identity: EnvironmentIdentity
    requested_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    effective_policy_hash: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    argv: tuple[str, ...]
    session_start_identity_hash: str
    requested_policy_hash: str
    effective_policy_hash: str
    effective_policy: EffectiveExecutionPolicy
    result: CommandResult
    artifacts: tuple[ArtifactResult, ...] = ()
```

Define `ExecutionEnvironmentPort` with these exact methods and return types:

```text
doctor(request: ExecutionRequest) -> EnvironmentDoctorResult
inspect(request: ExecutionRequest) -> EnvironmentInspection
prepare(request: ExecutionRequest) -> PreparedEnvironmentSession
execute(session: PreparedEnvironmentSession, argv: tuple[str, ...]) -> CommandResult
inspect_session(session: PreparedEnvironmentSession, request: ExecutionRequest) -> EnvironmentInspection
collect_artifacts(session: PreparedEnvironmentSession, artifact_paths: Sequence[str]) -> tuple[ArtifactResult, ...]
cleanup(session: PreparedEnvironmentSession) -> None
```

`ExecutionRequest.__post_init__` must validate non-empty bounded commands, cwd containment, timeout/output bounds, duplicate-free reviewed commands, and normalized bounded artifact paths.

- [ ] **Step 6: Run focused domain verification**

```bash
uv run --extra dev pytest \
  tests/test_execution_identity.py \
  tests/test_command_executor_error_codes.py -q
uv run --extra dev mypy \
  src/repoforge/domain/errors.py \
  src/repoforge/domain/execution_environment.py \
  src/repoforge/ports/execution_environment.py
```

Expected: all tests pass and strict Mypy reports no issues.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/repoforge/domain/execution_environment.py \
  src/repoforge/ports/execution_environment.py \
  src/repoforge/domain/errors.py \
  src/repoforge/domain/__init__.py \
  src/repoforge/ports/__init__.py \
  tests/test_execution_identity.py \
  tests/test_command_executor_error_codes.py
git commit -m "refactor(execution): define truthful environment contracts"
```

---

### Task 2: Add Coordinator, Test Fakes, Native Sessions, and Request Compilation

**Files:**
- Create: `src/repoforge/application/execution/__init__.py`
- Create: `src/repoforge/application/execution/coordinator.py`
- Create: `src/repoforge/application/execution/requests.py`
- Modify: `src/repoforge/adapters/execution/native.py`
- Modify: `src/repoforge/adapters/execution/native_identity.py`
- Modify: `src/repoforge/testing/fakes.py`
- Create: `tests/test_execution_coordinator.py`
- Modify: `tests/test_native_execution_environment.py`

**Interfaces:**
- Consumes: Task 1 execution contracts.
- Produces: `ExecutionCoordinator.session()`, `ExecutionCoordinator.run()`, `ExecutionSession.execute()`, and request compiler functions used by Tasks 3 through 8.

- [ ] **Step 1: Add a deterministic recording backend fake**

Add to `src/repoforge/testing/fakes.py`:

```python
class RecordingExecutionEnvironment:
    def __init__(
        self,
        *,
        result: CommandResult | None = None,
        initial: EnvironmentInspection | None = None,
        post: EnvironmentInspection | None = None,
        drift_on_prepare: bool = False,
        fail_post_inspection: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.result = result or CommandResult(("tool", "check"), ".", 0, "ok", "")
        self.initial = initial or fake_environment_inspection()
        self.post = post or self.initial
        self.drift_on_prepare = drift_on_prepare
        self.fail_post_inspection = fail_post_inspection
        self.requests: list[ExecutionRequest] = []

    def doctor(self, request: ExecutionRequest) -> EnvironmentDoctorResult:
        self.calls.append("doctor")
        self.requests.append(request)
        return EnvironmentDoctorResult()

    def inspect(self, request: ExecutionRequest) -> EnvironmentInspection:
        self.calls.append("inspect")
        return self.initial

    def prepare(self, request: ExecutionRequest) -> PreparedEnvironmentSession:
        self.calls.append("prepare")
        identity = self.initial.identity
        if self.drift_on_prepare:
            identity = dataclasses.replace(identity, adapter_version="drifted")
        return PreparedEnvironmentSession(
            session_id="recording-session",
            identity=identity,
            requested_policy_hash=self.initial.requested_policy_hash,
            effective_policy=self.initial.effective_policy,
            effective_policy_hash=self.initial.effective_policy_hash,
        )

    def execute(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
    ) -> CommandResult:
        self.calls.append("execute")
        return dataclasses.replace(self.result, argv=argv)

    def inspect_session(
        self,
        session: PreparedEnvironmentSession,
        request: ExecutionRequest,
    ) -> EnvironmentInspection:
        self.calls.append("inspect_session")
        if self.fail_post_inspection:
            raise RepoForgeError(
                "post inspection unavailable",
                code=ErrorCode.CHECK_EVIDENCE_UNAVAILABLE,
            )
        return self.post

    def collect_artifacts(
        self,
        session: PreparedEnvironmentSession,
        artifact_paths: Sequence[str],
    ) -> tuple[ArtifactResult, ...]:
        self.calls.append("collect_artifacts")
        return ()

    def cleanup(self, session: PreparedEnvironmentSession) -> None:
        self.calls.append("cleanup")
```

Add these complete factories in the same file so later tests do not duplicate policy construction:

```python
def fake_requested_execution_policy() -> RequestedExecutionPolicy:
    return RequestedExecutionPolicy(
        network=NetworkAccess.OFFLINE,
        filesystem=FilesystemAccess.SOURCE_READ,
        credentials=(),
        resources=RequestedResourceLimits(),
        enforcement_requirement=EnforcementRequirement.ADVISORY_BACKEND_ALLOWED,
    )


def fake_effective_execution_policy() -> EffectiveExecutionPolicy:
    return EffectiveExecutionPolicy(
        network=NetworkAccess.HOST_INHERITED,
        filesystem=FilesystemAccess.HOST_ACCOUNT_ACCESS,
        credential_capabilities=(),
        resource_limits=EffectiveResourceLimits(),
        enforcement=NATIVE_ADVISORY_ENFORCEMENT,
        degraded=True,
        degradation_reasons=("network_not_isolated", "filesystem_not_isolated"),
    )


def fake_environment_inspection() -> EnvironmentInspection:
    requested = fake_requested_execution_policy()
    effective = fake_effective_execution_policy()
    identity = EnvironmentIdentity(
        adapter_kind=EnvironmentAdapterKind.NATIVE_REVIEWED,
        adapter_version="2",
        platform="test-platform",
        architecture="test-architecture",
        python_version="3.13",
        runtime_version="python/3.13",
        tools=(ToolVersion("tool", "1.0"),),
        requested_policy_hash=requested.policy_hash,
        effective_policy_hash=effective.policy_hash,
        effective_network=effective.network,
        effective_filesystem=effective.filesystem,
        enforcement_assessment=effective.enforcement,
        backend_capability_hash="a" * 64,
        working_directory_policy_hash="b" * 64,
    )
    return EnvironmentInspection(
        identity=identity,
        requested_policy_hash=requested.policy_hash,
        effective_policy=effective,
        effective_policy_hash=effective.policy_hash,
        warnings=(),
    )


def fake_execution_request(
    root: Path,
    *,
    failure_mode: CommandFailureMode = CommandFailureMode.RETURN,
) -> ExecutionRequest:
    return ExecutionRequest(
        scope=ExecutionScope(
            kind=ExecutionScopeKind.WORKSPACE,
            root=root,
            command_cwd=root,
            workspace_id="workspace-test",
            working_directory_policy=".",
        ),
        reviewed_commands=(("tool", "check"),),
        requested_policy=fake_requested_execution_policy(),
        timeout_seconds=30,
        output_limit=2_000,
        artifact_paths=(),
        failure_mode=failure_mode,
    )
```

Import every referenced execution-domain and execution-port type at the top of `src/repoforge/testing/fakes.py`; do not use local imports inside these factories.

- [ ] **Step 2: Write failing coordinator lifecycle tests**

Create `tests/test_execution_coordinator.py`:

```python
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from repoforge.application.execution.coordinator import ExecutionCoordinator
from repoforge.domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError
from repoforge.domain.execution_environment import CommandFailureMode
from repoforge.ports.command import CommandResult
from repoforge.testing.fakes import RecordingExecutionEnvironment, fake_execution_request


def test_session_prepares_once_executes_reviewed_argv_and_cleans_once(tmp_path: Path) -> None:
    port = RecordingExecutionEnvironment()
    coordinator = ExecutionCoordinator(port)
    request = fake_execution_request(tmp_path)

    with coordinator.session(request) as session:
        receipt = session.execute(("tool", "check"))
        current = session.inspect_current()

    assert receipt.argv == ("tool", "check")
    assert current.identity.identity_hash == port.post.identity.identity_hash
    assert port.calls == [
        "doctor",
        "inspect",
        "prepare",
        "execute",
        "collect_artifacts",
        "inspect_session",
        "cleanup",
    ]


def test_session_rejects_unreviewed_argv_before_backend_execution(tmp_path: Path) -> None:
    port = RecordingExecutionEnvironment()
    coordinator = ExecutionCoordinator(port)

    with coordinator.session(fake_execution_request(tmp_path)) as session:
        with pytest.raises(SecurityError, match="reviewed command set"):
            session.execute(("tool", "different"))

    assert "execute" not in port.calls
    assert port.calls[-1] == "cleanup"


def test_prepare_identity_drift_fails_before_command_start(tmp_path: Path) -> None:
    port = RecordingExecutionEnvironment(drift_on_prepare=True)
    coordinator = ExecutionCoordinator(port)

    with pytest.raises(RepoForgeError) as raised:
        with coordinator.session(fake_execution_request(tmp_path)):
            raise AssertionError("session must not open")

    assert raised.value.code is ErrorCode.EXECUTION_ENVIRONMENT_DRIFT
    assert "execute" not in port.calls
    assert port.calls[-1] == "cleanup"


def test_failure_mode_controls_nonzero_result_semantics(tmp_path: Path) -> None:
    port = RecordingExecutionEnvironment(
        result=CommandResult(("tool", "check"), str(tmp_path), 3, "", "failed")
    )
    coordinator = ExecutionCoordinator(port)
    returning = fake_execution_request(tmp_path, failure_mode=CommandFailureMode.RETURN)
    raising = dataclasses.replace(returning, failure_mode=CommandFailureMode.RAISE)

    receipt, _ = coordinator.run(returning, ("tool", "check"))
    assert receipt.result.returncode == 3
    with pytest.raises(CommandError):
        coordinator.run(raising, ("tool", "check"))
```

- [ ] **Step 3: Run the coordinator tests and verify RED**

```bash
uv run --extra dev pytest tests/test_execution_coordinator.py -q
```

Expected: import failure because `ExecutionCoordinator` does not exist.

- [ ] **Step 4: Implement coordinator-owned sessions**

Create `src/repoforge/application/execution/coordinator.py`. The public methods must have these signatures:

```text
ExecutionCoordinator.inspect(request: ExecutionRequest) -> EnvironmentInspection
ExecutionCoordinator.session(request: ExecutionRequest) -> AbstractContextManager[ExecutionSession]
ExecutionCoordinator.run(request: ExecutionRequest, argv: tuple[str, ...]) -> tuple[ExecutionReceipt, EnvironmentInspection]
ExecutionSession.execute(argv: tuple[str, ...]) -> ExecutionReceipt
ExecutionSession.inspect_current() -> EnvironmentInspection
```

Implement these invariants:

1. `session()` calls doctor and pre-inspection before prepare, then merges bounded doctor/backend warnings into the coordinator-owned inspection.
2. `prepare()` identity and policy hashes must match pre-inspection; otherwise cleanup and raise `EXECUTION_ENVIRONMENT_DRIFT`.
3. `ExecutionSession.execute()` rejects argv absent from `request.reviewed_commands`.
4. Backend execution always returns `CommandResult`; the coordinator applies `CommandFailureMode`.
5. Artifact collection happens only after a returned command result.
6. `run()` opens one session, executes one argv, post-inspects the same session, and closes it.
7. Cleanup runs exactly once in `finally`, including drift, command failure, timeout, and cancellation paths.
8. `ENFORCEMENT_REQUIRED` fails before command start when effective enforcement cannot satisfy the request.

- [ ] **Step 5: Implement reviewed request compilation**

Create `src/repoforge/application/execution/requests.py` with these functions and mappings:

```text
resolve_profile_command_cwd(
    workspace: Path,
    working_directory: str | None,
) -> Path

profile_execution_request(
    *,
    workspace_id: str,
    workspace: Path,
    command_cwd: Path,
    commands: tuple[tuple[str, ...], ...],
    working_directory_policy: str,
    timeout_seconds: int,
    output_limit: int,
    resource_budget: ResourceBudget,
    cancel_token: CancellationToken | None,
) -> ExecutionRequest

diagnostic_execution_request(
    *,
    workspace_id: str,
    workspace: Path,
    command_cwd: Path,
    argv: tuple[str, ...],
    profile: DiagnosticProfileConfig,
    resource_budget: ResourceBudget,
    cancel_token: CancellationToken | None,
) -> ExecutionRequest

adhoc_execution_request(
    *,
    workspace_id: str,
    workspace: Path,
    command_cwd: Path,
    argv: tuple[str, ...],
    timeout_seconds: int,
    output_limit: int,
    resource_budget: ResourceBudget,
    cancel_token: CancellationToken | None,
) -> ExecutionRequest

hygiene_execution_request(
    *,
    scope: ExecutionScope,
    argv: tuple[str, ...],
    timeout_seconds: int,
    output_limit: int,
    resource_budget: ResourceBudget,
    failure_mode: CommandFailureMode,
) -> ExecutionRequest
```

Use exact policy mappings:

```text
profile                         -> public_general / workspace_write / RAISE
diagnostic local_only           -> offline
diagnostic restricted           -> public_http_https
diagnostic external             -> public_general
diagnostic read_only            -> source_read
diagnostic workspace_write      -> workspace_write
ad-hoc advisory_local_only       -> offline / workspace_write / RETURN
formatter local_only check       -> offline / source_read / RETURN
formatter local_only remediation -> offline / workspace_write / RETURN
```

Every request must carry the repository's reviewed resource budget, bounded timeout/output, exact cwd, exact reviewed command set, declared artifact paths, and cancellation token.

- [ ] **Step 6: Rewrite `NativeReviewedAdapter` as a truthful session backend**

Use a lock-protected `dict[str, ExecutionRequest]` for active native sessions and `uuid.uuid4().hex` for opaque session IDs. Native effective policy is always:

```text
network             = host_inherited, advisory
filesystem          = host_account_access, advisory
timeout              = enforced
output               = enforced
process cleanup      = enforced
CPU                  = unsupported
memory               = unsupported
disk                 = unsupported
subprocess count     = unsupported
network bytes        = unsupported
```

`execute()` retrieves the stored request and calls `CommandExecutor.run` with exact cwd, timeout, output limit, cancellation token, and `check=False`. `inspect_session()` re-hashes the same workspace/session request. `cleanup()` removes the session idempotently. `prepare()` rejects an enforcement-required isolation request before storing a session.

- [ ] **Step 7: Update native tests and run focused verification**

Update `tests/test_native_execution_environment.py` to assert:

- requested `offline` resolves to effective advisory `host_inherited`;
- requested `source_read` resolves to effective advisory `host_account_access`;
- timeout/output/cancellation are passed exactly;
- artifact escape, symlink, and size limits remain enforced;
- cleanup is idempotent;
- enforcement-required isolation fails before execution.

Run:

```bash
uv run --extra dev pytest tests/test_execution_coordinator.py tests/test_native_execution_environment.py -q
uv run --extra dev mypy src/repoforge/application/execution src/repoforge/adapters/execution
```

Expected: all tests pass and strict Mypy reports no issues.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/repoforge/application/execution \
  src/repoforge/adapters/execution \
  src/repoforge/testing/fakes.py \
  tests/test_execution_coordinator.py \
  tests/test_native_execution_environment.py
git commit -m "refactor(execution): add coordinator and native sessions"
```

---

### Task 3: Make Coordinator Wiring Required and Prove No Fallback

**Files:**
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_bootstrap_factories.py`
- Create: `tests/test_execution_routing.py`

**Interfaces:**
- Consumes: `ExecutionCoordinator` and `NativeReviewedAdapter` from Task 2.
- Produces: required `ApplicationContext.execution: ExecutionCoordinator` and the shared `build_service_with_recording_execution()` test helper in `tests/conftest.py`.

- [ ] **Step 1: Write failing required-wiring tests**

Add this shared helper to `tests/conftest.py`:

```python
def build_service_with_recording_execution(
    env: ForgeEnvironment,
    port: RecordingExecutionEnvironment,
) -> CodingService:
    config = load_config(env.config_path)
    application = build_application(
        config,
        overrides=AdapterOverrides(execution_environment=port),
    )
    return CodingService(config, application=application)
```

Import `RecordingExecutionEnvironment` in `tests/conftest.py`. Create `tests/test_execution_routing.py` with:

```python
from __future__ import annotations

from conftest import ForgeEnvironment

from repoforge.application.execution.coordinator import ExecutionCoordinator


def test_bootstrap_always_exposes_required_coordinator(forge_env: ForgeEnvironment) -> None:
    context = forge_env.service.application.context
    assert isinstance(context.execution, ExecutionCoordinator)
    assert not hasattr(context, "execution_environment")
```

The repository-runner no-fallback assertion is added only after profile, diagnostic, and ad-hoc migration in Task 5. Do not introduce a temporary xfail.

- [ ] **Step 2: Run bootstrap tests and verify RED**

```bash
uv run --extra dev pytest tests/test_bootstrap_factories.py tests/test_execution_routing.py -q
```

Expected: the required coordinator assertion fails because context still exposes an optional raw port.

- [ ] **Step 3: Make the coordinator a required context field**

In `ApplicationContext`, remove:

```python
execution_environment: ExecutionEnvironmentPort | None = None
```

Add the non-optional field before optional/defaulted fields:

```python
execution: ExecutionCoordinator
```

Keep `commands: CommandExecutor` for Git, GitHub, runtime, onboarding, and execution-adapter infrastructure only.

In `bootstrap.build_application`:

```python
execution_environment = o.execution_environment or NativeReviewedAdapter(
    command,
    max_artifact_bytes=config.server.max_file_bytes,
)
execution = ExecutionCoordinator(execution_environment)
```

Pass `execution=execution` into `ApplicationContext`. Never pass the raw port into context. Keep `AdapterOverrides.execution_environment` as the backend test seam so recording backends are still wrapped by the real coordinator.

- [ ] **Step 4: Run bootstrap and context tests**

```bash
uv run --extra dev pytest tests/test_bootstrap_factories.py tests/test_execution_routing.py -q
uv run --extra dev mypy src/repoforge/application/context.py src/repoforge/bootstrap.py
```

Expected: required-wiring tests pass with no skipped or xfailed tests.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/repoforge/application/context.py \
  src/repoforge/bootstrap.py \
  tests/conftest.py \
  tests/test_bootstrap_factories.py \
  tests/test_execution_routing.py
git commit -m "refactor(application): require execution coordinator wiring"
```

---

### Task 4: Migrate Multi-Step Profiles and Bind Post-Run Verification Identity

**Files:**
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/domain/workspace.py`
- Modify: `src/repoforge/application/verification_reuse.py`
- Modify: `src/repoforge/domain/retry_guidance.py`
- Modify: `tests/test_background_run_profile.py`
- Modify: `tests/test_retry_guidance.py`
- Modify: `tests/test_execution_routing.py`

**Interfaces:**
- Consumes: `ExecutionCoordinator.session()`, `profile_execution_request()`, and `build_service_with_recording_execution()`.
- Produces: profile results with post-run execution evidence, verification receipt v2 fields, and failure-reuse v2 bindings.

- [ ] **Step 1: Write failing one-session and convergence tests**

Add to `tests/test_background_run_profile.py`:

```python
def test_profile_uses_one_session_and_one_post_run_inspection(
    forge_env: ForgeEnvironment,
) -> None:
    port = RecordingExecutionEnvironment()
    service = build_service_with_recording_execution(forge_env, port)
    created = service.workspace_create("demo", "profile routing")
    Path(created["path"], "hello.txt").write_text("changed\n", encoding="utf-8")

    result = service.workspace_run_profile(created["workspace_id"], "full")

    assert result["execution_evidence"]["environment_identity_hash"] == (
        port.post.identity.identity_hash
    )
    assert port.calls.count("prepare") == 1
    assert port.calls.count("execute") == 1
    assert port.calls.count("inspect_session") == 1
    assert port.calls.count("cleanup") == 1
```

Add an idempotent profile fixture that writes `uv.lock` only when content differs, then assert two successful runs converge on the same fingerprint and post-run environment identity. Add a failure test using `RecordingExecutionEnvironment(fail_post_inspection=True)` and assert the same failed profile executes twice because incomplete post-failure identity cannot be reused.

- [ ] **Step 2: Run profile tests and verify RED**

```bash
uv run --extra dev pytest tests/test_background_run_profile.py tests/test_retry_guidance.py -q
```

Expected: new execution-evidence and session-count assertions fail because profile still owns raw port lifecycle and stores prepare-time identity.

- [ ] **Step 3: Replace profile raw-port/fallback logic with one coordinator session**

Compile one request with `profile_execution_request()`. Inside the existing workspace lock, use one context-managed session for all reviewed steps:

```python
with self.ctx.execution.session(request) as session:
    for step_index, verification_step in enumerate(steps):
        accepted = accepted_no_regression_step(verification_step)
        if accepted is not None:
            results.append(accepted)
            stage_telemetry.append((0.0, (time.monotonic() - run_started) * 1_000))
            continue
        if on_before_command is not None:
            on_before_command()
        stage_started = time.monotonic()
        try:
            execution_receipt = session.execute(verification_step.command)
        except CommandError as exc:
            failure_inspection = safe_post_failure_inspection(session)
            record_command_failure(
                exc,
                verification_step,
                step_index,
                (time.monotonic() - stage_started) * 1_000,
                failure_inspection,
            )
            raise
        results.append(execution_receipt.result)
        stage_telemetry.append(
            (
                (time.monotonic() - stage_started) * 1_000,
                (time.monotonic() - run_started) * 1_000,
            )
        )
    final_inspection = session.inspect_current()
```

Implement `safe_post_failure_inspection()` as a private helper that returns `EnvironmentInspection | None` and catches only bounded RepoForge environment-inspection errors. Delete all `execution_environment is not None`, `prepared_environment`, raw `self.ctx.commands.run`, and manual backend cleanup branches.

- [ ] **Step 4: Advance failure reuse to schema v2**

Extend `FailureReuseBinding` with:

```python
identity_schema_version: int
requested_policy_hash: str
effective_policy_hash: str
```

Set `FAILURE_REUSE_SCHEMA_VERSION = 2`. Create a failure-reuse binding only when:

- post-failure inspection exists;
- identity is complete;
- workspace fingerprint did not change;
- `assess_reuse_eligibility()` permits advisory reuse;
- target, command-source, config, requested policy, effective policy, and environment identity hashes are current.

Schema-v1 metadata remains readable as workspace metadata but never returns a hit.

- [ ] **Step 5: Write verification receipt v2 fields**

Add optional compatibility fields to `VerificationReceipt`:

```python
execution_identity_schema_version: int | None = None
requested_policy_hash: str | None = None
effective_policy_hash: str | None = None
adapter_kind: str | None = None
profile_target_hash: str | None = None
config_identity_hash: str | None = None
```

On successful verification, store post-run identity and policy hashes from `final_inspection`, adapter kind, current profile target hash, current config identity, and the existing post-run workspace fingerprint. Add a typed `execution_evidence` field to `WorkspaceRunProfileResult`. Background admission remains unchanged; terminal background result carries the evidence.

- [ ] **Step 6: Run profile, cancellation, and reuse tests**

```bash
uv run --extra dev pytest \
  tests/test_background_run_profile.py \
  tests/test_retry_guidance.py \
  tests/test_integration.py \
  tests/test_execution_routing.py -q
```

Expected: multi-step, no-regression, structured failure, reusable failure, timeout, cancellation, and lockfile convergence tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/repoforge/application/workspace/run_profile.py \
  src/repoforge/domain/workspace.py \
  src/repoforge/application/verification_reuse.py \
  src/repoforge/domain/retry_guidance.py \
  tests/test_background_run_profile.py \
  tests/test_retry_guidance.py \
  tests/test_execution_routing.py
git commit -m "refactor(profiles): route multi-step runs through one session"
```

---

### Task 5: Migrate Diagnostics and Ad-Hoc Execution

**Files:**
- Modify: `src/repoforge/application/workspace/run_diagnostic.py`
- Modify: `src/repoforge/application/workspace/run_adhoc.py`
- Modify: `tests/test_workspace_diagnostics.py`
- Modify: `tests/test_workspace_adhoc.py`
- Modify: `tests/test_background_run_profile.py`
- Modify: `tests/test_execution_routing.py`

**Interfaces:**
- Consumes: `diagnostic_execution_request()`, `adhoc_execution_request()`, `ExecutionCoordinator.run()`, and the recording-service helper.
- Produces: diagnostic/ad-hoc execution evidence with unchanged parser, mutation, and commit-gate behavior.

- [ ] **Step 1: Write failing diagnostic truthfulness tests**

Add to `tests/test_workspace_diagnostics.py`:

```python
def test_diagnostic_reports_requested_and_effective_policy(
    forge_env: ForgeEnvironment,
) -> None:
    port = RecordingExecutionEnvironment()
    service = build_service_with_recording_execution(forge_env, port)
    workspace_id = service.workspace_create("demo", "diagnostic routing")["workspace_id"]

    result = service.workspace_run_diagnostic(
        workspace_id,
        "pytest-target",
        "hello.txt",
    )

    evidence = result["execution_evidence"]
    assert evidence["requested_network"] == "offline"
    assert evidence["effective_network"] == "host_inherited"
    assert evidence["enforcement"]["network"] == "advisory"
    assert port.calls.count("execute") == 1


def test_nonzero_diagnostic_result_is_returned_to_parser(
    forge_env: ForgeEnvironment,
) -> None:
    port = RecordingExecutionEnvironment(
        result=CommandResult(("tool", "check"), ".", 1, "1 failed in 0.01s", "")
    )
    service = build_service_with_recording_execution(forge_env, port)
    workspace_id = service.workspace_create("demo", "diagnostic parse")["workspace_id"]

    result = service.workspace_run_diagnostic(
        workspace_id,
        "pytest-target",
        "hello.txt",
    )

    assert result["returncode"] == 1
    assert result["outcome"] == "failed"
```

- [ ] **Step 2: Write failing ad-hoc routing tests**

Add to `tests/test_workspace_adhoc.py`:

```python
def test_adhoc_uses_coordinator_but_remains_evidence_only(tmp_path: Path) -> None:
    env = _relaxed_env(tmp_path)
    port = RecordingExecutionEnvironment()
    service = build_service_with_recording_execution(env, port)
    workspace_id = service.workspace_create("demo", "adhoc boundary")["workspace_id"]

    result = service.workspace_run_adhoc(workspace_id, ["python3", "--version"])

    assert result["evidence_only"] is True
    assert result["satisfies_commit_gate"] is False
    assert result["execution_evidence"]["requested_network"] == "offline"
    assert result["execution_evidence"]["effective_network"] == "host_inherited"
    assert port.calls.count("execute") == 1
```

Extend the existing real background cancellation test to assert the cancellation token reaches the coordinated request and no verification receipt is created.

- [ ] **Step 3: Run diagnostic/ad-hoc tests and verify RED**

```bash
uv run --extra dev pytest \
  tests/test_workspace_diagnostics.py \
  tests/test_workspace_adhoc.py \
  tests/test_execution_routing.py -q
```

Expected: routing/evidence assertions fail because both runners still call `ctx.commands.run` directly.

- [ ] **Step 4: Route diagnostics through `ExecutionCoordinator.run()`**

Inside the existing lock, replace identity-only and raw command logic with:

```python
request = diagnostic_execution_request(
    workspace_id=command.workspace_id,
    workspace=locked_workspace,
    command_cwd=command_cwd,
    argv=resolved.argv,
    profile=locked_profile,
    resource_budget=locked_repo.resource_budget,
    cancel_token=None,
)
execution_receipt, post_inspection = self.ctx.execution.run(request, resolved.argv)
result = execution_receipt.result
```

Use `post_inspection` for reuse binding. Keep expected-fingerprint, selector, parser, expectation, artifact, mutation, TDD intent, and verification invalidation behavior unchanged. Add bounded `execution_evidence` to the result and audit projection.

- [ ] **Step 5: Route ad-hoc execution through `ExecutionCoordinator.run()`**

Inside the existing workspace lock:

```python
request = adhoc_execution_request(
    workspace_id=c.workspace_id,
    workspace=locked_workspace,
    command_cwd=command_cwd,
    argv=argv,
    timeout_seconds=locked_repo.adhoc_timeout_seconds,
    output_limit=self.ctx.config.server.max_tool_output_chars,
    resource_budget=locked_repo.resource_budget,
    cancel_token=cancel_token,
)
execution_receipt, post_inspection = self.ctx.execution.run(request, argv)
result = execution_receipt.result
```

Do not weaken runner allowlisting, basename validation, relaxed-mode requirement, working-directory containment, fingerprint invalidation, enrollment nudge, evidence-only status, or commit-gate guidance. Preserve legacy `network_policy="advisory_local_only"` and make `execution_evidence` the effective source of truth.

- [ ] **Step 6: Add the no-fallback routing assertion and run focused tests**

Add this test to `tests/test_execution_routing.py` after all three runners have been migrated:

```python
def test_workspace_runners_have_no_optional_execution_fallback_branches() -> None:
    files = (
        Path("src/repoforge/application/workspace/run_profile.py"),
        Path("src/repoforge/application/workspace/run_diagnostic.py"),
        Path("src/repoforge/application/workspace/run_adhoc.py"),
    )
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "execution_environment is not None" not in source
        assert "self.ctx.commands.run" not in source
        assert tree.body
```

Add `import ast` and `from pathlib import Path` to that test module, then run:

```bash
uv run --extra dev pytest \
  tests/test_workspace_diagnostics.py \
  tests/test_workspace_adhoc.py \
  tests/test_background_run_profile.py \
  tests/test_execution_routing.py -q
```

Expected: all tests pass; no `self.ctx.commands.run` remains in profile, diagnostic, or ad-hoc runners.

- [ ] **Step 7: Commit Task 5**

```bash
git add src/repoforge/application/workspace/run_diagnostic.py \
  src/repoforge/application/workspace/run_adhoc.py \
  tests/test_workspace_diagnostics.py \
  tests/test_workspace_adhoc.py \
  tests/test_background_run_profile.py \
  tests/test_execution_routing.py
git commit -m "refactor(workspace): unify diagnostic and adhoc execution"
```

---

### Task 6: Replace Command-Backed Hygiene with Coordinated Execution and Semantic Git Snapshots

**Files:**
- Create: `src/repoforge/application/execution/hygiene.py`
- Create: `src/repoforge/adapters/hygiene/parser.py`
- Modify: `src/repoforge/adapters/hygiene/__init__.py`
- Delete: `src/repoforge/adapters/hygiene/command.py`
- Modify: `src/repoforge/ports/hygiene.py`
- Modify: `src/repoforge/application/workspace/hygiene_status.py`
- Modify: `src/repoforge/application/workspace/format_changed.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_workspace_hygiene.py`
- Modify: `tests/test_execution_routing.py`

**Interfaces:**
- Consumes: `ExecutionCoordinator`, `GitRepository.read_snapshot_blob()`, and `hygiene_execution_request()`.
- Produces: `CoordinatedHygieneGateway` implementing `HygieneGateway` without a raw `CommandExecutor`.

- [ ] **Step 1: Write failing hygiene routing and architecture tests**

Add to `tests/test_workspace_hygiene.py`:

```python
def test_workspace_hygiene_executes_through_coordinator(
    forge_env: ForgeEnvironment,
) -> None:
    port = RecordingExecutionEnvironment()
    service = build_service_with_recording_execution(forge_env, port)
    workspace_id = service.workspace_create("demo", "hygiene boundary")["workspace_id"]

    result = service.workspace_hygiene_status(workspace_id)

    assert result["status"] == "available"
    assert result["execution_evidence"]["requested_network"] == "offline"
    assert port.calls.count("execute") >= 1


def test_hygiene_adapter_has_no_command_executor_dependency() -> None:
    parser_source = Path("src/repoforge/adapters/hygiene/parser.py").read_text(
        encoding="utf-8"
    )
    assert "CommandExecutor" not in parser_source
    assert "subprocess" not in parser_source
```

Add a fake Git wrapper that records `read_snapshot_blob` calls, then assert baseline inspection reads only selected validated paths from the exact base commit.

- [ ] **Step 2: Run hygiene tests and verify RED**

```bash
uv run --extra dev pytest tests/test_workspace_hygiene.py tests/test_execution_routing.py -q
```

Expected: tests fail because `CommandHygieneGateway` still owns a raw executor and `git archive`.

- [ ] **Step 3: Extract pure validation and parsing**

Move path validation and Ruff-format parsing into `src/repoforge/adapters/hygiene/parser.py` with these signatures:

```text
validate_hygiene_paths(paths: tuple[str, ...], policy: FormatterPolicy) -> tuple[str, ...]
parse_hygiene_result(policy: FormatterPolicy, result: CommandResult, environment_identity: str) -> HygieneInspection
```

Preserve return-code `{0, 1}` behavior for formatter checks, bounded excerpts, output-truncation flags, normalized finding identity, and `CommandError` for unsupported parser or other exit codes.

- [ ] **Step 4: Implement `CoordinatedHygieneGateway`**

Create `src/repoforge/application/execution/hygiene.py` with constructor:

```python
class CoordinatedHygieneGateway:
    def __init__(self, execution: ExecutionCoordinator, git: GitRepository) -> None:
        self._execution = execution
        self._git = git
```

Extend `HygieneGateway` method parameters to carry `RepositoryConfig` and its `resource_budget` per call. For workspace check/fix, build `WORKSPACE` scopes and execute the exact fixed argv plus validated paths.

For baseline inspection:

1. Create a RepoForge-owned `TemporaryDirectory`.
2. For each selected path, call `GitRepository.read_snapshot_blob(repository, repo, commit_sha, path)`.
3. Accept only regular-file modes beginning with `100`.
4. Enforce aggregate `max_archive_bytes` before writing.
5. Write only selected blobs beneath the temporary root.
6. Run the formatter under `SNAPSHOT_READ_ONLY` scope.
7. Let the temporary-directory context clean up all files.

Do not call raw `git archive` or expose temporary paths publicly.

- [ ] **Step 5: Wire coordinated hygiene and preserve integrity checks**

In bootstrap:

```python
hygiene = o.hygiene or CoordinatedHygieneGateway(execution, git)
```

Update `WorkspaceHygieneStatusReader` and `WorkspaceChangedFormatter` to pass repository/resource policy and return bounded execution evidence. Preserve baseline cache keys, selected paths, unexpected-mutation detection, stale fingerprint checks, and verification invalidation.

- [ ] **Step 6: Run hygiene and real-Git tests**

```bash
uv run --extra dev pytest \
  tests/test_workspace_hygiene.py \
  tests/test_phases1_4_real_git_integration.py \
  tests/test_execution_routing.py -q
```

Expected: baseline cache, no-regression hygiene, spaced paths, stale fingerprints, unexpected mutation, exact-commit snapshot, and coordinator routing tests pass.

- [ ] **Step 7: Commit Task 6**

```bash
git add src/repoforge/application/execution/hygiene.py \
  src/repoforge/adapters/hygiene \
  src/repoforge/ports/hygiene.py \
  src/repoforge/application/workspace/hygiene_status.py \
  src/repoforge/application/workspace/format_changed.py \
  src/repoforge/bootstrap.py \
  tests/test_workspace_hygiene.py \
  tests/test_execution_routing.py
git commit -m "refactor(hygiene): execute formatters through coordinator"
```

---

### Task 7: Bind Plan Receipts and Iteration Cache to Delegated Execution Evidence

**Files:**
- Modify: `src/repoforge/domain/execution_receipt.py`
- Modify: `src/repoforge/domain/verification_dag.py`
- Modify: `src/repoforge/adapters/persistence/json_iteration_cache.py`
- Modify: `src/repoforge/adapters/persistence/json_state_repository.py`
- Modify: `src/repoforge/application/workspace/execute_plan.py`
- Modify: `src/repoforge/application/workspace/failure_intelligence.py`
- Modify: `tests/test_plan_execution.py`
- Modify: `tests/test_verification_dag_cache.py`
- Modify: `tests/test_failure_intelligence.py`

**Interfaces:**
- Consumes: execution evidence returned by profile and diagnostic runners.
- Produces: stage receipt v2 and iteration-cache v2 keyed by post-run environment and effective policy.

- [ ] **Step 1: Write failing receipt and cache-migration tests**

Add to `tests/test_verification_dag_cache.py`:

```python
def test_legacy_v1_cache_reports_environment_schema_change_when_compatible(
    tmp_path: Path,
) -> None:
    store = JsonIterationCache(tmp_path, FcntlLockManager(tmp_path / "locks"))
    write_legacy_cache_record(
        store.root,
        key=legacy_key_matching_current_dimensions(),
    )

    lookup = store.lookup(current_v2_key(), workspace_root=tmp_path)

    assert lookup.hit is False
    assert lookup.reason is CacheMissReason.ENVIRONMENT_IDENTITY_SCHEMA_CHANGED


def test_unrelated_legacy_v1_cache_is_plain_not_found(tmp_path: Path) -> None:
    store = JsonIterationCache(tmp_path, FcntlLockManager(tmp_path / "locks"))
    write_legacy_cache_record(
        store.root,
        key=legacy_key_matching_current_dimensions(stage_definition_hash="9" * 64),
    )

    lookup = store.lookup(current_v2_key(), workspace_root=tmp_path)

    assert lookup.hit is False
    assert lookup.reason is CacheMissReason.NOT_FOUND
```

Implement the test-only `write_legacy_cache_record`, `legacy_key_matching_current_dimensions`, and `current_v2_key` helpers in the same file using the exact v1 envelope currently written by `JsonStateRepository`.

Add stage receipt tests asserting schema `2` and required `requested_policy_hash` and `effective_policy_hash` fields.

- [ ] **Step 2: Run plan/cache tests and verify RED**

```bash
uv run --extra dev pytest \
  tests/test_plan_execution.py \
  tests/test_verification_dag_cache.py \
  tests/test_failure_intelligence.py -q
```

Expected: missing receipt fields, schema version, and miss reason failures.

- [ ] **Step 3: Advance stage receipt and cache schemas**

Set:

```python
EXECUTION_RECEIPT_SCHEMA_VERSION = 2
ITERATION_CACHE_SCHEMA_VERSION = 2
```

Add `requested_policy_hash` and `effective_policy_hash` to `StageReceipt` and its semantic payload. Add those fields plus `environment_identity_schema_version` to `IterationCacheKey`. Include all three in `cache_key`.

Add this miss reason:

```python
ENVIRONMENT_IDENTITY_SCHEMA_CHANGED = "environment_identity_schema_changed"
```

Define a `compatibility_payload()` on cache keys containing every dimension except environment identity hash, requested/effective policy hashes, and identity schema version. Use it only to classify a legacy v1 miss; never use it to grant a hit.

- [ ] **Step 4: Add bounded raw-envelope inspection for legacy records**

Add `JsonStateRepository.read_raw_envelope(record_id: str) -> dict[str, object] | None`. It must:

- use the existing bounded byte reader;
- parse UTF-8 JSON;
- require exact envelope fields `payload`, `record_id`, `revision`, and `schema_version`;
- validate the requested record id;
- return a shallow dict without invoking the current codec.

In `JsonIterationCache`, when normal read raises `STATE_SCHEMA_UNSUPPORTED`, inspect only schema-v1 cache-key fields needed for compatibility comparison. Reject extra/malformed data as corrupt and never rewrite the old record.

- [ ] **Step 5: Remove plan executor synthetic identity**

Delete `_environment_identity()` and its `platform/sys` digest. Make `_execute_stage()` return the delegated runner result. Extract these exact fields from `result["execution_evidence"]`:

```text
identity_schema_version
environment_identity_hash
requested_policy_hash
effective_policy_hash
```

Fail closed with `CHECK_EVIDENCE_UNAVAILABLE` when a newly executed stage lacks required evidence. Cache hits use only v2 entries and create a fresh stage receipt carrying the current key's environment/policy values. Update failure intelligence to receive delegated evidence instead of a pre-run synthetic digest.

- [ ] **Step 6: Run plan/cache/failure tests**

```bash
uv run --extra dev pytest \
  tests/test_plan_execution.py \
  tests/test_verification_dag_cache.py \
  tests/test_failure_intelligence.py \
  tests/test_execution_plans.py -q
```

Expected: read-only iteration hits remain available under exact advisory bindings, final stages remain non-cacheable, legacy v1 reason is stable, and failure receipts contain current execution evidence.

- [ ] **Step 7: Commit Task 7**

```bash
git add src/repoforge/domain/execution_receipt.py \
  src/repoforge/domain/verification_dag.py \
  src/repoforge/adapters/persistence/json_iteration_cache.py \
  src/repoforge/adapters/persistence/json_state_repository.py \
  src/repoforge/application/workspace/execute_plan.py \
  src/repoforge/application/workspace/failure_intelligence.py \
  tests/test_plan_execution.py \
  tests/test_verification_dag_cache.py \
  tests/test_failure_intelligence.py
git commit -m "feat(execution): bind plan cache to environment evidence"
```

---

### Task 8: Enforce Verification Receipt v2 at Commit Time

**Files:**
- Modify: `src/repoforge/domain/workspace.py`
- Modify: `src/repoforge/adapters/persistence/json_workspace_store.py`
- Modify: `src/repoforge/application/workspace/commit.py`
- Modify: `src/repoforge/application/execution/requests.py`
- Modify: `tests/test_repository_commit_evidence.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_execution_routing.py`

**Interfaces:**
- Consumes: profile request compiler, `ExecutionCoordinator.inspect()`, and receipt fields written by Task 4.
- Produces: exact current-environment commit eligibility.

- [ ] **Step 1: Write failing legacy and drift tests**

Add to `tests/test_repository_commit_evidence.py`:

```python
def test_commit_rejects_legacy_verification_receipt(
    forge_env: ForgeEnvironment,
) -> None:
    created = forge_env.service.workspace_create("demo", "legacy receipt")
    workspace_id = created["workspace_id"]
    Path(created["path"], "hello.txt").write_text("changed\n", encoding="utf-8")
    status = forge_env.service.workspace_status(workspace_id)
    record = forge_env.service.state.load(workspace_id)
    record.last_verification = VerificationReceipt(
        profile="full",
        fingerprint=status["workspace_fingerprint"],
        completed_at="2026-07-19T00:00:00+00:00",
        commands=[],
    )
    forge_env.service.state.save(record)

    with pytest.raises(WorkspaceError, match="predates execution identity"):
        forge_env.service.workspace_commit(workspace_id, "legacy must not commit")
```

Add tests using `RecordingExecutionEnvironment` that change current effective policy or adapter version after verification. Assert commit calls `inspect` but does not call `execute`. Add a restart test that changes profile description/config hash and requires re-verification.

- [ ] **Step 2: Run commit tests and verify RED**

```bash
uv run --extra dev pytest \
  tests/test_repository_commit_evidence.py \
  tests/test_integration.py \
  tests/test_execution_routing.py -q
```

Expected: legacy/environment drift cases commit or fail for an unrelated reason.

- [ ] **Step 3: Add receipt-completeness helper**

In `src/repoforge/domain/workspace.py`:

```python
VERIFICATION_EXECUTION_IDENTITY_SCHEMA_VERSION = 2


def verification_receipt_has_current_execution_binding(
    receipt: VerificationReceipt,
) -> bool:
    return bool(
        receipt.execution_identity_schema_version
        == VERIFICATION_EXECUTION_IDENTITY_SCHEMA_VERSION
        and receipt.environment_identity_hash
        and receipt.requested_policy_hash
        and receipt.effective_policy_hash
        and receipt.adapter_kind
        and receipt.profile_target_hash
        and receipt.config_identity_hash
    )
```

Historical JSON remains readable because all new fields have defaults. Do not mutate old records in place.

- [ ] **Step 4: Inspect the current final profile before commit**

After the existing fingerprint check and before Git commit:

1. Reject a missing or legacy receipt with actionable guidance to rerun full verification.
2. Re-read the receipt's profile from current repository config.
3. Compile current steps and cwd with the shared request compiler.
4. Call `self.ctx.execution.inspect(request)`.
5. Compare current workspace fingerprint, environment identity hash, requested/effective policy hashes, adapter kind, profile target hash, and config identity hash.
6. On any mismatch, fail before Git commit with a bounded stale-verification error.

Commit inspection must not run repository command bodies or create external state.

- [ ] **Step 5: Run commit and integration tests**

```bash
uv run --extra dev pytest \
  tests/test_repository_commit_evidence.py \
  tests/test_integration.py \
  tests/test_workspace_adhoc.py \
  tests/test_background_run_profile.py \
  tests/test_execution_routing.py -q
```

Expected: one fresh verification repairs legacy state; source, environment, policy, adapter, profile, and config drift block commit; unchanged exact state commits normally.

- [ ] **Step 6: Commit Task 8**

```bash
git add src/repoforge/domain/workspace.py \
  src/repoforge/adapters/persistence/json_workspace_store.py \
  src/repoforge/application/workspace/commit.py \
  src/repoforge/application/execution/requests.py \
  tests/test_repository_commit_evidence.py \
  tests/test_integration.py \
  tests/test_execution_routing.py
git commit -m "feat(commit): require current execution identity evidence"
```

---

### Task 9: Project Bounded Execution Evidence Through Results, Audit, Contracts, and Docs

**Files:**
- Modify: `src/repoforge/domain/execution_environment.py`
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/application/workspace/run_diagnostic.py`
- Modify: `src/repoforge/application/workspace/run_adhoc.py`
- Modify: `src/repoforge/application/workspace/hygiene_status.py`
- Modify: `src/repoforge/application/workspace/format_changed.py`
- Modify: `src/repoforge/application/workspace/execute_plan.py`
- Modify: `src/repoforge/contracts/common.py`
- Modify: `src/repoforge/contracts/v2.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `docs/contracts/tool-schemas-v2.json`
- Modify: `docs/contracts/release-contract-v1.json`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/development/INTEGRITY_POLICY.md`
- Modify: `docs/testing/PLUGIN_TEST_CASES.md`
- Modify: `tests/test_v2_contract_models.py`
- Modify: `tests/test_v2_schema_golden.py`
- Modify: `tests/test_mcp_contract.py`
- Modify: `tests/test_phase5_mcp_contract.py`
- Modify: `tests/test_service_tools.py`

**Interfaces:**
- Consumes: internal execution inspection/receipt data from Tasks 1 through 8.
- Produces: stable bounded public and audit evidence with closed Pydantic schemas.

- [ ] **Step 1: Write failing public-evidence tests**

Add strict-model assertions to `tests/test_v2_contract_models.py`:

```python
def test_workspace_verify_output_exposes_closed_execution_evidence() -> None:
    _, registry = _contracts()
    schema = registry.V2_TOOL_SPECS["workspace_verify"].output_model.model_json_schema()
    rendered = json.dumps(schema, sort_keys=True)
    for field in (
        "adapter_kind",
        "identity_schema_version",
        "environment_identity_hash",
        "requested_policy_hash",
        "effective_policy_hash",
        "requested_network",
        "effective_network",
        "requested_filesystem",
        "effective_filesystem",
        "enforcement",
    ):
        assert field in rendered
```

Add runtime-model validation that unknown execution-evidence fields are rejected. Add MCP tests asserting `workspace_run_profile` and deprecated `workspace_verify` return identical evidence and no tool input schema gains backend controls.

- [ ] **Step 2: Run contract tests and verify RED**

```bash
uv run --extra dev pytest \
  tests/test_v2_contract_models.py \
  tests/test_v2_schema_golden.py \
  tests/test_mcp_contract.py \
  tests/test_phase5_mcp_contract.py \
  tests/test_service_tools.py -q
```

Expected: missing model fields and golden drift failures.

- [ ] **Step 3: Project the existing typed evidence consistently**

Use Task 1's `build_execution_evidence(request.requested_policy, inspection.identity, inspection.effective_policy, inspection.warnings)` in profile, diagnostic, ad-hoc, hygiene, formatter, and plan results. Remove any temporary dict construction introduced during migration tasks so every execution-capable result serializes the same frozen `ExecutionEvidence` dataclass.

Audit stores only adapter kind, policy hashes, degraded flag, enforcement levels, command count, target identifier, duration/exit code, and fingerprint-change facts. Do not store raw output, environment values, credentials, source bodies, patches, backend logs, or process trees.

- [ ] **Step 4: Add closed Pydantic output models**

In `src/repoforge/contracts/common.py`, add `EnforcementEvidenceModel` and `ExecutionEvidenceModel` with closed fields and SHA/enum/length bounds. Add `execution_evidence: ExecutionEvidenceModel | None = None` to execution-capable v2 outputs, including `WorkspaceVerifyOutput` and `WorkspaceFormatChangedOutput`. Keep all input models unchanged.

Update direct MCP result schemas additively. Tool names, titles, descriptions, annotations, and inputs must remain unchanged.

- [ ] **Step 5: Regenerate intentional contract changes**

Run:

```bash
uv run --extra dev python scripts/generate_tool_schemas.py --write
uv run --extra dev python scripts/check_release_contracts.py --write
```

Review the diff. Expected:

- no tool-count change;
- no tool-name or annotation change;
- no input-schema change;
- additive bounded output fields only;
- runtime control protocol unchanged.

- [ ] **Step 6: Update operator and tool documentation**

Document requested policy versus effective backend behavior, native advisory execution, stale verification after PATH/tool/environment restart changes, one fresh full verification as recovery, absence of new shell/network/credential capability, and `execution_evidence` as source of truth over legacy labels.

- [ ] **Step 7: Run contract, MCP, and documentation tests**

```bash
uv run --extra dev pytest \
  tests/test_v2_contract_models.py \
  tests/test_v2_schema_golden.py \
  tests/test_mcp_contract.py \
  tests/test_phase5_mcp_contract.py \
  tests/test_service_tools.py \
  tests/test_docs_command_drift.py -q
uv run --extra dev python scripts/check_release_contracts.py
```

Expected: closed schemas validate, goldens match, tool counts stay unchanged, and release contracts pass.

- [ ] **Step 8: Commit Task 9**

Stage only the exact changed files listed by `git status --short`, then commit:

```bash
git commit -m "feat(execution): expose truthful enforcement evidence"
```

Before committing, confirm no unrelated file is staged with `git diff --cached --name-only`.

---

### Task 10: Prove No Execution Bypass and Run the Exact Final Production Gate

**Files:**
- Modify: `tests/test_execution_routing.py`
- Modify: `tests/test_root_module_architecture.py`
- Modify: `tests/test_phase8_program_completion.py`
- Modify: `BUILD_REPORT.md` only when the repository's production-gate workflow updates it intentionally

**Interfaces:**
- Consumes: all previous tasks.
- Produces: repository-wide negative proof, exact-tree integration evidence, and final verified commits.

- [ ] **Step 1: Add repository-wide direct-execution classification**

In `tests/test_execution_routing.py`, parse Python AST and reject raw execution calls in workspace/execution/hygiene code. Use this reviewed infrastructure allowlist:

```python
ALLOWED_DIRECT_COMMAND_FILES = {
    "src/repoforge/adapters/execution/native.py",
    "src/repoforge/adapters/git/cli.py",
    "src/repoforge/adapters/github/gh_cli.py",
    "src/repoforge/adapters/github/capability_probe.py",
    "src/repoforge/adapters/github/ticket_graph.py",
    "src/repoforge/adapters/github/ticket_project.py",
    "src/repoforge/adapters/runtime/launcher.py",
    "src/repoforge/adapters/runtime/tunnel_cli.py",
    "src/repoforge/application/repository/doctor.py",
    "src/repoforge/adapters/onboarding_environment.py",
}
```

The test must inspect at least:

```text
src/repoforge/application/workspace/*.py
src/repoforge/application/execution/*.py
src/repoforge/adapters/hygiene/*.py
```

Reject `.commands.run`, `._executor.run`, `subprocess.run`, `subprocess.Popen`, and shell helpers outside the allowlist. Review each allowlisted file against the spec; do not expand the list merely to pass.

- [ ] **Step 2: Run routing and architecture tests**

```bash
uv run --extra dev pytest \
  tests/test_execution_routing.py \
  tests/test_root_module_architecture.py \
  tests/test_phase8_program_completion.py -q
```

Expected: no workspace use case or hygiene adapter can bypass the coordinator.

- [ ] **Step 3: Run all focused execution suites together**

```bash
uv run --extra dev pytest \
  tests/test_execution_identity.py \
  tests/test_execution_coordinator.py \
  tests/test_native_execution_environment.py \
  tests/test_background_run_profile.py \
  tests/test_workspace_diagnostics.py \
  tests/test_workspace_adhoc.py \
  tests/test_workspace_hygiene.py \
  tests/test_plan_execution.py \
  tests/test_verification_dag_cache.py \
  tests/test_failure_intelligence.py \
  tests/test_repository_commit_evidence.py \
  tests/test_execution_routing.py -q
```

Expected: all pass with no xfail remaining for the new behavior.

- [ ] **Step 4: Run formatting, lint, strict typing, and release contracts**

```bash
uv run --extra dev ruff format --check .
uv run --extra dev ruff check .
uv run --extra dev mypy src/repoforge
uv run --extra dev python scripts/check_release_contracts.py
```

Expected: all commands exit `0`.

- [ ] **Step 5: Run the authoritative production gate on the exact tree**

```bash
scripts/verify-production.sh --allow-dirty
```

Read the complete output and confirm all pytest shards, branch coverage, source/wheel builds, release contracts, and installed-wheel lifecycle complete successfully.

- [ ] **Step 6: Review exact diff and security invariants**

```bash
git status --short
git diff --stat
git diff --check
git grep -n "ctx\.commands\.run\|self\.ctx\.commands\.run" -- \
  src/repoforge/application/workspace \
  src/repoforge/application/execution
git grep -n "CommandExecutor" -- src/repoforge/adapters/hygiene
```

Expected:

- only intended files are changed;
- `git diff --check` is clean;
- both grep commands return no matches;
- no public tool input exposes backend flags, mounts, credentials, network overrides, or shell strings.

- [ ] **Step 7: Commit final integration-only adjustments**

Stage only files changed by Task 10. Commit:

```bash
git commit -m "test(execution): prove unified boundary end to end"
```

- [ ] **Step 8: Push and watch exact-SHA CI**

```bash
git push origin HEAD
```

Use RepoForge PR/check tools to verify every check belongs to the pushed HEAD SHA. Do not report completion while checks are pending or stale. Fix failures in a focused commit and rerun the exact local gate before pushing again.

---

## Final Acceptance Checklist

- [ ] `ApplicationContext` has a required `ExecutionCoordinator`; raw backend is private to bootstrap/coordinator wiring.
- [ ] Profile, diagnostic, ad-hoc, formatter, hygiene, and plan-stage commands cannot reach `CommandExecutor` directly.
- [ ] Multi-step profiles prepare once, execute admitted argv only, post-inspect the same session, and clean up once.
- [ ] Native effective policy is advisory `host_inherited` and `host_account_access`.
- [ ] Requested `offline` or `source_read` labels are never represented as enforced native facts.
- [ ] Unsupported CPU, memory, disk, PID, and network-byte limits are never represented as enforced.
- [ ] Profile failure mode raises; diagnostic/ad-hoc/hygiene failure mode returns a parseable result.
- [ ] Final verification receipt binds post-run environment, requested/effective policy, adapter, profile target, config identity, and exact workspace fingerprint.
- [ ] Commit reconstructs the current profile request and inspects environment without executing repository commands.
- [ ] Legacy verification receipts require one fresh full verification.
- [ ] Iteration cache v2 does not reuse v1 entries and reports `environment_identity_schema_changed` only for otherwise-compatible v1 keys.
- [ ] Advisory native read-only reuse remains available under exact bindings and is labeled advisory, not hermetic.
- [ ] Hygiene exact-commit inspection uses semantic Git snapshot reads and no raw executor.
- [ ] Public output schemas add bounded execution evidence without changing tool names, counts, annotations, or inputs.
- [ ] Audit and durable state contain no raw environment values, credentials, source bodies, patches, or unbounded backend logs.
- [ ] Focused suites, Ruff, strict Mypy, release contracts, all production pytest shards, builds, wheel smoke, and exact-SHA CI pass.
