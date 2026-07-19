# Repo Issue Write Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing `repo_issue` Forge v2 tool with governed, bounded, idempotent GitHub issue mutations without adding a public tool.

**Architecture:** Keep read orchestration in `application/repository/family_v2.py` and add a focused `issue_mutation_v2.py` use case. Repository policy owns allowed operations, approval requirements, create conventions, and external-write ceilings. Application code depends on typed GitHub mutation, approval, idempotency, and durable budget ports; `bootstrap.py` alone selects JSON/GitHub CLI adapters.

**Tech Stack:** Python 3.11+, dataclasses, Pydantic v2, GitHub REST through reviewed `gh api` commands, existing JSON durable-state adapters, shared approval/idempotency plane, pytest/Ruff/Mypy.

## Global Constraints

- Public Forge v2 roster remains exactly 28 tools; all operations stay inside `repo_issue`.
- `comment` is enabled by default; `close`, `reopen`, `link`, and `create` are off by default.
- Every external write requires an idempotency key and full audit evidence.
- `comment` and `close` require a bounded evidence reference; close without evidence fails closed.
- Lost responses must reconcile through embedded markers or live GitHub state before retrying.
- Project V2 remains read-only; links mutate GitHub-native issue relationships only.
- Per-call and per-window external-write ceilings are policy enforced and durable.
- No application module may import a concrete adapter.

---

### Task 1: Typed Issue-Write Policy and Contracts

**Files:**
- Create: `src/repoforge/domain/issue_writes.py`
- Modify: `src/repoforge/config.py`
- Modify: `src/repoforge/application/configuration/source.py`
- Modify: `src/repoforge/application/configuration/document.py`
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `src/repoforge/contracts/v2.py`
- Test: `tests/test_config.py`
- Test: `tests/test_config_admin.py`
- Test: `tests/test_v2_contract_models.py`

**Interfaces:**
- Produces `IssueWritePolicy`, `IssueWriteOperation`, and `IssueLinkType`.
- Extends `RepoIssueInput` with write modes and mode-specific fields validated by a Pydantic model validator.
- Extends `RepoIssueOutput` with typed mutation/approval evidence.

- [ ] **Step 1: Write failing policy and contract tests**

```python
def test_issue_write_policy_defaults_to_comment_only() -> None:
    policy = IssueWritePolicy()
    assert policy.enabled_ops == ("comment",)
    assert policy.max_writes_per_call == 2


def test_repo_issue_close_requires_key_and_evidence() -> None:
    with pytest.raises(ValidationError):
        RepoIssueInput(repo_id="demo", mode="close", issue_number=7)
```

- [ ] **Step 2: Run RED diagnostics**

Run `tests/test_config.py`, `tests/test_config_admin.py`, and the new contract node. Expected: missing policy fields/write modes.

- [ ] **Step 3: Implement strict policy parsing/rendering**

```python
@dataclass(frozen=True, slots=True)
class IssueWritePolicy:
    enabled_ops: tuple[str, ...] = ("comment",)
    approval_required_ops: tuple[str, ...] = ()
    max_writes_per_call: int = 2
    max_writes_per_window: int = 20
    window_seconds: int = 3600
    create_title_prefix: str = "[TASK]"
    create_body_template: str = "## Objective\n{body}\n\n## Evidence\n{evidence_ref}"
```

Parse from `repositories.<repo_id>.issue_writes`, preserve it through resolved documents, and include it in `repo_policy` exact-state preview/apply payloads.

- [ ] **Step 4: Implement strict `repo_issue` write schemas**

Add modes `comment|close|reopen|link|create`, fields `body`, `title`, `evidence_ref`, `target_issue`, `link_type`, `idempotency_key`, and `approval_request_id`; validate exact mode requirements and forbid irrelevant fields.

- [ ] **Step 5: Run GREEN diagnostics and commit policy/contracts checkpoint**

### Task 2: GitHub-Native Mutation Port and Adapter

**Files:**
- Create: `src/repoforge/ports/issue_mutation.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/github/gh_cli.py`
- Modify: `src/repoforge/adapters/github/__init__.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_integration.py`
- Test: `tests/test_github_ticket_graph_adapter.py`

