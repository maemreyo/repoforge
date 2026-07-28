# Summary-first workspace diff implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `workspace_diff` return cheap per-file summaries by default while preserving exact, opt-in structured hunks.

**Architecture:** Add `include_hunks=False` at the public contract and thread it unchanged through MCP dispatch, service, command, cursor binding, and audit evidence. Summary mode uses a dedicated Git name-status/numstat adapter and never constructs unified hunks; hunk mode retains the existing implementation.

**Tech Stack:** Python 3.12, Pydantic v2, Git CLI porcelain, pytest, Ruff, strict Mypy, generated JSON contracts.

## Global Constraints

- Keep the public Forge v2 roster at exactly 28 tools.
- `include_hunks` defaults to `False`; callers must opt in to patch hunks.
- Keep `max_files=100` and `byte_budget=120_000`; the byte budget is a safety ceiling.
- Summary mode must not generate, parse, materialize, or discard a unified diff.
- Hunk mode must preserve current patch fidelity.
- Cursor identity must bind `staged`, `path_glob`, and `include_hunks`, but not page-size controls.
- `change_metrics` remains aggregate workspace evidence, not page-local evidence.
- Untracked text files report their line count as additions; untracked binary files report zero additions/deletions.
- Generic MCP resources and the remaining work in #236 are out of scope.
- Preserve authorization, path, symlink, file-size, branch, verification, commit, and publication policies.

---

## Execution preflight: reconcile the live base

The workspace is currently nine commits behind `main`, and upstream overlaps the contract, retrieval, Git adapter, MCP server, and tests touched below. Do not implement against the stale tree.

- [ ] Run `workspace_status` for `local`, `base`, and `hygiene`; record exact `head_sha`, fingerprint, and latest base SHA.
- [ ] Obtain a terminal quick verification receipt for the two planning documents and commit them as `docs: design summary-first workspace diff`.
- [ ] Run `workspace_refresh(action="preview")` against the latest base with exact head/fingerprint locks.
- [ ] Apply that exact preview. Resolve only reported conflicts and preserve both the upstream activation changes and this design.
- [ ] Re-read every file listed in Task 1 before editing; adjust line locations but not public names or behavior.
- [ ] Run the existing focused retrieval and contract tests to establish a green post-refresh baseline:

```bash
uv run --extra dev pytest \
  tests/test_v2_contract_models.py \
  tests/test_v2_retrieval.py \
  tests/test_mcp_contract_v2.py -q
```

Expected: PASS before feature tests are added.

### Task 1: Public contract and end-to-end argument wiring

**Files:**
- Modify: `tests/test_v2_contract_models.py`
- Modify: `tests/test_mcp_contract_v2.py`
- Modify: `src/repoforge/contracts/v2.py`
- Modify: `src/repoforge/application/workspace/retrieval.py`
- Modify: `src/repoforge/application/service.py`

**Interfaces:**
- Produces: `WorkspaceDiffInput.include_hunks: bool = False`
- Produces: `WorkspaceDiffV2Command.include_hunks: bool = False`
- Produces: `CodingService.workspace_diff_v2(workspace_id: str, staged: bool = False, path_glob: str | None = None, max_files: int = 100, byte_budget: int = 120_000, cursor: str | None = None, *, include_hunks: bool = False)`
- Consumes: Pydantic strict input validation and the existing automatic MCP `model_dump` dispatch.

- [ ] **Step 1: Add failing contract tests**

Add these assertions to `test_retrieval_contracts_publish_budget_and_truncation_metadata`:

```python
diff_input = registry.V2_TOOL_SPECS["workspace_diff"].input_model
assert diff_input.model_fields["include_hunks"].default is False
assert diff_input.model_json_schema()["properties"]["include_hunks"]["default"] is False
assert diff_input.model_validate({"workspace_id": "demo"}).include_hunks is False
assert diff_input.model_validate(
    {"workspace_id": "demo", "include_hunks": True}
).include_hunks is True
```

