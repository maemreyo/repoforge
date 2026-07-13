# Guided Local Repository Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, resumable `rf onboard ROOT...` workflow that discovers eligible local Git repositories, guides repository-specific decisions and approvals, accepts the selected batch as one immutable configuration generation, and activates it at most once.

**Architecture:** Keep the existing dependency direction: CLI interface → onboarding application services → pure domain models and ports ← filesystem/Git/persistence adapters. Reuse the existing proposal engine, configuration store, candidate smoke test, generation activator, stable error envelopes, and runtime control. The new CLI module owns presentation only; policy, state transitions, discovery classification, batch planning, mutation, and resume validation remain outside `interfaces/cli`.

**Tech Stack:** Python 3.10+, standard library (`argparse`, `dataclasses`, `enum`, `fnmatch`, `hashlib`, `json`, `os`, `pathlib`, `subprocess`, `sys`), existing RepoForge ports/adapters, `pytest`, `pytest-timeout`, Ruff, strict Mypy, Hatch/uv.

## Global Constraints

- Baseline: `main@85b809a90aba43f1dd6630438aed792ef08736c2`.
- Approved spec: `docs/superpowers/specs/2026-07-14-guided-onboarding-design.md`.
- Preserve Python `>=3.10`; do not add a runtime dependency.
- Preserve the existing dependency direction and import-matrix tests.
- Do not add onboarding logic to `src/repoforge/interfaces/cli/main.py`; it may only declare arguments, compose dependencies, dispatch, and render the final envelope.
- Keep existing `setup`, `repo inspect`, `repo propose`, `repo enroll`, `repo add`, and `repo refresh` behavior backward-compatible.
- Never persist API keys, GitHub tokens, environment snapshots, repository contents, patches, diffs, PR bodies, stdout, stderr, raw verification output, or raw approval tokens.
- Session directory mode must be `0700`; session file mode must be `0600`.
- Discovery must not follow symlinks outside supplied roots.
- Linked worktrees and RepoForge-managed workspaces are excluded by default.
- No executable profile is enabled without an exact proposal approval.
- Non-interactive mode never fabricates decisions or approvals.
- One completed selected batch creates one accepted generation and at most one activation transaction.
- Candidate smoke failure leaves source config, accepted generation, active generation, and runtime unchanged.
- Full release verification remains: frozen contracts, Ruff, strict Mypy, full branch coverage `>=80%`, build, and installed-wheel E2E on Linux and macOS.

---

## File Structure

### New production files

- `src/repoforge/domain/onboarding.py` — immutable discovery/session/batch-plan models, stable onboarding enums, duplicate-ID detection, transition validation, summaries.
- `src/repoforge/ports/repository_discovery.py` — repository traversal and Git identity protocol.
- `src/repoforge/ports/onboarding_store.py` — optimistic private session persistence protocol.
- `src/repoforge/ports/operator_io.py` — presentation-neutral interaction protocol.
- `src/repoforge/application/onboarding/__init__.py` — public onboarding application exports.
- `src/repoforge/application/onboarding/discover.py` — config-aware candidate classification.
- `src/repoforge/application/onboarding/preflight.py` — environment/tool/executable-shadow diagnostics.
- `src/repoforge/application/onboarding/inputs.py` — shared parsing and repository-scoped decision/override selection.
- `src/repoforge/application/onboarding/planner.py` — proposal generation, decisions, approvals, deterministic complete batch plan.
- `src/repoforge/application/onboarding/candidate.py` — candidate smoke testing against isolated state/workspace roots.
- `src/repoforge/application/onboarding/activation.py` — onboarding-facing wrapper over existing generation activation semantics.
- `src/repoforge/application/onboarding/coordinator.py` — create/resume/pause/cancel/status/apply/activate orchestration.
- `src/repoforge/adapters/repository/discovery.py` — bounded traversal and Git common-dir/worktree identity implementation.
- `src/repoforge/adapters/persistence/json_onboarding_store.py` — private atomic JSON session store.
- `src/repoforge/adapters/onboarding_environment.py` — executable/tool/environment preflight adapter.
- `src/repoforge/interfaces/cli/onboarding.py` — JSON and interactive terminal UX.

### Modified production files

- `src/repoforge/domain/errors.py` — stable onboarding error codes and envelopes.
- `src/repoforge/ports/__init__.py` — export new port protocols.
- `src/repoforge/bootstrap.py` — build discovery, session store, environment probe, and coordinator.
- `src/repoforge/interfaces/cli/main.py` — parser wiring and dispatch only.
- `docs/contracts/release-contract-v1.json` — freeze the new CLI surface and exit-code contract.
- `scripts/check_release_contracts.py` — validate onboarding command metadata.
- `scripts/verify-wheel-e2e.py` — installed-wheel multi-repository onboarding lifecycle.
- `README.md`, `docs/CHATGPT_SETUP.md`, `docs/TOOL_REFERENCE.md`, `docs/FULL_FLOW_TESTING.md` — replace manual `find`/`jq` flow.

### New tests

- `tests/test_onboarding_domain.py`
- `tests/test_onboarding_session_store.py`
- `tests/test_repository_discovery.py`
- `tests/test_onboarding_discovery_service.py`
- `tests/test_onboarding_preflight.py`
- `tests/test_onboarding_planner.py`
- `tests/test_onboarding_coordinator.py`
- `tests/test_onboarding_cli.py`
- `tests/test_onboarding_resume.py`
- `tests/test_onboarding_real_git.py`

### Modified tests

- `tests/test_phase5_architecture.py` — import-boundary coverage for new packages.
- `tests/test_cli_surface_coverage.py` — parser and command coverage.
- `tests/test_phase8_program_completion.py` — frozen contract and release artifact checks.
- `tests/test_bootstrap_factories.py` — new composition-root factories.

---

### Task 1: Add Stable Onboarding Domain Models and Error Codes

**Files:**
- Create: `src/repoforge/domain/onboarding.py`
- Modify: `src/repoforge/domain/errors.py`
- Modify: `src/repoforge/domain/__init__.py`
- Test: `tests/test_onboarding_domain.py`

**Interfaces:**
- Produces:
  - `OnboardingStatus`
  - `RepositoryProgress`
  - `ExclusionReason`
  - `DiscoveryIdentity`
  - `DiscoveryCandidate`
  - `DiscoveryExclusion`
  - `OnboardingOptions`
  - `OnboardingRepositoryState`
  - `OnboardingSession`
  - `OnboardingBatchPlan`
  - `OnboardingSummary`
  - `detect_duplicate_repo_ids(candidates: tuple[DiscoveryCandidate, ...]) -> dict[str, tuple[str, ...]]`
  - `transition_session(session: OnboardingSession, target: OnboardingStatus, *, now: str) -> OnboardingSession`
  - `summarize_session(session: OnboardingSession) -> OnboardingSummary`
