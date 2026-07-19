# Round-4 Contract and Process Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Forge v2 structured errors and recovery actions truthful at discovery, harden timeout descendant cleanup against PID reuse, and publish practical verification-input bounds without changing the 28-tool roster.

**Architecture:** Keep each existing success model as the application validator, but compose it with one shared `ToolFailure` through a registry-owned Pydantic `TypeAdapter` for the public output schema. Emit recovery evidence as `kind + exact arguments`, move OS process identity/tree inspection into a focused subprocess adapter, and define selector sequence bounds on the array type itself so JSON Schema renders `maxItems`.

**Tech Stack:** Python 3.12, Pydantic 2.13, MCP Python SDK 1.28, pytest, Ruff, Mypy, standard-library `/proc` and bounded `ps` fallback.

## Global Constraints

- Keep the static Forge v2 roster at exactly 28 tools; add or rename no public tool.
- Add no dependency or privileged process-control feature.
- Preserve every successful output payload shape.
- Structured success and structured failure must both conform to the advertised output schema.
- Recovery `arguments` must validate directly with `V2_TOOL_SPECS[kind].validate_input(arguments)`.
- `selector`, `selector2`, and `argv` accept at most 100 items of at most 4,096 characters each.
- Never signal a captured PID after its process start identity changes.
- Implement each behavior test-first and observe the intended RED failure before production edits.

---

## File Structure

- `src/repoforge/contracts/common.py`: shared success metadata, `ToolError`, and concrete `ToolFailure`.
- `src/repoforge/contracts/registry.py`: public success/failure union adapter, output validation, and schema rendering.
- `src/repoforge/contracts/v2.py`: exact discriminated recovery-action wire models and selector sequence aliases.
- `src/repoforge/domain/failure_intelligence.py`: render exact target-tool arguments from typed domain recovery actions.
- `src/repoforge/adapters/subprocess/process_tree.py`: bounded process identity and descendant discovery.
- `src/repoforge/adapters/subprocess/command_executor.py`: timeout orchestration using identity-safe process-tree helpers.
- `src/repoforge/interfaces/mcp/server.py`: advertise/validate the union contract and return typed failures.
- `tests/test_mcp_contract_v2.py`, `tests/test_v2_schema_golden.py`: public union/schema coverage.
- `tests/test_failure_intelligence.py`: direct recovery-argument validation.
- `tests/test_command_executor_error_codes.py`: isolated descendant and PID-reuse coverage.
- `docs/development/TOOL_REFERENCE.md`, `docs/testing/TEST_RUN_RECORD.md`: contract and metadata verification evidence.
- `docs/contracts/tool-schemas-v2.json`, `docs/contracts/release-contract-v2.json`: generated reviewed goldens.

---

### Task 1: Advertise and validate one shared success/failure output union

**Files:**
- Modify: `src/repoforge/contracts/common.py`
- Modify: `src/repoforge/contracts/registry.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `tests/test_mcp_contract_v2.py`
- Modify: `tests/test_v2_schema_golden.py`

**Interfaces:**
- Produces: `ToolFailure`, `ToolContractSpec.output_schema()`, `validate_success_output()`, `validate_failure_output()`, and union-aware `validate_output()`.
- Preserves: `ToolContractSpec.output_model` as the tool-specific success model.

- [ ] **Step 1: Write failing public-union tests**

Add tests that build one valid shared failure envelope, then for every spec assert both union-schema visibility and failure validation:

```python
def _failure_payload() -> dict[str, object]:
    return {
        "status": "failed",
        "summary": "Request failed",
        "error": {
            "code": "NOT_FOUND",
            "message": "Repository not found",
            "why": "The repository id is not enrolled.",
            "retryable": False,
            "safe_next_action": "Choose an enrolled repository.",
            "details": {"correlation_id": "corr-1"},
            "unchanged_state": ["No state changed."],
            "automatic_retry_allowed": False,
        },
    }


def test_every_advertised_output_schema_accepts_shared_failure() -> None:
    for spec in V2_TOOL_SPECS.values():
        validated = spec.validate_output(_failure_payload())
        assert isinstance(validated, ToolFailure)
        schema = spec.output_schema()
        assert "anyOf" in schema