In `tests/test_mcp_contract_v2.py`, validate that the listed `workspace_diff` input schema contains a boolean `include_hunks` property with default `False`, and that the public tool count remains 28.

- [ ] **Step 2: Run tests and observe the intended failure**

```bash
uv run --extra dev pytest \
  tests/test_v2_contract_models.py::test_retrieval_contracts_publish_budget_and_truncation_metadata \
  tests/test_mcp_contract_v2.py -q
```

Expected: FAIL because `include_hunks` is absent.

- [ ] **Step 3: Add the public field and thread it through the service**

In `WorkspaceDiffInput`:

```python
include_hunks: bool = False
```

Append `include_hunks` after the existing command fields so internal positional construction remains compatible:

```python
max_files: int = 100
byte_budget: int = 120_000
cursor: str | None = None
include_hunks: bool = False
```

Keep the existing positional service parameters stable and expose the new option as keyword-only:

```python
def workspace_diff_v2(
    self,
    workspace_id: str,
    staged: bool = False,
    path_glob: str | None = None,
    max_files: int = 100,
    byte_budget: int = 120_000,
    cursor: str | None = None,
    *,
    include_hunks: bool = False,
) -> dict[str, Any]:
    return _result(
        self._workspace_retrieval.diff(
            WorkspaceDiffV2Command(
                workspace_id=workspace_id,
                staged=staged,
                path_glob=path_glob,
                max_files=max_files,
                byte_budget=byte_budget,
                cursor=cursor,
                include_hunks=include_hunks,
            )
        )
    )
```

Do not add custom MCP dispatch code: `_dispatch_kwargs` already forwards strict-model fields by name.

- [ ] **Step 4: Run the focused contract tests**

```bash
uv run --extra dev pytest \
  tests/test_v2_contract_models.py \
  tests/test_mcp_contract_v2.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the contract slice**

```bash
git add \
  src/repoforge/contracts/v2.py \
  src/repoforge/application/workspace/retrieval.py \
  src/repoforge/application/service.py \
  tests/test_v2_contract_models.py \
  tests/test_mcp_contract_v2.py
git commit -m "feat(workspace): add opt-in diff hunks"
```

### Task 2: Dedicated Git summary adapter

**Files:**
- Create: `src/repoforge/adapters/git/diff_summary.py`
- Modify: `src/repoforge/ports/git.py`
- Modify: `src/repoforge/adapters/git/cli.py`
- Create: `tests/test_git_diff_summary.py`

**Interfaces:**
- Produces: immutable `GitDiffSummary(path, status, additions, deletions)`
- Produces: `GitRepository.diff_summary(path, repo, *, staged) -> tuple[GitDiffSummary, ...]`
- Produces: pure `parse_diff_summary(name_status: bytes, numstat: bytes) -> tuple[GitDiffSummary, ...]`
- Consumes: Git `--name-status -z` and `--numstat -z` output.

- [ ] **Step 1: Write failing black-box and parser tests**

Cover all contract statuses with real temporary repositories:

```python
def test_diff_summary_reports_tracked_and_untracked_changes(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "git diff summary")["workspace_id"]
    _, repo, root = service._workspace_retrieval.ctx.workspace(workspace_id)
    (root / "hello.txt").write_text("changed\\nsecond\\n", encoding="utf-8")
    (root / "added.txt").write_text("added\\n", encoding="utf-8")
    subprocess.run(["git", "add", "added.txt"], cwd=root, check=True)
    (root / "README.md").unlink()
    subprocess.run(["git", "mv", "AGENTS.md", "renamed.txt"], cwd=root, check=True)
    (root / "untracked.txt").write_text("one\\ntwo\\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\\x00binary")

    summaries = service._workspace_retrieval.ctx.git.diff_summary(
        root, repo, staged=False
    )
    by_path = {item.path: item for item in summaries}
    assert by_path["hello.txt"].status == "modified"
    assert (by_path["hello.txt"].additions, by_path["hello.txt"].deletions) == (2, 1)
    assert by_path["added.txt"].status == "added"
    assert by_path["README.md"].status == "deleted"
    assert by_path["renamed.txt"].status == "renamed"
    assert by_path["untracked.txt"].additions == 2
    assert by_path["binary.bin"].additions == 0