- Adds stable error codes:
  - `DISCOVERY_ROOT_NOT_FOUND`
  - `DISCOVERY_PERMISSION_DENIED`
  - `DUPLICATE_REPOSITORY_ID`
  - `INTERACTION_REQUIRED`
  - `SESSION_NOT_FOUND`
  - `SESSION_CORRUPT`
  - `SESSION_STALE`
  - `CONFIG_CHANGED`
  - `REPOSITORY_FACTS_CHANGED`
  - `PROPOSAL_BLOCKED`
  - `DECISION_REQUIRED`
  - `APPROVAL_REQUIRED`
  - `APPROVAL_MISMATCH`
  - `CANDIDATE_SMOKE_FAILED`
  - `ACTIVATION_FAILED`
  - `EXECUTABLE_SHADOWED`

- [ ] **Step 1: Write failing domain tests**

```python
from dataclasses import replace

import pytest

from repoforge.domain.onboarding import (
    DiscoveryCandidate,
    DiscoveryIdentity,
    OnboardingOptions,
    OnboardingRepositoryState,
    OnboardingSession,
    OnboardingStatus,
    RepositoryProgress,
    detect_duplicate_repo_ids,
    summarize_session,
    transition_session,
)


def candidate(repo_id: str, path: str, common_dir: str | None = None) -> DiscoveryCandidate:
    identity = DiscoveryIdentity(
        path=path,
        worktree_root=path,
        git_common_dir=common_dir or f"{path}/.git",
        primary=True,
        bare=False,
    )
    return DiscoveryCandidate(identity=identity, repo_id=repo_id, parent_repo_id=None)


def test_duplicate_repository_ids_report_all_paths() -> None:
    duplicates = detect_duplicate_repo_ids(
        (candidate("api", "/repos/client/api"), candidate("api", "/repos/legacy/api"))
    )
    assert duplicates == {"api": ("/repos/client/api", "/repos/legacy/api")}


def test_session_transition_rejects_skipping_required_states() -> None:
    session = OnboardingSession.new(
        session_id="a" * 24,
        created_at="2026-07-14T00:00:00+00:00",
        config_path="/tmp/config.toml",
        roots=("/repos",),
        options=OnboardingOptions(),
    )
    with pytest.raises(ValueError, match="created -> ready"):
        transition_session(session, OnboardingStatus.READY, now=session.created_at)


def test_summary_counts_repository_progress() -> None:
    session = OnboardingSession.new(
        session_id="b" * 24,
        created_at="2026-07-14T00:00:00+00:00",
        config_path="/tmp/config.toml",
        roots=("/repos",),
        options=OnboardingOptions(),
    )
    session = replace(
        session,
        repositories=(
            OnboardingRepositoryState(
                candidate=candidate("one", "/repos/one"),
                progress=RepositoryProgress.APPROVED,
            ),
            OnboardingRepositoryState(
                candidate=candidate("two", "/repos/two"),
                progress=RepositoryProgress.SKIPPED,
            ),
        ),
    )
    assert summarize_session(session).approved == 1
    assert summarize_session(session).skipped == 1
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/test_onboarding_domain.py -v
```

Expected: collection fails because `repoforge.domain.onboarding` does not exist.

- [ ] **Step 3: Implement immutable models and transition table**

Create enums with string values:

```python
class OnboardingStatus(str, Enum):
    CREATED = "created"
    DISCOVERED = "discovered"
    AWAITING_DECISIONS = "awaiting_decisions"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    APPLYING = "applying"
    ACTIVATING = "activating"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED_RECOVERABLE = "failed_recoverable"
    CANCELLED = "cancelled"
    INVALID = "invalid"
```

Use frozen, slotted dataclasses. Store mappings as sorted tuples of `(str, str)` so JSON serialization and hashes remain deterministic. Define the only forward transitions:

```python
_ALLOWED_TRANSITIONS = {
    OnboardingStatus.CREATED: {OnboardingStatus.DISCOVERED, OnboardingStatus.PAUSED,
                               OnboardingStatus.CANCELLED},
    OnboardingStatus.DISCOVERED: {OnboardingStatus.AWAITING_DECISIONS,
                                  OnboardingStatus.AWAITING_APPROVAL,
                                  OnboardingStatus.READY,
                                  OnboardingStatus.PAUSED,
                                  OnboardingStatus.CANCELLED},
    OnboardingStatus.AWAITING_DECISIONS: {OnboardingStatus.AWAITING_APPROVAL,
                                         OnboardingStatus.READY,
                                         OnboardingStatus.PAUSED,
                                         OnboardingStatus.CANCELLED},
    OnboardingStatus.AWAITING_APPROVAL: {OnboardingStatus.READY,
                                        OnboardingStatus.PAUSED,
                                        OnboardingStatus.CANCELLED},
    OnboardingStatus.READY: {OnboardingStatus.APPLYING,
                             OnboardingStatus.PAUSED,
                             OnboardingStatus.CANCELLED},
    OnboardingStatus.APPLYING: {OnboardingStatus.ACTIVATING,
                                OnboardingStatus.COMPLETED,
                                OnboardingStatus.FAILED_RECOVERABLE},
    OnboardingStatus.ACTIVATING: {OnboardingStatus.COMPLETED,
                                  OnboardingStatus.FAILED_RECOVERABLE},
    OnboardingStatus.PAUSED: {OnboardingStatus.DISCOVERED,
                              OnboardingStatus.AWAITING_DECISIONS,
                              OnboardingStatus.AWAITING_APPROVAL,
                              OnboardingStatus.READY,
                              OnboardingStatus.CANCELLED},
    OnboardingStatus.FAILED_RECOVERABLE: {OnboardingStatus.DISCOVERED,
                                          OnboardingStatus.AWAITING_DECISIONS,
                                          OnboardingStatus.AWAITING_APPROVAL,
                                          OnboardingStatus.READY,
                                          OnboardingStatus.CANCELLED},
}
```

`OnboardingSession` must include `schema_version=1`, `revision`, timestamps, expected source/generation guards, repositories, exclusions, warning codes, accepted/active generations, and a redacted last-error dictionary.

- [ ] **Step 4: Add stable onboarding error codes**

Extend the existing `OperationErrorCode` enum and the exception-to-envelope mapping. Do not introduce a parallel error hierarchy. Each code must map to a concrete `safe_next_action`; for example:

```python
OperationErrorCode.DUPLICATE_REPOSITORY_ID: (
    "Two or more repositories resolved to the same model-facing id.",
    "Assign unique --repo-id values and resume the onboarding session.",
)
```

- [ ] **Step 5: Run domain tests**

```bash
uv run pytest tests/test_onboarding_domain.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Run architecture tests**

```bash
uv run pytest tests/test_phase5_architecture.py -v
```

Expected: pass; domain imports only standard library and other domain modules.

- [ ] **Step 7: Commit**

```bash
git add src/repoforge/domain/onboarding.py src/repoforge/domain/errors.py \
  src/repoforge/domain/__init__.py tests/test_onboarding_domain.py