```

Update the protocol error test to call `V2_TOOL_SPECS["repo_read"].validate_output(result.structuredContent)` rather than validating only `ToolResponse`.

- [ ] **Step 2: Run RED tests**

Run:

```sh
uv run --extra dev pytest -q \
  tests/test_mcp_contract_v2.py::test_every_advertised_output_schema_accepts_shared_failure \
  tests/test_mcp_contract_v2.py::test_protocol_error_is_one_redacted_typed_envelope
```

Expected: failure because concrete tool output models require success fields and no union schema exists.

- [ ] **Step 3: Add the shared failure and registry adapter**

In `common.py`, make successful metadata success-only and add:

```python
class ToolResponse(StrictModel):
    status: Literal["ok"] = "ok"
    summary: str = Field(min_length=1, max_length=500)
    error: None = None


class ToolFailure(StrictModel):
    status: Literal["failed"]
    summary: str = Field(min_length=1, max_length=500)
    error: ToolError
```

In `registry.py`, create one adapter per tool:

```python
from pydantic import TypeAdapter

@dataclass(frozen=True, slots=True)
class ToolContractSpec:
    name: str
    input_model: type[StrictModel]
    output_model: type[ToolResponse]

    def _output_adapter(self) -> TypeAdapter[ToolResponse | ToolFailure]:
        return TypeAdapter(self.output_model | ToolFailure)

    def output_schema(self) -> dict[str, object]:
        return self._output_adapter().json_schema(mode="validation")

    def validate_success_output(self, payload: Mapping[str, object]) -> BaseModel:
        return self.output_model.model_validate(payload)

    def validate_failure_output(self, payload: Mapping[str, object]) -> ToolFailure:
        return ToolFailure.model_validate(payload)

    def validate_output(self, payload: Mapping[str, object]) -> BaseModel:
        return self._output_adapter().validate_python(payload)
```

Cache the adapter in the frozen spec if profiling shows construction is material; do not introduce a global mutable cache.

- [ ] **Step 4: Wire discovery and server validation**

Use `spec.output_schema()` in schema bundles and MCP `list_tools`. Use `validate_success_output()` in the normal path and `validate_failure_output()` in the structured-error path.

- [ ] **Step 5: Run GREEN tests and commit**

Run the two RED tests plus `tests/test_mcp_contract.py` and `tests/test_v2_schema_golden.py`. Expected: all pass except the golden mismatch intentionally addressed in Task 5.

Commit:

```sh
git add src/repoforge/contracts/common.py src/repoforge/contracts/registry.py \
  src/repoforge/interfaces/mcp/server.py tests/test_mcp_contract_v2.py \
  tests/test_v2_schema_golden.py
git commit -m "fix(contract): advertise typed failure unions"
```

---

### Task 2: Emit exact callable recovery arguments

**Files:**
- Modify: `src/repoforge/domain/failure_intelligence.py`
- Modify: `src/repoforge/contracts/v2.py`
- Modify: `tests/test_failure_intelligence.py`

**Interfaces:**
- Produces: public recovery shape `{kind, precondition, arguments}`.
- Consumes: existing `RecoveryAction` domain invariants and each existing v2 input model.

- [ ] **Step 1: Replace the test-only translator with a failing direct assertion**

Delete `_reconstruct_real_input`. For every produced action assert:

```python
payload = action.payload()
arguments = payload["arguments"]
assert isinstance(arguments, dict)
V2_TOOL_SPECS[action.kind.value].validate_input(arguments)
assert set(payload) == {"kind", "precondition", "arguments"}
```

Add contract-schema assertions that each `kind` variant has a typed `arguments` schema rather than one generic optional-field bag.

- [ ] **Step 2: Run RED test**

Run `uv run --extra dev pytest -q tests/test_failure_intelligence.py::test_recovery_actions_name_only_real_v2_tools_with_reconstructible_calls`.

Expected: failure because `payload()` exposes flattened generic fields and mutate/refresh need translation.

- [ ] **Step 3: Render exact arguments in the domain**

Keep typed invariant fields internally, but make `payload()` dispatch by `kind` and return only exact input arguments. The mutate branch must produce:

```python
arguments = {
    "workspace_id": self.workspace_id,
    "operations": [{"op": "restore", "paths": list(self.relative_paths)}],
    "expected_head_sha": self.expected_head_sha,
    "expected_workspace_fingerprint": self.expected_workspace_fingerprint,
}
```

The refresh branch uses `expected_fingerprint`; verify uses `plan_through`; operation includes `action="get"`. Remove `None` values before returning.

- [ ] **Step 4: Replace the public generic model with discriminated variants**

Define six strict action variants in `v2.py`, reusing the exact existing input models as their `arguments` type:

```python
class WorkspaceMutateRecoveryAction(StrictModel):
    kind: Literal["workspace_mutate"]
    precondition: str = Field(min_length=1, max_length=500)
    arguments: WorkspaceMutateInput