```

Add a staged-only case proving an unstaged and untracked change is excluded when `staged=True`. Add pure-parser cases for NUL-delimited modified, added, deleted, renamed, and binary records, including a rename whose old and new names contain spaces.

- [ ] **Step 2: Run the new test file**

```bash
uv run --extra dev pytest tests/test_git_diff_summary.py -q
```

Expected: FAIL because the port, parser, and adapter method do not exist.

- [ ] **Step 3: Define the typed port result**

In `src/repoforge/ports/git.py`:

```python
@dataclass(frozen=True, slots=True)
class GitDiffSummary:
    path: str
    status: Literal["added", "modified", "deleted", "renamed"]
    additions: int
    deletions: int
```

Extend `GitRepository`:

```python
def diff_summary(
    self,
    path: Path,
    repo: RepositoryConfig,
    *,
    staged: bool,
) -> tuple[GitDiffSummary, ...]: ...
```

- [ ] **Step 4: Implement the pure NUL parsers**

`diff_summary.py` must:

- parse `M/A/D/R*/C*` name-status records;
- use the destination path for rename/copy records;
- map copies to `added` and type changes to `modified`;
- parse numeric numstat values;
- map `-\t-` binary counts to `0, 0`;
- fail with `CommandError` on malformed records instead of guessing;
- correlate status and counts by normalized destination path;
- sort the final tuple by path.

The public entry point is:

```python
def parse_diff_summary(
    name_status: bytes,
    numstat: bytes,
) -> tuple[GitDiffSummary, ...]:
    statuses = _parse_name_status_z(name_status)
    counts = _parse_numstat_z(numstat)
    return tuple(
        GitDiffSummary(path, status, *counts.get(path, (0, 0)))
        for path, status in sorted(statuses.items())
    )
```

- [ ] **Step 5: Implement the adapter without unified diff construction**

Use `git diff HEAD` for the combined index/worktree view and `git diff --cached` for staged-only view:

```python
base = ["git", "diff", "--no-ext-diff"]
if staged:
    base.append("--cached")
else:
    base.append("HEAD")
name_status = self._executor.run_bytes(
    [*base, "--name-status", "-z", "--"],
    cwd=path,
    max_bytes=self.server.max_fingerprint_bytes,
)
numstat = self._executor.run_bytes(
    [*base, "--numstat", "-z", "--"],
    cwd=path,
    max_bytes=self.server.max_fingerprint_bytes,
)
summaries = list(parse_diff_summary(name_status, numstat))
```

When `staged=False`, append allowlisted regular untracked files from `untracked_paths`. Reject symlinks and files above `max_file_bytes` using the existing errors. Count additions as `data.count(b"\\n") + int(bool(data) and not data.endswith(b"\\n"))`; if `b"\\x00" in data`, use zero additions/deletions. Return one sorted tuple with no duplicate path.

- [ ] **Step 6: Run adapter tests, lint, and strict typing**

```bash
uv run --extra dev pytest tests/test_git_diff_summary.py -q
uv run --extra dev ruff check \
  src/repoforge/adapters/git/diff_summary.py \
  src/repoforge/adapters/git/cli.py \
  src/repoforge/ports/git.py \
  tests/test_git_diff_summary.py
uv run --extra dev mypy src/repoforge
```

Expected: all commands PASS.

- [ ] **Step 7: Commit the adapter slice**

```bash
git add \
  src/repoforge/adapters/git/diff_summary.py \
  src/repoforge/adapters/git/cli.py \
  src/repoforge/ports/git.py \
  tests/test_git_diff_summary.py
