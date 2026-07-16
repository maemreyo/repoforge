# MCP Tool Contract Versioning and Verification Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reviewed MCP tool-contract versioning and use it to consolidate `workspace_verify` into `workspace_run_profile` without weakening verification or policy enforcement.

**Architecture:** A pure typed registry owns contract versions, aliases, deprecation windows, removal gates, and capability-based selection. A contract-aware FastMCP adapter filters discovery and invocation per selected version. The application keeps one profile runner; the legacy service/MCP alias delegates to the canonical method.

**Tech Stack:** Python 3.10+, dataclasses/enums, MCP Python SDK 1.28.1, pytest, Ruff, strict Mypy, RepoForge release-contract checker.

## Global Constraints

- Work only in workspace `issues-77-141-contract-v-73a22024a0`.
- Preserve repository, path, command, verification, change-budget, and publication policy.
- Keep `workspace_verify` only as a contract-v1 compatibility alias; contract v2 must not advertise or accept it.
- Alias annotations must equal canonical annotations.
- Run tests before implementation for every behavior change and observe the expected failure.
- Review `workspace_diff` after each meaningful change.
- Run RepoForge `full` verification once on the final exact tree before commit.

---

### Task 1: Typed tool-contract registry and selection policy

**Files:**
- Create: `src/repoforge/domain/tool_contract.py`
- Create: `tests/test_tool_contract.py`

**Interfaces:**
- Produces: `ToolContractRegistry.current_version`, `ToolContractRegistry.resolve(capabilities)`, `ToolContractRegistry.tool_names(version, registered_names)`, `ToolAlias.as_dict()`, and `default_tool_contract_registry()`.
- Consumes: `ClientCapabilities.compatibility_flags` and `ClientCapabilities.legacy`.

- [ ] **Step 1: Write failing registry tests**

```python
def test_default_registry_keeps_verify_only_in_v1() -> None:
    registry = default_tool_contract_registry()
    assert "workspace_verify" in registry.tool_names(1, REGISTERED)
    assert "workspace_verify" not in registry.tool_names(2, REGISTERED)


def test_unknown_requested_version_falls_back_to_current() -> None:
    resolution = registry.resolve(capabilities_with("repoforge-tool-contract-v99"))
    assert resolution.version == 2
    assert resolution.reason == "unknown_requested_version"


def test_removal_without_deprecation_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="deprecation window"):
        ToolContractRegistry(...invalid removal...)
```

- [ ] **Step 2: Run RED test**

Run: `workspace_run_diagnostic(diagnostic_id="pytest-target", selector="tests/test_tool_contract.py")`
Expected: failure because `repoforge.domain.tool_contract` does not exist.

- [ ] **Step 3: Implement the minimal typed registry**

```python
@dataclass(frozen=True, slots=True)
class ContractResolution:
    version: int
    requested_version: int | None
    reason: str

@dataclass(frozen=True, slots=True)
class ToolAlias:
    alias: str
    canonical: str
    deprecated_in: int
    removed_in: int
    notice: str

@dataclass(frozen=True, slots=True)
class ToolContractRegistry:
    current_version: int
    supported_versions: tuple[int, ...]
    aliases: tuple[ToolAlias, ...]

    def resolve(self, capabilities: ClientCapabilities) -> ContractResolution: ...
    def tool_names(self, version: int, registered_names: Collection[str]) -> frozenset[str]: ...
```

- [ ] **Step 4: Run GREEN test**

Run the same diagnostic. Expected: all registry tests pass.

### Task 2: Canonical profile runner and compatibility facade

**Files:**
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/application/service.py`
- Delete: `src/repoforge/application/workspace/verify.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `tests/test_service_tools.py`
- Modify: other tests that call `workspace_verify` as the canonical path.

**Interfaces:**
- `CodingService.workspace_run_profile(workspace_id, profile_name=None, background=False)` selects the default verification profile when omitted.
- `CodingService.workspace_verify(workspace_id, profile_name=None)` delegates directly to `workspace_run_profile` for v1 compatibility.
- Result includes `repo_id` and `used_default` in both paths.

- [ ] **Step 1: Write failing service tests**

```python
def test_run_profile_without_name_uses_default_verification(forge_env) -> None:
    result = forge_env.service.workspace_run_profile(workspace_id)
    assert result["profile"] == "full"
    assert result["verification"] is True
    assert result["used_default"] is True


def test_verify_facade_matches_canonical_result(forge_env) -> None:
    canonical = service.workspace_run_profile(workspace_id)
    alias = service.workspace_verify(other_equivalent_workspace_id)
    assert comparable(alias) == comparable(canonical)
```

- [ ] **Step 2: Run RED focused tests**

Run: `workspace_run_diagnostic(diagnostic_id="pytest-target", selector="tests/test_service_tools.py")`
Expected: failure because `profile_name` is required and result metadata is missing.

- [ ] **Step 3: Implement one execution path**

Use `select_verification_profile` only when `profile_name is None`; otherwise retain `get_profile`. Add `repo_id` and `used_default` to `WorkspaceRunProfileResult`. Remove the standalone verifier object and make the compatibility method call the canonical method.