FailureRecoveryAction = Annotated[
    OperationRecoveryAction
    | WorkspaceStatusRecoveryAction
    | WorkspaceVerifyRecoveryAction
    | WorkspaceRefreshRecoveryAction
    | WorkspaceMutateRecoveryAction
    | ConfigInspectRecoveryAction,
    Field(discriminator="kind"),
]
```

- [ ] **Step 5: Run GREEN tests and commit**

Run all `tests/test_failure_intelligence.py` and contract model tests. Expected: pass.

Commit:

```sh
git add src/repoforge/domain/failure_intelligence.py src/repoforge/contracts/v2.py \
  tests/test_failure_intelligence.py
git commit -m "fix(recovery): emit exact callable arguments"
```

---

### Task 3: Make descendant cleanup identity-safe and tests isolated

**Files:**
- Create: `src/repoforge/adapters/subprocess/process_tree.py`
- Modify: `src/repoforge/adapters/subprocess/command_executor.py`
- Modify: `tests/test_command_executor_error_codes.py`

**Interfaces:**
- Produces: `ProcessIdentity(pid: int, ppid: int, start_token: str)`, `snapshot_descendants(root_pid, limit=4096)`, `identity_is_current(identity)`, `kill_identity(identity, sig)`.
- Consumes: standard-library `/proc`, bounded `ps`, `os.kill`, and existing timeout orchestration.

- [ ] **Step 1: Write RED tests for exact identity and isolated descendant cleanup**

Replace global `pgrep` with a child PID file and unique marker. Assert the exact recorded identity disappears. Add a unit test:

```python
def test_kill_identity_skips_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = ProcessIdentity(pid=123, ppid=12, start_token="old")
    monkeypatch.setattr(process_tree, "read_identity", lambda pid: ProcessIdentity(pid, 1, "new"))
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    assert process_tree.kill_identity(captured, signal.SIGKILL) is False
    assert kills == []
```

Add parser tests for Linux `/proc/<pid>/stat` including a command name containing spaces/parentheses.

- [ ] **Step 2: Run RED tests**

Run the new process-tree tests and exact descendant test. Expected: import/function failures and the old global test removed.

- [ ] **Step 3: Implement bounded process identity discovery**

Create a frozen `ProcessIdentity`. On Linux, enumerate at most 4,096 numeric `/proc` entries and parse PPID/starttime from stat field 4/22 using the final `)` boundary. On Darwin, read at most 1 MiB from `ps -Ao pid=,ppid=,lstart=` with a two-second deadline, terminate the probe after the byte cap, and parse a stable start token. Return an empty snapshot on unsupported platforms or bounded probe failure.

`kill_identity` must re-read identity immediately before `os.kill` and return false on absence or token mismatch.

- [ ] **Step 4: Integrate identity-safe timeout sweeping**

In `_communicate`, snapshot descendants before the first group signal. Replace direct PID sweeping with `kill_identity`. Keep all existing bounded waits and direct root-process fallback. Never call `os.kill` on a captured descendant without identity revalidation.

- [ ] **Step 5: Run GREEN tests and commit**

Run all command-executor and cancellation tests. Expected: pass without global process matching.

Commit:

```sh
git add src/repoforge/adapters/subprocess/process_tree.py \
  src/repoforge/adapters/subprocess/command_executor.py \
  tests/test_command_executor_error_codes.py