git commit -m "feat(git): provide hunk-free diff summaries"
```

### Task 3: Summary/hunk routing, cursor identity, and audit evidence

**Files:**
- Modify: `src/repoforge/application/workspace/retrieval.py`
- Modify: `tests/test_v2_retrieval.py`
- Modify: `tests/test_mcp_runtime_coverage.py`

**Interfaces:**
- Consumes: `GitRepository.diff_summary(path: Path, repo: RepositoryConfig, *, staged: bool) -> tuple[GitDiffSummary, ...]`
- Produces: default `WorkspaceDiffV2Result.files[*].hunks == ()`
- Produces: explicit `include_hunks=True` existing hunks
- Produces: cursor request binding containing `include_hunks`
- Produces: audit detail `include_hunks`.

- [ ] **Step 1: Change the existing retrieval test to express both modes**

Replace the implicit-hunk assertion with summary-first assertions:

```python
summary = service.workspace_diff_v2(workspace_id, staged=False)
assert all(item["hunks"] == [] for item in summary["files"])
hello_summary = next(item for item in summary["files"] if item["path"] == "hello.txt")
assert (hello_summary["additions"], hello_summary["deletions"]) == (2, 1)

detailed = service.workspace_diff_v2(
    workspace_id,
    staged=False,
    include_hunks=True,
    path_glob="hello.txt",
    max_files=1,
)
hello_diff = detailed["files"][0]
assert hello_diff["hunks"][0]["header"].startswith("@@")
assert {line["kind"] for line in hello_diff["hunks"][0]["lines"]} >= {
    "add",
    "delete",
}
```

Make the staged multi-hunk test pass `include_hunks=True`.

- [ ] **Step 2: Add routing and cursor tests**

Add tests that monkeypatch `ctx.git.diff_summary` and `ctx.git.diff`:

```python
def test_summary_mode_never_calls_full_diff(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "summary routing")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed\\n", hello["sha256"])
    retrieval = service._workspace_retrieval
    monkeypatch.setattr(
        retrieval.ctx.git,
        "diff",
        lambda *args, **kwargs: pytest.fail("full diff called in summary mode"),
    )

    result = service.workspace_diff_v2(workspace_id)

    assert all(item["hunks"] == [] for item in result["files"])
```

Add the inverse test: `include_hunks=True` must not call `diff_summary`.

Create a two-page summary and a two-page hunk response. Assert same-mode cursors resume successfully, while passing a summary cursor to hunk mode and a hunk cursor to summary mode raises `ValueError` matching `cursor.*request`. Also assert a cursor remains valid when only `max_files` or `byte_budget` is reduced.

Parameterize `include_hunks` over `(False, True)` and prove `path_glob`, `max_files`, and byte-budget truncation work in both modes. Add an unchanged-workspace case returning `files == []`. Capture the `ctx.audited` details for one call in each mode and assert `include_hunks` is `False` and `True` respectively, so post-activation audit data can distinguish adoption.

- [ ] **Step 3: Run the retrieval tests and observe failures**

```bash
uv run --extra dev pytest \
  tests/test_v2_retrieval.py \
  tests/test_mcp_runtime_coverage.py -q
```

Expected: FAIL because mode routing, cursor binding, and audit detail are absent.

- [ ] **Step 4: Route summary mode directly to the Git summary port**

At the start of the diff operation:

```python
if command.include_hunks:
    files, source_truncated = self._hunk_diff(command, repo, workspace, head_sha)
else:
    files = [
        StructuredDiffFile(
            path=item.path,
            status=item.status,
            additions=item.additions,
            deletions=item.deletions,
            hunks=(),
        )
        for item in self.ctx.git.diff_summary(
            workspace,
            repo,
            staged=command.staged,
        )
    ]
    source_truncated = False
