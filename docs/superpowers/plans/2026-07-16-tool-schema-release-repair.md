# Tool Schema Release Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the already-generated concrete `workspace_edit` item schema and numeric input bounds through the frozen release contract so installed `@forge` clients receive usable argument types.

**Architecture:** Keep runtime validation in the application layer and use typed `Annotated[..., Field(...)]` MCP signatures as the schema source. Add protocol-level regression assertions, then intentionally refresh the frozen public contract and its hash.

**Tech Stack:** Python 3.10+, FastMCP, Pydantic, pytest, RepoForge release-contract generator.

## Global Constraints

- Preserve all workspace safety and optimistic-lock invariants.
- Do not replace typed inputs with `dict[str, Any]`.
- Treat frozen contract changes as reviewed public API changes.
- No GitHub writes are needed for this plan.

---

### Task 1: Lock the MCP schema behavior with protocol tests

**Files:**
- Modify: `tests/test_mcp_contract.py`

**Interfaces:**
- Consumes: `create_server()` and the in-memory MCP client fixture already used in this module.
- Produces: regression assertions for `workspace_edit`, `repo_search`, and `workspace_search` input schemas.

- [ ] **Step 1: Add a failing schema regression test**

Add an async test that indexes `await session.list_tools()` by tool name and asserts:

```python
workspace_edit = tools["workspace_edit"].inputSchema
file_item = workspace_edit["properties"]["files"]["items"]
assert file_item["type"] == "object"
assert set(file_item["required"]) == {"path", "expected_sha256", "edits"}
assert file_item["properties"]["edits"]["items"]["type"] == "object"

for name in ("repo_search", "workspace_search"):
    schema = tools[name].inputSchema["properties"]
    assert schema["context_lines"]["minimum"] == 0
    assert schema["context_lines"]["maximum"] == 5
    assert schema["max_results"]["minimum"] == 1
    assert schema["max_results"]["maximum"] == 200
```

- [ ] **Step 2: Run the narrow test and record its current result**

Run through the repository-enrolled test mechanism. Expected runtime schema assertions: PASS; frozen contract gate remains FAIL until Task 2.

- [ ] **Step 3: Add negative invocation coverage**

Assert MCP rejects `context_lines=-1`, `context_lines=6`, `max_results=0`, and `max_results=201` before service dispatch, and accepts the boundary values.

- [ ] **Step 4: Re-run the MCP contract module**

Expected: all schema and protocol invocation tests PASS.

### Task 2: Refresh the reviewed frozen release contract

**Files:**
- Modify: `docs/contracts/release-contract-v1.json`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `scripts/check_release_contracts.py --write` generated output.
- Produces: frozen contract hash and schemas matching `create_server()`.

- [ ] **Step 1: Apply only the intentional generated drift**

Refresh the contract so it contains:

```json
"context_lines": {"default": 0, "maximum": 5, "minimum": 0, "type": "integer"}
```

for both search tools, corresponding `1..200` bounds for `max_results`, and a concrete inline object under `workspace_edit.properties.files.items`.

- [ ] **Step 2: Review the generated diff**

Confirm the only contract changes are the tool-surface hash, numeric bounds, and `$defs/$ref` replacement with an equivalent inline nested schema. Reject any tool name, annotation, output schema, or unrelated version drift.

- [ ] **Step 3: Add a concise changelog entry**

Record that installed clients can now discover `workspace_edit`'s nested item shape and numeric bounds without a failed-call retry.

- [ ] **Step 4: Run the release-contract check**

Expected: no public release contract drift.

### Task 3: Verify and publish the schema repair

**Files:**
- Preserve: all source and test files outside this plan.

**Interfaces:**
- Produces: one verified commit suitable for the first commit in the draft PR.

- [ ] **Step 1: Review `workspace_diff`**

Confirm scope is limited to spec/plan docs, protocol tests, frozen contract, and changelog.

- [ ] **Step 2: Run the repository `full` verification profile**

Expected: `make check` PASS, including release contract, lint, typecheck, tests, build, and wheel smoke gates.

- [ ] **Step 3: Commit the verified tree**

Commit message:

```text
fix(mcp): publish concrete tool input schemas
```