git commit -m "fix(runtime): validate process identity before cleanup"
```

---

### Task 4: Publish practical selector bounds

**Files:**
- Modify: `src/repoforge/contracts/v2.py`
- Modify: `tests/test_v2_contract_models.py`
- Modify: `tests/test_v2_schema_golden.py`

**Interfaces:**
- Produces: selector scalar-or-sequence schema with `maxItems=100` and item `maxLength=4096`.

- [ ] **Step 1: Write RED runtime and schema tests**

Add assertions that 101 selector items fail validation and that both selector array branches publish `maxItems: 100`:

```python
with pytest.raises(ValidationError):
    WorkspaceVerifyInput(workspace_id="ws-1", selector=tuple("x" for _ in range(101)))

array_branch = next(branch for branch in selector_schema["anyOf"] if branch.get("type") == "array")
assert array_branch["maxItems"] == 100
assert array_branch["items"]["maxLength"] == 4096
```

- [ ] **Step 2: Run RED tests**

Expected: runtime accepts 101 items or discovery lacks `maxItems`.

- [ ] **Step 3: Put constraints on the sequence branch**

Use:

```python
_SelectorItem = Annotated[str, Field(min_length=1, max_length=4096)]
_SelectorItems = Annotated[tuple[_SelectorItem, ...], Field(max_length=100)]
_Selector = _SelectorItem | _SelectorItems
```

Declare `selector: _Selector | None = None` and the same for `selector2`; do not put `max_length` on the outer union field.

- [ ] **Step 4: Run GREEN tests and commit**

Run contract-model and schema tests. Expected: pass before golden regeneration.

Commit:

```sh
git add src/repoforge/contracts/v2.py tests/test_v2_contract_models.py \
  tests/test_v2_schema_golden.py
git commit -m "fix(contract): publish selector item limits"
```

---

### Task 5: Regenerate contracts, document behavior, and verify the release candidate

**Files:**
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/testing/TEST_RUN_RECORD.md`
- Modify: `docs/contracts/tool-schemas-v2.json`
- Modify: `docs/contracts/release-contract-v2.json`

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: reviewed generated contracts and recorded metadata verification.

- [ ] **Step 1: Update documentation**

Document that every tool advertises success-or-`ToolFailure`, recovery actions contain exact `arguments`, workspace mutate/verify annotations are conservative tool-wide hints, and verify selectors/argv use 100 × 4,096 limits.

- [ ] **Step 2: Run and record metadata prompt checks**

Execute the direct, indirect, and negative cases in `docs/testing/PLUGIN_TEST_CASES.md`. Append one dated row/section to `TEST_RUN_RECORD.md` with exact cases, observed tool choices, confirmation behavior, and pass/fail; do not claim unavailable live-client evidence.

- [ ] **Step 3: Regenerate reviewed goldens**

Run:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/repoforge-pr225-r4-venv \
UV_CACHE_DIR=/tmp/uv-cache-pr225-r4 make schemas
```

Review that the roster remains 28 and each output schema has a failure union.

- [ ] **Step 4: Run focused and full verification**

Run, in order:

```sh
uv run --extra dev ruff check .
uv run --extra dev mypy src/repoforge
uv run --extra dev pytest -q tests/test_mcp_contract_v2.py tests/test_failure_intelligence.py tests/test_command_executor_error_codes.py tests/test_v2_contract_models.py tests/test_v2_schema_golden.py
uv run --extra dev pytest
make v2-gates
scripts/verify-production.sh --allow-dirty
git diff --check
```

Expected: every command exits zero. If any command flakes, diagnose it; do not record a retry as proof without fixing or explicitly documenting the nondeterminism.

- [ ] **Step 5: Review and commit**

Run the repository code-review workflow against `36c51f1...HEAD`, fix actionable findings, then commit:

```sh
git add docs/development/TOOL_REFERENCE.md docs/testing/TEST_RUN_RECORD.md \
  docs/contracts/tool-schemas-v2.json docs/contracts/release-contract-v2.json
git commit -m "docs(contract): record round4 hardening"
```

Do not push until the user requests publication or confirms the final local commit set.