git commit -m "feat(onboarding): add session domain model"
```

---

### Task 2: Define Discovery, Session Store, Environment, and Operator Ports

**Files:**
- Create: `src/repoforge/ports/repository_discovery.py`
- Create: `src/repoforge/ports/onboarding_store.py`
- Create: `src/repoforge/ports/operator_io.py`
- Create: `src/repoforge/ports/onboarding_environment.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `tests/test_phase5_architecture.py`
- Test: `tests/test_onboarding_ports.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    roots: tuple[Path, ...]
    max_depth: int
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    managed_workspace_roots: tuple[Path, ...]


class RepositoryDiscovery(Protocol):
    def discover(self, request: DiscoveryRequest) -> tuple[DiscoveryIdentity, ...]: ...


class OnboardingStore(Protocol):
    def create(self, session: OnboardingSession) -> OnboardingSession: ...
    def read(self, session_id: str) -> OnboardingSession | None: ...
    def save(self, session: OnboardingSession, *, expected_revision: int) -> OnboardingSession: ...
    def cancel(self, session_id: str, *, expected_revision: int, updated_at: str) -> OnboardingSession: ...


@dataclass(frozen=True, slots=True)
class EnvironmentPreflight:
    current_rf: str
    python: str
    virtual_env: str | None
    uv_tool_rf: str | None
    git_version: str | None
    gh_version: str | None
    gh_authenticated: bool
    tunnel_version: str | None
    config_exists: bool
    api_key_available: bool
    warnings: tuple[str, ...]


class OnboardingEnvironment(Protocol):
    def inspect(self, config_path: Path) -> EnvironmentPreflight: ...


class OperatorIO(Protocol):
    @property
    def interactive(self) -> bool: ...
    def show(self, event: dict[str, object]) -> None: ...
    def choose(self, *, prompt: str, choices: tuple[str, ...]) -> str: ...
    def ask(self, *, prompt: str, secret: bool = False) -> str: ...
    def confirm(self, *, prompt: str, default: bool = False) -> bool: ...
```

- [ ] **Step 1: Write protocol-shape tests**

```python
from pathlib import Path
from typing import get_type_hints

from repoforge.ports.onboarding_store import OnboardingStore
from repoforge.ports.repository_discovery import DiscoveryRequest, RepositoryDiscovery


def test_discovery_request_is_bounded_and_explicit() -> None:
    request = DiscoveryRequest(
        roots=(Path("/repos"),),
        max_depth=6,
        include=(),
        exclude=("**/.venv/**",),
        managed_workspace_roots=(Path("/state/workspaces"),),
    )
    assert request.max_depth == 6


def test_onboarding_store_exposes_optimistic_save() -> None:
    hints = get_type_hints(OnboardingStore.save)
    assert "expected_revision" in hints
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_ports.py -v
```

Expected: import failures for the new ports.

- [ ] **Step 3: Implement the protocols without adapter imports**

Use `typing.Protocol`, immutable dataclasses, `Path`, and the Task 1 domain types only.

- [ ] **Step 4: Export ports**

Add explicit exports in `src/repoforge/ports/__init__.py`; do not use wildcard imports.

- [ ] **Step 5: Verify ports and architecture**

```bash
uv run pytest tests/test_onboarding_ports.py tests/test_phase5_architecture.py -v
uv run mypy --strict src/repoforge/ports src/repoforge/domain
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/repoforge/ports tests/test_onboarding_ports.py tests/test_phase5_architecture.py
git commit -m "feat(onboarding): define orchestration ports"
```

---

### Task 3: Implement Private, Optimistic, Redacted Session Persistence

**Files:**
- Create: `src/repoforge/adapters/persistence/json_onboarding_store.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_onboarding_session_store.py`
- Test: `tests/test_bootstrap_factories.py`

**Interfaces:**
- Consumes: `OnboardingStore`, `OnboardingSession`, `OnboardingStatus`
- Produces:
  - `JsonOnboardingStore(root: Path, locks: LockManager)`
  - `build_onboarding_store(state_root: Path | None = None, locks: LockManager | None = None) -> OnboardingStore`

- [ ] **Step 1: Write failing persistence tests**

```python
import json
import stat

import pytest

from repoforge.adapters.persistence.json_onboarding_store import JsonOnboardingStore
from repoforge.domain.errors import ConfigError
from repoforge.domain.onboarding import OnboardingOptions, OnboardingSession


def make_session() -> OnboardingSession:
    return OnboardingSession.new(
        session_id="c" * 24,
        created_at="2026-07-14T00:00:00+00:00",
        config_path="/tmp/config.toml",
        roots=("/repos",),
        options=OnboardingOptions(),
    )


def test_store_uses_private_permissions_and_optimistic_revision(tmp_path, lock_manager) -> None:
    store = JsonOnboardingStore(tmp_path, lock_manager)
    created = store.create(make_session())
    path = tmp_path / "onboarding" / f"{created.session_id}.json"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = store.save(created, expected_revision=created.revision)
    assert saved.revision == created.revision + 1
    with pytest.raises(ConfigError, match="SESSION_STALE"):
        store.save(created, expected_revision=created.revision)


def test_store_rejects_secret_fields_and_corrupt_payload(tmp_path, lock_manager) -> None:
    store = JsonOnboardingStore(tmp_path, lock_manager)
    created = store.create(make_session())
    path = tmp_path / "onboarding" / f"{created.session_id}.json"
    payload = json.loads(path.read_text())
    assert "CONTROL_PLANE_API_KEY" not in json.dumps(payload)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigError, match="SESSION_CORRUPT"):
        store.read(created.session_id)
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_session_store.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement canonical serialization**

Write explicit `to_dict`/`from_dict` helpers for every domain dataclass. Reject:

- unknown `schema_version`;
- missing required keys;
- unknown enum values;
- non-string path values;
- duplicate repository entries;
- invalid revision;
- raw keys matching case-insensitive patterns `token`, `secret`, `credential`, `api_key`, `patch`, `diff`, `stdout`, `stderr`, `content`, `body`.

Store only approval-token SHA-256 hashes.

- [ ] **Step 4: Implement atomic private write**

Follow the repository's existing atomic write/fsync pattern:

1. acquire `locks.lock(f"onboarding-session:{session_id}")`;
2. create directory and chmod `0700`;
3. write a sibling temporary file with mode `0600`;
4. flush and `os.fsync`;
5. `os.replace`;
6. fsync parent directory;
7. return the persisted session with incremented revision.

Raise `SESSION_STALE` if the stored revision differs from `expected_revision`.

- [ ] **Step 5: Add bootstrap factory**

```python
def build_onboarding_store(
    state_root: Path | None = None,
    locks: LockManager | None = None,
) -> OnboardingStore:
    root = state_root or default_state_root()
    return JsonOnboardingStore(root, locks or build_lock_manager(root))