- [ ] **Step 4: Update canonical callers and run GREEN tests**

Replace final verification calls in tests with `workspace_run_profile(workspace_id)` while retaining dedicated compatibility tests for `workspace_verify`. Run the service, lifecycle, refresh, PR-watch, and CI-evidence focused tests.

### Task 3: Contract-aware MCP discovery, invocation, and alias behavior

**Files:**
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `src/repoforge/interfaces/mcp/capabilities.py`
- Modify: `tests/test_mcp_contract.py`
- Modify: `tests/test_client_capabilities.py`

**Interfaces:**
- `ContractAwareFastMCP.list_tools()` returns only names allowed by the resolved contract.
- `ContractAwareFastMCP.call_tool()` rejects names outside the resolved contract.
- `create_server(..., contract_version: int | None = None)` supports deterministic offline/golden and legacy tests.

- [ ] **Step 1: Write failing MCP tests**

```python
@pytest.mark.anyio
async def test_current_contract_omits_verify_alias(...):
    server = create_server(..., contract_version=2)
    assert "workspace_verify" not in await listed_names(server)

@pytest.mark.anyio
async def test_v1_alias_is_deprecated_and_routes_canonically(...):
    server = create_server(..., contract_version=1)
    verify = await tool(server, "workspace_verify")
    assert "Deprecated" in verify.description
    assert verify.annotations == run_profile.annotations
```

- [ ] **Step 2: Run RED MCP tests**

Expected: `create_server` rejects `contract_version`, and current discovery still contains the alias.

- [ ] **Step 3: Implement contract-aware adapter and alias metadata**

Subclass FastMCP, resolve the request contract from connection capabilities unless an explicit override is supplied, filter `list_tools`, gate `call_tool`, and keep the alias registered with the same `LOCAL_MUTATE` annotations and an explicit migration notice.

- [ ] **Step 4: Run GREEN MCP and capability tests**

Run `tests/test_mcp_contract.py`, `tests/test_client_capabilities.py`, and `tests/test_tool_contract.py`.

### Task 4: Versioned release contract and compatibility gates

**Files:**
- Modify: `src/repoforge/interfaces/mcp/contract.py`
- Modify: `scripts/check_release_contracts.py`
- Preserve: `docs/contracts/release-contract-v1.json`
- Create: `docs/contracts/release-contract-v2.json`
- Modify: `tests/test_phase8_program_completion.py`

**Interfaces:**
- `build_release_contract(contract_version=2)` generates one deterministic version snapshot.
- `build_release_contract_registry()` records supported versions, selection rules, aliases, hashes, and removal gates.
- Checker validates v1 compatibility evidence and exact v2 golden output.

- [ ] **Step 1: Write failing release tests**

```python
def test_release_contract_registry_records_alias_window() -> None:
    registry = asyncio.run(build_release_contract_registry())
    assert registry["current_version"] == 2
    assert registry["aliases"][0]["alias"] == "workspace_verify"
    assert registry["aliases"][0]["removed_in"] == 2


def test_v2_golden_omits_verify_but_v1_keeps_it() -> None:
    assert "workspace_verify" in names(v1)
    assert "workspace_verify" not in names(v2)
```

- [ ] **Step 2: Run RED release tests**

Expected: missing versioned builder and missing v2 golden.

- [ ] **Step 3: Implement builders/checker and review generated drift**

Make v2 the current golden, keep v1 immutable, and run the typed `release-contract-diff` diagnostic. Apply only the reviewed generated delta to `release-contract-v2.json`.

- [ ] **Step 4: Run GREEN release tests**

Run phase-8 tests and the `release-contract-diff` diagnostic. Expected: exact match and both version invariants pass.

### Task 5: Documentation, prompts, full verification, and publication

**Files:**
- Modify: `docs/contracts/README.md`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/testing/PLUGIN_TEST_CASES.md`
- Modify: any server instruction/tool-count assertions affected by the current surface.

- [ ] **Step 1: Update docs and golden prompts**

Document contract v1/v2 selection, alias deprecation/removal, current 46-tool surface, and `workspace_run_profile(workspace_id)` as the sole final verification entry point.

- [ ] **Step 2: Review exact diff**

Run `workspace_diff`; verify no policy broadening, no workflow edits, v1 golden preserved, and only intended alias/surface changes.

- [ ] **Step 3: Run iterative profiles**

Run focused diagnostics, then `quick`, then `test`. Fix any failures with new RED/GREEN cycles.

- [ ] **Step 4: Run final exact-tree verification**

Run RepoForge `full` through `workspace_verify` on the outer harness because the connected server still exposes the pre-change tool set. Require success and record the returned fingerprint/head.

- [ ] **Step 5: Commit, push, and create one draft PR**

Commit with a scoped Conventional Commit, push without force, and create one draft PR whose body includes `Closes #77` and `Closes #141`, verification evidence, compatibility notes, and the live #76 blocker drift observation.