```

Keep the current staged and unstaged hunk implementation in a focused private method:

```python
def _hunk_diff(
    self,
    command: WorkspaceDiffV2Command,
    repo: RepositoryConfig,
    workspace: Path,
    head_sha: str,
) -> tuple[list[StructuredDiffFile], bool]:
    source_truncated = False
    if command.staged:
        raw = self.ctx.git.diff(workspace, repo, staged=True)
        files = list(parse_unified_diff(raw["diff"]))
        for parsed_file in files:
            assert_path_allowed(parsed_file.path, repo)
        source_truncated = bool(raw["truncated"])
    else:
        files = []
        for raw_path in sorted(self.ctx.git.changed_paths(workspace, repo)):
            path = assert_path_allowed(raw_path, repo)
            try:
                base = self.ctx.git.read_snapshot_blob(
                    workspace, repo, head_sha, path
                ).data
            except RepoForgeError as exc:
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
                base = None
            candidate = resolve_workspace_path(workspace, path, repo)
            if candidate.is_file() and not candidate.is_symlink():
                if candidate.stat().st_size > self.ctx.config.server.max_file_bytes:
                    raise SecurityError(
                        f"Diff target exceeds max_file_bytes: {path}"
                    )
                current = candidate.read_bytes()
            else:
                current = None
            diff_file = build_diff_file(path, base, current)
            if diff_file is not None:
                files.append(diff_file)
    return files, source_truncated
```

This is the existing hunk behavior moved without semantic changes. Apply `path_glob` after either mode returns and before pagination.

- [ ] **Step 5: Bind mode into cursor and audit identity**

Use this exact pagination request:

```python
request={
    "staged": command.staged,
    "path_glob": command.path_glob,
    "include_hunks": command.include_hunks,
}
```

Add `"include_hunks": command.include_hunks` to the `workspace_diff_v2` audit details. Keep `max_files` and `byte_budget` out of the cursor binding so smaller resumed pages remain valid.

- [ ] **Step 6: Run retrieval, integration, and runtime tests**

```bash
uv run --extra dev pytest \
  tests/test_v2_retrieval.py \
  tests/test_integration.py \
  tests/test_service_tools.py \
  tests/test_mcp_runtime_coverage.py -q
```

Expected: PASS. Update legacy assertions that genuinely require patch text to pass `include_hunks=True`; do not weaken their hunk assertions.

- [ ] **Step 7: Commit routing and pagination**

```bash
git add \
  src/repoforge/application/workspace/retrieval.py \
  tests/test_v2_retrieval.py \
  tests/test_integration.py \
  tests/test_service_tools.py \
  tests/test_mcp_runtime_coverage.py
git commit -m "feat(workspace): default diff retrieval to summaries"
```

### Task 4: Agent guidance, generated contracts, and payload regression

**Files:**
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/testing/FULL_FLOW_TESTING.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_docs_command_drift.py`
- Modify: `tests/test_mcp_contract_v2.py`
- Modify: `tests/test_v2_retrieval.py`
- Regenerate: `docs/contracts/tool-schemas-v2.json`
- Regenerate: `docs/contracts/release-contract-v2.json`

**Interfaces:**
- Produces: public guidance for summary → selection → narrow hunk request.
- Produces: generated schema with `include_hunks=false`.
- Produces: measurable summary payload regression guard.

- [ ] **Step 1: Add failing guidance and payload tests**

In `tests/test_mcp_contract_v2.py`, list tools and assert the `workspace_diff` description contains all three phrases:

```python
assert "summary" in description
assert "include_hunks=True" in description
assert "path_glob" in description
```

In `tests/test_v2_retrieval.py`, create 40 modified text files with several changed lines each, then compare serialized public outputs:

```python
summary = service.workspace_diff_v2(workspace_id, max_files=40)
detailed = service.workspace_diff_v2(
    workspace_id,
    include_hunks=True,
    max_files=40,
)
summary_bytes = len(json.dumps(summary, separators=(",", ":")).encode())
detailed_bytes = len(json.dumps(detailed, separators=(",", ":")).encode())
assert summary_bytes < detailed_bytes
assert summary_bytes <= detailed_bytes // 3
```

The ratio, not an absolute byte constant, prevents platform-dependent brittleness while proving material reduction.

- [ ] **Step 2: Run the failing tests**

```bash
uv run --extra dev pytest \
  tests/test_mcp_contract_v2.py \
  tests/test_v2_retrieval.py -q
```