```

- [ ] **Step 6: Run focused tests**

```bash
uv run pytest tests/test_onboarding_session_store.py tests/test_bootstrap_factories.py -v
uv run mypy --strict src/repoforge/adapters/persistence/json_onboarding_store.py
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/repoforge/adapters/persistence/json_onboarding_store.py \
  src/repoforge/adapters/persistence/__init__.py src/repoforge/bootstrap.py \
  tests/test_onboarding_session_store.py tests/test_bootstrap_factories.py
git commit -m "feat(onboarding): persist resumable private sessions"
```

---

### Task 4: Implement Bounded Git-Aware Repository Discovery

**Files:**
- Create: `src/repoforge/adapters/repository/discovery.py`
- Modify: `src/repoforge/adapters/repository/__init__.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_repository_discovery.py`
- Test: `tests/test_onboarding_real_git.py`

**Interfaces:**
- Consumes: `DiscoveryRequest`, `RepositoryDiscovery`, `DiscoveryIdentity`
- Produces:
  - `LocalRepositoryDiscovery(command: CommandExecutor)`
  - `build_repository_discovery(state_root: Path | None = None) -> RepositoryDiscovery`

- [ ] **Step 1: Write real-Git RED tests**

Create helpers that initialize normal repositories, a bare repository, a nested distinct repository, and a linked worktree.

```python
def test_discovery_keeps_primary_and_excludes_linked_worktree(tmp_path, git) -> None:
    primary = init_repo(tmp_path / "project")
    linked = tmp_path / "project" / ".claude" / "worktrees" / "agent-1"
    git(primary, "worktree", "add", "-b", "agent-1", str(linked))

    result = discovery.discover(
        DiscoveryRequest(
            roots=(tmp_path,),
            max_depth=8,
            include=(),
            exclude=(),
            managed_workspace_roots=(),
        )
    )

    identities = {item.path: item for item in result}
    assert str(primary.resolve()) in identities
    assert identities[str(primary.resolve())].primary is True
    assert str(linked.resolve()) in identities
    assert identities[str(linked.resolve())].primary is False
```

Also add:

- symlink from root to an outside repository is not traversed;
- `.venv`, `node_modules`, `.cache`, `vendor` are pruned;
- bare repository is returned with `bare=True` so the application classifier can explain it;
- a real nested repository with a distinct common dir is returned;
- unreadable directories produce an identity-level error/exclusion input rather than aborting the whole scan;
- `max_depth` is enforced.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_repository_discovery.py tests/test_onboarding_real_git.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement bounded traversal**

Use `os.scandir` with an explicit queue of `(path, depth)`. Never call `Path.rglob`. For each directory:

- prune configured/default excluded names before descending;
- do not follow directory symlinks;
- check for `.git` file or directory;
- call bounded Git commands with argv arrays:

```text
git -C PATH rev-parse --show-toplevel
git -C PATH rev-parse --git-common-dir
git -C PATH rev-parse --is-bare-repository
git -C PATH rev-parse --git-dir
```

Resolve relative Git paths against the worktree root. Determine `primary` by comparing resolved `git_dir` with resolved `git_common_dir`; linked worktrees have a distinct git dir under `<common>/worktrees/...`.

Do not inspect repository contents beyond Git metadata.

- [ ] **Step 4: Implement stable ordering and de-duplication**

Sort identities by canonical path. Collapse repeated traversal hits with the tuple:

```python
(worktree_root, git_common_dir)
```

Keep distinct nested repositories because their common dirs differ.

- [ ] **Step 5: Add factory and run tests**

```bash
uv run pytest tests/test_repository_discovery.py tests/test_onboarding_real_git.py \
  tests/test_bootstrap_factories.py -v
uv run mypy --strict src/repoforge/adapters/repository/discovery.py
```

Expected: pass on Linux and macOS.

- [ ] **Step 6: Commit**

```bash
git add src/repoforge/adapters/repository/discovery.py \
  src/repoforge/adapters/repository/__init__.py src/repoforge/bootstrap.py \
  tests/test_repository_discovery.py tests/test_onboarding_real_git.py \
  tests/test_bootstrap_factories.py
git commit -m "feat(onboarding): discover local git repositories safely"
```

---

### Task 5: Add Config-Aware Discovery Classification

**Files:**
- Create: `src/repoforge/application/onboarding/__init__.py`
- Create: `src/repoforge/application/onboarding/discover.py`
- Test: `tests/test_onboarding_discovery_service.py`

**Interfaces:**
- Consumes:
  - `RepositoryDiscovery.discover`
  - existing `SourceConfiguration`
  - existing server workspace root
- Produces:

```python
@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    candidates: tuple[DiscoveryCandidate, ...]
    exclusions: tuple[DiscoveryExclusion, ...]
    duplicates: tuple[tuple[str, tuple[str, ...]], ...]


class OnboardingDiscoveryService:
    def discover(
        self,
        *,
        roots: tuple[Path, ...],
        max_depth: int,
        include: tuple[str, ...],
        exclude: tuple[str, ...],
        enrolled: tuple[SourceRepository, ...],
        managed_workspace_roots: tuple[Path, ...],
    ) -> DiscoveryResult: ...
```

- [ ] **Step 1: Write classification RED tests**

Use a fake `RepositoryDiscovery` returning identities for:

- enrolled primary repo;
- eligible primary repo;
- linked worktree;
- bare repo;
- RepoForge-managed workspace;
- two eligible repos that slug to the same ID;
- real nested repo.

Assert exact stable exclusion reasons.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_discovery_service.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement classification**

Rules, in order:

1. root validation; missing root raises `DISCOVERY_ROOT_NOT_FOUND`;
2. managed workspace containment → `repoforge_managed_workspace`;
3. `bare=True` → `bare_repository`;
4. `primary=False` → `linked_worktree`;
5. canonical path already enrolled → `already_enrolled`;
6. path matches operator `exclude` and not `include` → `generated_worktree_directory` for built-in generated-directory patterns, otherwise `operator_excluded`;
7. eligible primary candidate;
8. detect duplicate IDs after classification.

Derive repository ID using the existing safe slug function; do not duplicate slug behavior.

- [ ] **Step 4: Preserve parent/child relation**

For each eligible candidate, set `parent_repo_id` when its canonical path is contained by another eligible repository path with a distinct common dir.

- [ ] **Step 5: Run tests and typecheck**

```bash
uv run pytest tests/test_onboarding_discovery_service.py -v
uv run mypy --strict src/repoforge/application/onboarding/discover.py
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/repoforge/application/onboarding \
  tests/test_onboarding_discovery_service.py