**Interfaces:**
- Produces `IssueMutationGateway` methods for issue state, comments, creation, marker reconciliation, sub-issues, and blocked-by relationships.
- Uses REST endpoints `/issues/{number}/sub_issues` and `/issues/{number}/dependencies/blocked_by`; supersede emits a GitHub-native `Duplicate of #N` marker comment.

- [ ] **Step 1: Write fake-command RED tests for exact argv/body and bounded list calls.**
- [ ] **Step 2: Add typed port records `RemoteIssue`, `RemoteComment`, and `IssueRelationshipResult`.**
- [ ] **Step 3: Implement reviewed `gh api` calls with repository slug derivation, JSON validation, max 100 reconciliation records, and no shell.**
- [ ] **Step 4: Wire the gateway through `AdapterOverrides` and `ApplicationContext`; run adapter/architecture GREEN tests.**

### Task 3: Durable External-Write Budget and Reconciled Idempotency

**Files:**
- Create: `src/repoforge/ports/external_mutation_ledger.py`
- Create: `src/repoforge/adapters/persistence/json_external_mutation_ledger.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/application/idempotency.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_mutation_idempotency.py`
- Test: `tests/test_durable_state.py`

**Interfaces:**
- `ExternalMutationLedger.reserve(repo_id, marker, count, now_epoch, limit, window_seconds)` atomically consumes bounded capacity once per marker.
- `execute_idempotent(..., reconcile_uncertain=...)` reconciles external effects before replay or safe retry; local mutation uncertainty behavior remains unchanged.

- [ ] **Step 1: Write RED tests for duplicate reservations, window expiry, conflicting keys, and lost-response reconciliation.**
- [ ] **Step 2: Implement private JSON ledger with schema/version validation and lock-bound atomic writes.**
- [ ] **Step 3: Add reconciliation hook to shared idempotency orchestration; completed live effects become durable completed receipts rather than duplicate writes.**
- [ ] **Step 4: Run durability/idempotency/architecture GREEN tests.**

### Task 4: Governed `repo_issue` Mutation Use Case

**Files:**
- Create: `src/repoforge/application/repository/issue_mutation_v2.py`
- Modify: `src/repoforge/application/repository/family_v2.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/application/context.py`
- Test: `tests/test_repo_issue_write_tools.py`
- Test: `tests/test_service_tools.py`

**Interfaces:**
- Consumes `IssueWritePolicy`, `IssueMutationGateway`, approval stores, ledger, and reconciled idempotency.
- Produces one typed `RepoIssueV2Result` mutation branch.

- [ ] **Step 1: Write RED end-to-end tests for default comment, disabled close/create/link, mandatory close evidence, title/template enforcement, and per-call/window limits.**
- [ ] **Step 2: Implement bounded redaction and deterministic marker rendering.**

```python
marker = f"<!-- repoforge-issue-write:{sha256(action_request).hexdigest()} -->"
```

- [ ] **Step 3: Implement live reconciliation:** comments scan markers; close/reopen inspect marker plus state; create scans recent issue bodies; sub-issue/blocked-by query native relationships; supersede scans the native duplicate comment marker.
- [ ] **Step 4: Implement optional shared approval:** create exact payload digest request when policy requires approval; accepted requests must match repository, operation, payload digest, and current policy digest before execution.
- [ ] **Step 5: Implement explicit operation execution and typed audit/result evidence; no automatic close or relationship side effects.**
- [ ] **Step 6: Run all issue-write/service GREEN tests.**

### Task 5: Public Contract, Golden, and Release Verification

**Files:**
- Modify: `docs/contracts/tool-schemas-v2.json`
- Modify: `tests/test_v2_schema_golden.py`
- Modify: `tests/test_mcp_contract.py`
- Modify: `tests/test_phase5_architecture.py`

**Interfaces:**
- Keeps tool count at 28 and `repo_issue` as the sole issue tool.

- [ ] **Step 1: Validate actual service results through strict Pydantic output models.**
- [ ] **Step 2: Update the canonical byte-stable schema golden and confirm release-contract diff.**
- [ ] **Step 3: Run Ruff formatter, quick profile, focused issue/config/idempotency suites, then the authoritative full profile.**
- [ ] **Step 4: Commit the complete Addendum 3–4 implementation as an atomic #187 checkpoint.**