Expected: FAIL on description guidance and the schema golden until implementation/generation is complete.

- [ ] **Step 3: Update the MCP description atomically**

Set the public description to:

```python
"workspace_diff": (
    "Return a structured bounded diff for the current workspace tree. "
    "Defaults to a hunk-free per-file summary; inspect paths and change counts first, "
    "then request include_hunks=True with a narrow path_glob and small max_files only "
    "for files whose patch content is needed. Follow next_cursor when truncated."
),
```

Do not add another tool or alter the title.

- [ ] **Step 4: Update operator and test-flow documentation**

Document two concrete calls:

```json
{"workspace_id":"demo-workspace"}
```

and:

```json
{
  "workspace_id":"demo-workspace",
  "include_hunks":true,
  "path_glob":"src/repoforge/application/workspace/retrieval.py",
  "max_files":1
}
```

State explicitly that `change_metrics` describes the whole workspace, while `files` is the filtered/paginated selection. State that 120 KB is a safety cap, not a target response size. Update full-flow testing so both default summary and explicit narrow hunk mode are exercised. Add an `Unreleased` CHANGELOG entry calling out the new default and showing the migration: callers that need patches must now send `include_hunks=true` with a narrow filter.

- [ ] **Step 5: Regenerate reviewed schemas**

```bash
make schemas
```

Expected changes are limited to the `workspace_diff` input schema/digest, server description-derived contract material, and any deterministic release-contract hashes. Review the generated diff; reject unrelated tool-roster or output-schema changes.

- [ ] **Step 6: Run docs, schema, and payload tests**

```bash
uv run --extra dev pytest \
  tests/test_docs_command_drift.py \
  tests/test_mcp_contract_v2.py \
  tests/test_v2_contract_models.py \
  tests/test_v2_retrieval.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit guidance and generated contracts**

```bash
git add \
  src/repoforge/interfaces/mcp/server.py \
  docs/development/TOOL_REFERENCE.md \
  docs/testing/FULL_FLOW_TESTING.md \
  CHANGELOG.md \
  docs/contracts/tool-schemas-v2.json \
  docs/contracts/release-contract-v2.json \
  tests/test_docs_command_drift.py \
  tests/test_mcp_contract_v2.py \
  tests/test_v2_retrieval.py
git commit -m "docs(workspace): teach summary-first diff review"
```

### Task 5: Repository-wide verification and exact-tree commit evidence

**Files:**
- Modify only files required by formatter, strict typing, or deterministic generated-contract drift.
- Do not change behavior merely to silence a failing test; diagnose each failure against the design.

**Interfaces:**
- Consumes: all prior task commits.
- Produces: exact verified fingerprint suitable for RepoForge commit/push policy.
- Produces: evidence that the public roster remains 28 and both modes work in production composition.

- [ ] **Step 1: Format only server-derived changed paths**

Run `workspace_format_changed` and inspect the returned path list. Expected formatter: `ruff-format`.

- [ ] **Step 2: Run focused quality gates**

```bash
make lint
make typecheck
make test-affected
```

Expected: PASS.

- [ ] **Step 3: Run v2 release and build gates**

```bash
make v2-gates
make build
```

Expected: PASS, exactly one wheel and one source distribution.

- [ ] **Step 4: Run the stable full production verification contract**

```bash
make check
```

Expected: PASS with contract generation clean, full tests green, strict typing green, v2 gates green, and build artifacts valid.

- [ ] **Step 5: Inspect the final summary-first diff**

Call default `workspace_diff` first and confirm every returned `hunks` array is empty. Then call `include_hunks=True` separately for each implementation path that needs review. Follow cursors; do not raise the working response size to consume the entire patch in one call.

- [ ] **Step 6: Commit only the exact verified tree**

Use `workspace_commit` with the terminal verification fingerprint and exact current `head_sha`. Commit any final formatter/generated drift as:

```text
chore: finalize summary-first workspace diff
```

Do not push, activate, or publish unless the user explicitly requests that external state transition.