git commit -m "feat(onboarding): classify discovered repositories"
```

---

### Task 6: Add Environment Preflight and Executable-Shadow Detection

**Files:**
- Create: `src/repoforge/application/onboarding/preflight.py`
- Create: `src/repoforge/adapters/onboarding_environment.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_onboarding_preflight.py`
- Test: `tests/test_bootstrap_factories.py`

**Interfaces:**
- Produces:
  - `SystemOnboardingEnvironment`
  - `OnboardingPreflightService`
  - `build_onboarding_environment() -> OnboardingEnvironment`

- [ ] **Step 1: Write RED tests**

```python
def test_preflight_warns_when_virtualenv_rf_shadows_uv_tool(tmp_path, monkeypatch) -> None:
    current = tmp_path / "repo" / ".venv" / "bin" / "rf"
    uv_rf = tmp_path / "home" / ".local" / "bin" / "rf"
    fake = FakeEnvironment(
        current_rf=current,
        python=tmp_path / "repo" / ".venv" / "bin" / "python",
        virtual_env=tmp_path / "repo" / ".venv",
        path_entries=(current.parent, uv_rf.parent),
        executables={"git": "/usr/bin/git", "gh": "/usr/bin/gh",
                     "tunnel-client": "/usr/local/bin/tunnel-client"},
        versions={"git": "2.50", "gh": "2.96", "tunnel-client": "1.0"},
        gh_authenticated=True,
        api_key_available=True,
    )
    result = OnboardingPreflightService(fake).inspect(Path("/tmp/config.toml"))
    assert result.uv_tool_rf == str(uv_rf)
    assert "EXECUTABLE_SHADOWED" in result.warnings
```

Also test missing `git`, unauthenticated `gh`, missing tunnel client, absent API key with `activate=never` versus `activate=always`, and that no environment values are returned.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_preflight.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement adapter with bounded commands**

The adapter may run only:

```text
git --version
gh --version
gh auth status
tunnel-client --version
uv tool dir --bin
```

Each command uses `subprocess.run` with argv, `capture_output=True`, `check=False`, and a 10-second timeout. Return booleans and version first lines only; never return token-bearing output.

- [ ] **Step 4: Implement shadowing logic**

Warn when:

- `VIRTUAL_ENV` is set;
- `current_rf` is under `VIRTUAL_ENV`;
- a different executable named `rf` exists in the uv tool bin directory.

Do not fail merely because of shadowing. Mark incompatible only when `rf --version` cannot report the running package's expected major version; use the package version constant rather than a hard-coded string.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_onboarding_preflight.py tests/test_bootstrap_factories.py -v
uv run mypy --strict src/repoforge/application/onboarding/preflight.py \
  src/repoforge/adapters/onboarding_environment.py
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/repoforge/application/onboarding/preflight.py \
  src/repoforge/adapters/onboarding_environment.py src/repoforge/bootstrap.py \
  tests/test_onboarding_preflight.py tests/test_bootstrap_factories.py
git commit -m "feat(onboarding): add operator environment preflight"
```

---

### Task 7: Build Deterministic Proposal and Approval Planning

**Files:**
- Create: `src/repoforge/application/onboarding/inputs.py`
- Create: `src/repoforge/application/onboarding/planner.py`
- Test: `tests/test_onboarding_planner.py`

**Interfaces:**
- Consumes:
  - `RepositoryProposalService.inspect`
  - `RepositoryProposalService.propose`
  - `RepositoryProposalService.verify_approval`
  - `SourceConfiguration`
  - `parse_resolved`, `apply_proposal`, `render_source`, `render_resolved`
- Produces:

```python
@dataclass(frozen=True, slots=True)
class PlanningInput:
    template: EnrollmentMode
    decisions: tuple[tuple[str, str], ...]
    overrides: tuple[tuple[str, str], ...]
    approvals: tuple[str, ...]


class OnboardingPlanner:
    def plan(
        self,
        session: OnboardingSession,
        *,
        current_source: SourceConfiguration | None,
        current_resolved_text: str | None,
        current_generation: ConfigGeneration | None,
        inputs: tuple[tuple[str, PlanningInput], ...],
        now: str,
    ) -> tuple[OnboardingSession, OnboardingBatchPlan | None]: ...
```

- [ ] **Step 1: Write RED tests**

Cases:

- proposal with required decisions moves session to `awaiting_decisions`;
- fully decided proposal without approval moves to `awaiting_approval`;
- blocked proposal records `PROPOSAL_BLOCKED` and cannot become ready;
- exact approval token is hashed and raw token does not appear in session serialization;
- two approved repositories produce one source text, one resolved candidate, one combined proposal digest, and one batch plan;
- existing enrolled repository is unchanged;
- duplicate repo IDs stop planning before proposals are accepted;
- changing template from `standard` to `read_only` regenerates proposal ID.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_planner.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement repository-scoped inputs**

Reuse the current CLI's semantics:

- global `CODE=CHOICE`;
- repository override `REPO_ID.CODE=CHOICE`;
- repository-scoped value wins.

Move `_parse_decisions`, `_decisions_for_repo`, `_parse_overrides`, and `_overrides_for_repo` from `interfaces/cli/main.py` into `application/onboarding/inputs.py`. Keep same-name compatibility wrappers in `main.py` that delegate to the new functions until all existing CLI tests pass.

- [ ] **Step 4: Implement exact approval verification**

For each proposal, require `approve:<proposal_id>`. Call the existing verification helper; persist only:

```python
hashlib.sha256(token.encode("utf-8")).hexdigest()
```

The batch plan may carry proposal IDs and approval hashes but never raw approval tokens.

- [ ] **Step 5: Build one candidate configuration**

Starting from existing or empty source/resolved documents:

1. add every approved repository to the source;
2. apply every approved proposal to the resolved document;
3. sort repositories by repo ID;
4. build one combined proposal ID from sorted proposal IDs;
5. render one source text;
6. render one resolved candidate for `current_generation + 1`;
7. calculate capability delta against current resolved text;
8. return one `OnboardingBatchPlan`.

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_onboarding_planner.py \
  tests/test_phase3_repository_proposals.py \
  tests/test_cli_command_bodies.py -v
uv run mypy --strict src/repoforge/application/onboarding/inputs.py \
  src/repoforge/application/onboarding/planner.py
```

Expected: pass and no change to existing proposal IDs for unchanged inputs.

- [ ] **Step 7: Commit**

```bash
git add src/repoforge/application/onboarding/inputs.py \
  src/repoforge/application/onboarding/planner.py \
  src/repoforge/interfaces/cli/main.py tests/test_onboarding_planner.py
git commit -m "feat(onboarding): plan approved repository batches"
```

---

### Task 8: Implement Coordinator, Candidate Smoke, Atomic Accept, and Single Activation

**Files:**
- Create: `src/repoforge/application/onboarding/candidate.py`
- Create: `src/repoforge/application/onboarding/activation.py`
- Create: `src/repoforge/application/onboarding/coordinator.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/interfaces/cli/main.py` to reuse extracted activation/smoke services, not duplicate them
- Test: `tests/test_onboarding_coordinator.py`
- Test: `tests/test_bootstrap_factories.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class OnboardingCommand:
    config_path: Path
    roots: tuple[Path, ...]
    options: OnboardingOptions
    decisions: tuple[tuple[str, str], ...]
    overrides: tuple[tuple[str, str], ...]
    approvals: tuple[str, ...]
    resume_session_id: str | None = None
    plan_only: bool = False


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    session: OnboardingSession
    plan: OnboardingBatchPlan | None
    summary: OnboardingSummary
    activation: dict[str, object] | None


class OnboardingCoordinator:
    def run(self, command: OnboardingCommand) -> OnboardingResult: ...
    def status(self, session_id: str) -> OnboardingResult: ...
    def cancel(self, session_id: str) -> OnboardingResult: ...
```

Constructor dependencies must be ports/use cases, not concrete adapters:

```python
def __init__(
    self,
    *,
    sessions: OnboardingStore,
    discovery: OnboardingDiscoveryService,
    preflight: OnboardingPreflightService,
    planner: OnboardingPlanner,
    configs: ConfigurationStore,
    proposal_service: RepositoryProposalService,
    clock: Clock,
    ids: IdGenerator,
    smoke_candidate: Callable[[str, tuple[str, ...]], tuple[dict[str, object], ...]],
    activate: Callable[[ConfigGeneration, str, bool, bool], dict[str, object]],
) -> None: ...
```

- [ ] **Step 1: Write RED coordinator tests**

Use fakes and assert:

- new config path creates session then plans;
- existing config skips enrolled paths;
- `plan_only=True` never calls smoke, accept, or activate;
- unresolved decisions returns session state without mutation;
- missing approvals returns session state without mutation;
- complete batch calls smoke for every repo before accept;
- second smoke failure means `accept` call count is zero;
- complete batch calls `ConfigurationStore.accept` exactly once;
- activation is called at most once;
- `activate=never` accepts but does not activate;
- source hash changed before accept raises `CONFIG_CHANGED`;
- accepted generation changes session from applying to activating/completed;
- activation failure with rollback enabled records recoverable failure and the exact activator result;
- no raw approval token is present in stored session.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_coordinator.py -v
```

Expected: import failure.

- [ ] **Step 3: Extract reusable candidate smoke helper**

Move `_smoke_resolved` out of `interfaces/cli/main.py` into an application or bootstrap-owned callable with signature:

```python
def smoke_candidate(
    resolved_text: str,
    repo_ids: tuple[str, ...],
    *,
    state_root: Path,
) -> tuple[dict[str, object], ...]:
```

It must use one temporary candidate file and isolated state/workspace roots, then smoke each repo. Existing `setup` and `repo enroll` tests must continue to pass through the extracted helper.

- [ ] **Step 4: Extract reusable activation façade**

Move `_activate` behavior behind:

```python
class ConfigurationActivator:
    def activate(
        self,
        generation: ConfigGeneration,
        *,
        mode: str,
        wait: bool,
        rollback_on_failure: bool,
    ) -> dict[str, object]: ...
```

Keep runtime semantics unchanged. Existing CLI commands call this façade.

- [ ] **Step 5: Implement coordinator transaction**

Ordered behavior:

1. inspect preflight;
2. create or load session;
3. discover and persist;
4. plan and persist;
5. return if decisions/approvals are required;
6. return plan without mutation when `plan_only`;
7. re-read source/generation guards;
8. smoke the complete candidate;
9. transition to `applying` and persist;
10. call `configs.accept` once with exact expected generation/source SHA;
11. transition to `activating` when activation is attempted;
12. activate once;
13. persist accepted/active generation and `completed`;
14. on recoverable failure, persist a redacted stable envelope and `failed_recoverable`.

The config accept call must carry one `ApprovalEvent` whose digest is computed from sorted per-repository approval hashes.

- [ ] **Step 6: Add composition factory**

```python
def build_onboarding_coordinator(config_path: Path) -> OnboardingCoordinator:
    ...
```

Use one lock manager and one state root for all persistence/runtime collaborators.

- [ ] **Step 7: Run focused and compatibility tests**

```bash
uv run pytest tests/test_onboarding_coordinator.py \
  tests/test_cli_command_bodies.py \
  tests/test_cli_runtime_commands.py \
  tests/test_phase7_atomic_hot_reload.py \
  tests/test_bootstrap_factories.py -v
uv run mypy --strict src/repoforge/application/onboarding/candidate.py \
  src/repoforge/application/onboarding/activation.py \
  src/repoforge/application/onboarding/coordinator.py
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add src/repoforge/application/onboarding/candidate.py \
  src/repoforge/application/onboarding/activation.py \
  src/repoforge/application/onboarding/coordinator.py src/repoforge/bootstrap.py \
  src/repoforge/interfaces/cli/main.py tests/test_onboarding_coordinator.py \
  tests/test_bootstrap_factories.py
git commit -m "feat(onboarding): accept and activate repository batches"
```

---

### Task 9: Add Read-Only `rf repo discover` and Non-Interactive `rf onboard`

**Files:**
- Create: `src/repoforge/interfaces/cli/onboarding.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Test: `tests/test_onboarding_cli.py`
- Modify: `tests/test_cli_surface_coverage.py`

**Interfaces:**
- Consumes: `OnboardingCoordinator`, `OnboardingCommand`, `OnboardingResult`, discovery service
- Produces:
  - `add_onboarding_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None`
  - `run_onboarding_command(args: argparse.Namespace) -> int | None`
  - exit code `0` for completed/plan/read-only success
  - exit code `3` for decisions/approval/interaction required
  - exit code `2` for stable operation failure
  - exit code `0` for a deliberately paused resumable session with `"status": "paused"`

- [ ] **Step 1: Write parser and non-interactive RED tests**

```python
def test_repo_discover_is_read_only(cli, tmp_path) -> None:
    result = cli("repo", "discover", str(tmp_path), "--max-depth", "5")
    assert result.exit_code == 0
    assert result.json["status"] == "discovered"
    assert result.calls["config_accept"] == 0
    assert result.calls["session_create"] == 0


def test_non_interactive_onboard_returns_three_when_approval_missing(cli, repo_root) -> None:
    result = cli("onboard", str(repo_root), "--non-interactive", "--activate", "never")
    assert result.exit_code == 3
    assert result.json["status"] == "awaiting_approval"
    assert result.json["required_approval_tokens"]
    assert result.calls["config_accept"] == 0
```

Also test all options from the spec, config option normalization before/after command, existing config branch, duplicate IDs, `--plan-only`, and JSON error envelopes.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_cli.py tests/test_cli_surface_coverage.py -v
```

Expected: parser rejects `onboard` and `repo discover`.

- [ ] **Step 3: Implement parser in focused module**

`main.py` calls:

```python
from .onboarding import add_onboarding_parsers, run_onboarding_command
...
add_onboarding_parsers(commands)
...
handled = run_onboarding_command(args)
if handled is not None:
    return handled
```

Do not import concrete adapters in `interfaces/cli/onboarding.py`; receive a coordinator factory from bootstrap or a small composition function.

- [ ] **Step 4: Implement structured output**

Required keys for incomplete onboarding:

```json
{
  "status": "awaiting_approval",
  "session_id": "...",
  "required_decisions": [],
  "required_approval_tokens": ["approve:..."],
  "eligible": [],
  "excluded": [],
  "warnings": [],
  "unchanged_state": ["configuration", "runtime"],
  "safe_next_action": "..."
}
```

For security, approval tokens may be emitted to the operator because they are challenge tokens, but they must not be persisted. Never include API-key availability beyond a boolean.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_onboarding_cli.py tests/test_cli_surface_coverage.py \
  tests/test_cli_command_bodies.py -v
uv run mypy --strict src/repoforge/interfaces/cli
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/repoforge/interfaces/cli/onboarding.py \
  src/repoforge/interfaces/cli/main.py \
  tests/test_onboarding_cli.py tests/test_cli_surface_coverage.py
git commit -m "feat(cli): add non-interactive guided onboarding"
```

---

### Task 10: Add Interactive Terminal Wizard Without Policy Logic

**Files:**
- Modify: `src/repoforge/interfaces/cli/onboarding.py`
- Create: `src/repoforge/adapters/operator/terminal.py`
- Create: `src/repoforge/adapters/operator/__init__.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_onboarding_cli.py`
- Create: `tests/test_terminal_operator_io.py`

**Interfaces:**
- Consumes: `OperatorIO`
- Produces:
  - `TerminalOperatorIO(stdin: TextIO, stdout: TextIO, stderr: TextIO)`
  - `ScriptedOperatorIO` only in tests, not production
  - guided actions `approve`, `strict`, `read_only`, `details`, `skip`, `pause`

- [ ] **Step 1: Write RED interaction tests**

Use `io.StringIO` and scripted choices. Test:

- discovery summary displayed before proposals;
- each proposal displays exact argv arrays before approval;
- `s` regenerates proposal with strict template;
- `r` regenerates with read-only template;
- `d` prints full details then prompts again;
- `k` skips only that repo;
- `q` persists paused session;
- duplicate ID prompts for a unique safe ID;
- EOF or non-TTY while input is required returns `INTERACTION_REQUIRED`;
- no secret environment values appear in stdout/stderr.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_terminal_operator_io.py \
  tests/test_onboarding_cli.py -k interactive -v
```

Expected: failures because terminal adapter does not exist.

- [ ] **Step 3: Implement terminal adapter**

Use plain standard-library I/O. Do not add Rich, Click, Prompt Toolkit, or curses.

`choose` must:

- print numbered and single-letter choices;
- normalize whitespace and lowercase;
- reject unknown input with a bounded retry loop;
- raise `INTERACTION_REQUIRED` after EOF.

`ask(secret=True)` uses `getpass.getpass` only when needed; this onboarding feature must not ask for the control-plane key because runtime startup already requires it in the environment.

- [ ] **Step 4: Implement wizard loop in interface**

The loop asks the coordinator/planner to regenerate session state after each decision. It does not edit proposal objects itself.

Before final apply, display a deterministic batch summary and ask:

```text
Apply this reviewed batch as one configuration generation? [y/N]
```

No “approve all unseen” command exists.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_terminal_operator_io.py tests/test_onboarding_cli.py -v
uv run mypy --strict src/repoforge/adapters/operator \
  src/repoforge/interfaces/cli/onboarding.py
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/repoforge/adapters/operator src/repoforge/bootstrap.py \
  src/repoforge/interfaces/cli/onboarding.py \
  tests/test_terminal_operator_io.py tests/test_onboarding_cli.py
git commit -m "feat(cli): add interactive onboarding wizard"
```

---

### Task 11: Implement Resume, Status, Cancel, and Staleness Revalidation

**Files:**
- Modify: `src/repoforge/application/onboarding/coordinator.py`
- Modify: `src/repoforge/interfaces/cli/onboarding.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Test: `tests/test_onboarding_resume.py`
- Test: `tests/test_onboarding_cli.py`

**Interfaces:**
- Produces command surface:
  - `rf onboard --resume SESSION_ID`
  - `rf onboard resume SESSION_ID`
  - `rf onboard status SESSION_ID`
  - `rf onboard cancel SESSION_ID`

- [ ] **Step 1: Write RED resume tests**

Cases:

- resume from `paused` returns to the exact prior actionable state;
- missing session → `SESSION_NOT_FOUND`;
- corrupt session → `SESSION_CORRUPT`;
- source SHA/generation changed → `CONFIG_CHANGED`, no mutation;
- repository canonical identity changed → `REPOSITORY_FACTS_CHANGED`;
- proposal facts fingerprint changed → clear approval hash, regenerate proposal, return to decision/approval state;
- already-enrolled repository after external completion becomes `unchanged`, not duplicated;
- cancel is idempotent and cannot erase an accepted generation;
- completed session status is read-only;
- raw approval token is not required to resume when the persisted hash equals `sha256(f"approve:{proposal_id}")`; a changed proposal ID clears the hash and returns `APPROVAL_REQUIRED`.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_onboarding_resume.py -v
```

Expected: failures for missing resume methods.

- [ ] **Step 3: Implement session action-state memory**

Add `resume_target: OnboardingStatus | None` to the domain session if not already included. When pausing or failing recoverably, persist the previous actionable state. Transition from `paused` or `failed_recoverable` only to that state after validation.

- [ ] **Step 4: Implement revalidation order**

1. load and schema-check session;
2. ensure not cancelled/invalid;
3. read current config source/generation;
4. compare expected guards;
5. rediscover selected canonical paths;
6. regenerate repository facts/proposals;
7. compare facts fingerprints and `sha256(f"approve:{proposal_id}")` against persisted approval hashes;
8. clear stale decisions/approval evidence only for changed repositories;
9. reconcile already-enrolled paths;
10. persist the revalidated session;
11. continue planning/apply.

- [ ] **Step 5: Implement status and cancel CLI**

`status` never probes repository contents; it reads session metadata and current config/runtime generation only.

`cancel` sets status to `cancelled`, increments revision, and reports any already accepted generation. It does not roll back configuration.

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_onboarding_resume.py tests/test_onboarding_cli.py \
  tests/test_onboarding_session_store.py -v
uv run mypy --strict src/repoforge/application/onboarding/coordinator.py \
  src/repoforge/interfaces/cli/onboarding.py
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/repoforge/domain/onboarding.py \
  src/repoforge/application/onboarding/coordinator.py \
  src/repoforge/interfaces/cli/onboarding.py src/repoforge/interfaces/cli/main.py \
  tests/test_onboarding_resume.py tests/test_onboarding_cli.py
git commit -m "feat(onboarding): resume and manage onboarding sessions"
```

---

### Task 12: Freeze Contracts, Document the Happy Path, and Add Installed-Wheel E2E

**Files:**
- Modify: `docs/contracts/release-contract-v1.json`
- Modify: `scripts/check_release_contracts.py`
- Modify: `scripts/verify-wheel-e2e.py`
- Modify: `.github/workflows/production-gate.yml`
- Modify: `README.md`
- Modify: `docs/CHATGPT_SETUP.md`
- Modify: `docs/TOOL_REFERENCE.md`
- Modify: `docs/FULL_FLOW_TESTING.md`
- Modify: `tests/test_phase8_program_completion.py`
- Modify: `tests/test_phase5_architecture.py`

**Interfaces:**
- Freezes:
  - `rf onboard`
  - `rf onboard resume`
  - `rf onboard status`
  - `rf onboard cancel`
  - `rf repo discover`
  - options, defaults, exit codes, stable error codes, session schema version

- [ ] **Step 1: Write release-contract RED test**

Extend the contract test to assert:

```python
assert contract["cli"]["commands"]["onboard"]["arguments"]["roots"]["nargs"] == "+"
assert contract["cli"]["commands"]["onboard"]["defaults"]["activate"] == "auto"
assert contract["cli"]["commands"]["onboard"]["exit_codes"]["input_required"] == 3
assert contract["onboarding_session_schema"] == 1
assert "DUPLICATE_REPOSITORY_ID" in contract["stable_error_codes"]
```

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_phase8_program_completion.py -v
```

Expected: frozen contract mismatch.

- [ ] **Step 3: Update contract generator/checker**

Generate parser metadata from `build_parser()` and compare:

- command/subcommand names;
- option flags;
- choices;
- nargs;
- defaults;
- BooleanOptionalAction pairs;
- exit-code contract.

Do not hand-maintain parser metadata in two Python files.

- [ ] **Step 4: Add installed-wheel E2E**

From a fresh wheel-only venv:

1. create root with two normal Git repos;
2. create `.claude/worktrees/agent-*` linked worktree under one repo;
3. create a distinct nested repo;
4. create minimal fake `gh` and `tunnel-client` executables;
5. run `rf repo discover ROOT --output json`;
6. assert the primary repos and nested repo are eligible and linked worktree is excluded;
7. run `rf onboard ROOT --non-interactive --activate never`;
8. parse exact decisions/approval tokens from structured output in Python, not `jq`;
9. rerun with those inputs;
10. assert one accepted generation contains all selected repos;
11. rerun and assert already-enrolled paths are skipped;
12. assert session files are `0600` and contain none of the seeded secrets;
13. run `rf runtime status` and verify stopped/restart-required semantics;
14. create and resume a paused session.

The E2E test must import RepoForge only from the installed wheel, never from the source tree.

- [ ] **Step 5: Update CI**

Ensure Linux and macOS jobs run:

```bash
uv run python scripts/check_release_contracts.py
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy --strict src/repoforge
uv run pytest --cov=repoforge --cov-branch --cov-report=term-missing
uv build
bash scripts/verify-wheel-install.sh
```

Keep per-test timeout `60` seconds.

- [ ] **Step 6: Update operator documentation**

Replace manual `find`, `jq`, and shell loops with:

```bash
uv tool install --force 'git+https://github.com/maemreyo/repoforge.git@main'
rf onboard /absolute/root/containing/repos
rf runtime start
```

Document:

- why linked worktrees are excluded;
- existing-config auto-detection;
- decisions and exact approval;
- `--plan-only`;
- non-interactive operation;
- session resume/status/cancel;
- executable-shadow warning;
- API key still belongs in the runtime environment, not onboarding state.

- [ ] **Step 7: Run the full production gate**

```bash
uv sync --extra dev --frozen
uv run python scripts/check_release_contracts.py
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy --strict src/repoforge
uv run pytest --cov=repoforge --cov-branch --cov-report=term-missing
uv build
bash scripts/verify-wheel-install.sh
```

Expected:

- all tests pass;
- branch coverage is at least 80%;
- wheel and sdist build;
- installed-wheel onboarding E2E passes;
- repository remains clean after the gate.

- [ ] **Step 8: Run focused macOS regression commands**

On macOS:

```bash
uv run pytest tests/test_repository_discovery.py \
  tests/test_onboarding_real_git.py \
  tests/test_phase6_operational_hardening.py \
  tests/test_phase7_regressions.py -v
```

Expected: pass, including linked-worktree discovery and tunnel-child lifecycle regressions.

- [ ] **Step 9: Commit**

```bash
git add docs README.md scripts .github/workflows/production-gate.yml \
  tests/test_phase8_program_completion.py tests/test_phase5_architecture.py
git commit -m "docs(onboarding): publish guided onboarding workflow"
```

---

## Final Verification Checklist

- [ ] `git status --short` shows only intended changes before the final commit and is empty after commit.
- [ ] `git diff --check` passes.
- [ ] `uv run python scripts/check_release_contracts.py` passes.
- [ ] Ruff format and lint pass.
- [ ] Strict Mypy passes for all `src/repoforge`.
- [ ] Full pytest suite passes with branch coverage `>=80%`.
- [ ] Existing setup/proposal/enroll/refresh tests remain green.
- [ ] Existing runtime hot-reload/restart/rollback tests remain green.
- [ ] Real Git discovery excludes linked worktrees and retains real nested repositories.
- [ ] Private session permissions are `0700`/`0600`.
- [ ] Session artifacts contain no seeded token, secret, body, patch, diff, stdout, or stderr.
- [ ] Missing decisions/approvals exit with code `3` and no config mutation.
- [ ] Candidate smoke failure leaves config and runtime unchanged.
- [ ] Complete batch accepts exactly one generation and performs at most one activation.
- [ ] Resume detects config and repository-facts drift.
- [ ] Installed-wheel E2E passes without source imports.
- [ ] Linux and macOS production-gate jobs pass.

## Recommended Execution Order and Review Gates

1. Tasks 1–3: domain contracts and session persistence.
2. Tasks 4–6: discovery and preflight.
3. Tasks 7–8: planning and transactional coordination.
4. Tasks 9–11: non-interactive, interactive, and resume CLI.
5. Task 12: contract freeze, docs, wheel E2E, full release gate.

After each task:

```bash
git show --stat --oneline HEAD
git diff HEAD^ --check
```

Request review against the task's interfaces and invariants before starting the next task. Do not combine Task 8 with CLI work; transaction semantics must be reviewable independently from presentation.
