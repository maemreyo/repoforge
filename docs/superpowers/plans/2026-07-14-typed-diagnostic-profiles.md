# Typed Diagnostic Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repository-reviewed diagnostic profiles with typed selectors, bounded parsers, exact workspace mutation detection, and one thin `workspace_run_diagnostic` MCP operation.

**Architecture:** Store diagnostics as typed per-repository configuration, resolve selectors and argv entirely server-side, execute through the existing constrained command port, and enforce pre/post fingerprint and path-policy invariants in one workspace application use case. Parsers are pure typed adapters; MCP remains a conservative local-mutation facade and diagnostics never grant verification or commit eligibility.

**Tech Stack:** Python 3.10+, frozen dataclasses/enums, TOML configuration, existing Git/command/workspace ports, FastMCP, pytest, Ruff, strict Mypy.

## Global Constraints

- No arbitrary shell, arbitrary argv, free-form environment, unknown executable selection, or model-provided working directory.
- Existing configurations remain valid with `diagnostics={}`.
- The initial network policy is exactly `local_only`; no operating-system network sandbox is claimed.
- Selectors resolve to zero or one bounded argv token and never expand through a shell.
- Read-only diagnostics must preserve the exact workspace fingerprint.
- Artifact diagnostics may change only configured policy-allowed artifact paths and must report every changed path.
- Any fingerprint change invalidates the stored verification receipt.
- Diagnostic success never grants commit eligibility or updates release contracts.
- Audit excludes selector values, command output, excerpts, source bodies, patches, secrets, and environment bodies.

---

### Task 1: Define and load typed diagnostic profiles

**Files:**
- Create: `src/repoforge/domain/diagnostics.py`
- Modify: `src/repoforge/config.py`
- Modify: `src/repoforge/domain/errors.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_workspace_diagnostics.py`

**Interfaces:**
- Produces `DiagnosticSelectorKind`, `DiagnosticNetworkPolicy`, `DiagnosticMutability`, `DiagnosticParserKind`, `DiagnosticSelectorConfig`, and `DiagnosticProfileConfig`.
- Produces `validate_diagnostic_profile(profile) -> DiagnosticProfileConfig` and `RepositoryConfig.diagnostics`.

- [ ] Write failing config tests that load `pytest-target` and `release-contract-diff` profiles and assert frozen typed fields.

```python
profile = loaded.repositories["demo"].diagnostics["pytest-target"]
assert profile.argv_template == ("uv", "run", "--extra", "dev", "pytest", "{selector}", "-q")
assert profile.selector.kind is DiagnosticSelectorKind.PYTEST_NODE
assert profile.mutability is DiagnosticMutability.READ_ONLY
```

- [ ] Add table-driven failing tests for unsafe diagnostic IDs, empty argv, unknown placeholders, missing/extra `{selector}`, invalid selector enum values, non-positive timeout/output limit, unsupported network policy, artifacts without artifact paths, and path traversal in working/artifact paths.
- [ ] Run `uv run --extra dev pytest tests/test_config.py tests/test_workspace_diagnostics.py -q`; expect import or missing-field failures.
- [ ] Implement the enums and frozen profile/selector dataclasses in `domain/diagnostics.py` with closed validation helpers.
- [ ] Add stable error codes: `DIAGNOSTIC_NOT_FOUND`, `DIAGNOSTIC_SELECTOR_REQUIRED`, `DIAGNOSTIC_SELECTOR_INVALID`, `DIAGNOSTIC_STALE_WORKSPACE`, `DIAGNOSTIC_TOOL_MISSING`, `DIAGNOSTIC_TIMEOUT`, `DIAGNOSTIC_PARSER_FAILED`, `DIAGNOSTIC_UNEXPECTED_MUTATION`, and `DIAGNOSTIC_OUTPUT_INVALID`.
- [ ] Implement `_load_diagnostics(raw, repo_id)` in `config.py`; default to `{}` and add `diagnostics` to `RepositoryConfig` after `profiles` so existing positional construction remains compatible.
- [ ] Re-run the focused config tests until green.

### Task 2: Preserve diagnostics through reviewed configuration contracts

**Files:**
- Modify: `src/repoforge/application/configuration/document.py`
- Modify: `src/repoforge/domain/config_generation.py`
- Modify: `src/repoforge/application/repository/list.py`
- Modify: `src/repoforge/application/repository/doctor.py`
- Test: `tests/test_config_generation.py`
- Test: `tests/test_guided_onboarding.py`
- Test: `tests/test_workspace_diagnostics.py`

**Interfaces:**
- Consumes `DiagnosticProfileConfig` from Task 1.
- Produces deterministic resolved TOML tables at `repositories.<repo>.diagnostics.<id>` and semantic capability-delta classification.

- [ ] Write failing rendering tests proving selector tables, argv templates, enum values, artifact paths, timeouts, and output limits survive parse/render/load round trips.
- [ ] Write failing delta tests proving add/remove diagnostics are expansion/restriction; argv/parser/mutability/network/cwd changes are incompatible; timeout/output-limit increases are expansion and decreases are restriction; enum/artifact widening and narrowing are classified directionally; summaries are metadata-only.
- [ ] Run the focused configuration-generation tests and verify the intentional failures.
- [ ] Extend resolved TOML rendering with deterministic diagnostic tables and a nested selector table.
- [ ] Add `diagnostics` to recognized repository fields and implement `_diagnostic_map` plus `_record_diagnostic_changes` in `domain/config_generation.py`.
- [ ] Expose safe profile metadata from `repo_list` without source paths or command output.
- [ ] Extend `doctor` to check each configured diagnostic executable once, using the same executable locator and no execution.
- [ ] Keep guided onboarding non-inferential: proposal application writes `diagnostics = {}` implicitly and does not invent commands.
- [ ] Re-run focused tests until green.

### Task 3: Add selector security and pure parsers

**Files:**
- Create: `src/repoforge/application/workspace/diagnostic_selector.py`
- Create: `src/repoforge/application/workspace/diagnostic_parser.py`
- Modify: `src/repoforge/ports/git.py`
- Modify: `src/repoforge/adapters/git/cli.py`
- Modify: `src/repoforge/ports/command.py`
- Modify: `src/repoforge/adapters/subprocess/command_executor.py`
- Test: `tests/test_workspace_diagnostics.py`

**Interfaces:**
- Produces `ResolvedDiagnosticSelector(value: str | None, argv: tuple[str, ...])`.
- Produces `resolve_diagnostic_selector(profile, selector, *, workspace, repo, git) -> ResolvedDiagnosticSelector`.
- Adds `GitRepository.is_tracked_path(path, relative_path) -> bool` implemented by exact `git ls-files --error-unmatch -- <path>`.
- Extends `CommandResult` with defaulted `stdout_truncated: bool = False` and `stderr_truncated: bool = False`.
- Produces `ParsedDiagnostic(outcome, failure_class, fields, excerpt)` and `parse_diagnostic(profile, result)`.

- [ ] Write failing selector tests for tracked files, pytest `path::node`, leading dash, traversal, denied paths, untracked files, control/shell characters, package/check IDs, enum membership, missing selector, and selector supplied to `none`.
- [ ] Write failing parser tests for pytest pass/fail/error/skipped counts, release-contract match/drift/malformed output, generic text failure, dependency/environment markers, and bounded/truncated excerpts.
- [ ] Run `uv run --extra dev pytest tests/test_workspace_diagnostics.py -q`; expect missing resolver/parser failures.
- [ ] Implement exact tracked-path validation using `assert_path_allowed`, `normalize_relative_path`, symlink rejection, and `GitRepository.is_tracked_path`.
- [ ] Replace `{selector}` once in an immutable argv tuple; reject all unresolved braces.
- [ ] Refactor executor truncation to return `(text, truncated)` and populate the new defaulted `CommandResult` flags without breaking existing five-argument constructors.
- [ ] Implement pure deterministic parsers; do not raise on ordinary diagnostic non-zero exit, but raise `DIAGNOSTIC_PARSER_FAILED` for malformed required parser output.
- [ ] Re-run focused tests until green.

### Task 4: Implement exact-state workspace diagnostic execution

**Files:**
- Create: `src/repoforge/application/workspace/run_diagnostic.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/domain/operations.py`
- Test: `tests/test_workspace_diagnostics.py`
- Test: `tests/test_service_tools.py`

**Interfaces:**
- Produces `WorkspaceRunDiagnosticCommand(workspace_id, diagnostic_id, selector=None, expected_fingerprint=None)`.
- Produces `WorkspaceRunDiagnosticResult` with the exact fields in the design spec.
- Produces `WorkspaceDiagnosticRunner.execute(command)` and `CodingService.workspace_run_diagnostic(...)`.

- [ ] Write failing use-case tests for stale expected fingerprint with zero command calls, exact argv/cwd/timeout/output-limit, read-only unchanged success, non-zero parsed failure result, missing tool, timeout, output truncation, and audit-safe metadata.
- [ ] Write failing mutation tests: read-only mutation fails and reports paths; artifact mutation succeeds only under configured patterns; unrelated/pre-existing changed paths fail closed; every fingerprint change clears `last_verification`; change metrics and refreshed fingerprint are returned.
- [ ] Run focused diagnostic/service tests and verify failures are at the missing use-case boundary.
- [ ] Implement one workspace lock transaction: reload record, capture fingerprint/path set, verify expected fingerprint, resolve selector/cwd, run with `check=False`, inspect post-state, clear and persist stale verification receipt, enforce mutability/artifact patterns, parse, and return deterministic next actions.
- [ ] Catch executor missing-tool/timeout errors, inspect post-state before raising stable diagnostic errors, and never skip mutation detection after a partial command.
- [ ] Add `workspace_run_diagnostic` to mutating and policy-write action sets; audit only IDs/enums/counts/fingerprints/return code/failure class.
- [ ] Wire the typed runner into `CodingService`; do not expose diagnostic creation or argv resolution publicly.
- [ ] Re-run focused tests until green.

### Task 5: Add one conservative MCP tool and freeze contracts

**Files:**
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `tests/test_mcp_contract.py`
- Modify: `tests/test_phase5_mcp_contract.py`
- Modify: `docs/contracts/release-contract-v1.json`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/testing/TESTING.md`
- Modify: `docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md`
- Modify: `config.repoforge.toml`

**Interfaces:**
- Adds MCP tool `workspace_run_diagnostic(workspace_id, diagnostic_id, selector=None, expected_fingerprint=None)`.
- Increases the reviewed MCP inventory from 36 to 37 tools.

- [ ] Add failing actual-client tests for inventory, input schema, `LOCAL_MUTATE` annotations, successful pytest-target invocation, structured diagnostic failure, and selector rejection.
- [ ] Add the thin MCP handler delegating to the service with no policy logic.
- [ ] Add the two reviewed RepoForge diagnostics to `config.repoforge.toml` using local-only/read-only semantics.
- [ ] Update the phase tool-count test to 37 only after reviewing tool title, description, annotations, and schema.
- [ ] Regenerate the release contract with `uv run --extra dev python scripts/check_release_contracts.py --write`, review the exact diff, and retain it only if the sole MCP surface change is `workspace_run_diagnostic`.
- [ ] Document selector kinds, mutation semantics, output bounds, error classes, examples, and the fact that diagnostics do not replace verification.
- [ ] Mark issue #11 complete in the roadmap without claiming immutable plans or background diagnostics.
- [ ] Run diagnostic, MCP, contract, and documentation-focused tests until green.

### Task 6: Review, exact-tree verification, and draft publication

**Files:**
- Review every changed file; no unrelated refactor.

**Interfaces:**
- Produces one verified commit and one draft PR with `Closes #11`.

- [ ] Review `workspace_diff` for arbitrary argv/environment, generic untyped dictionaries at policy boundaries, selectors in audit logs, unsafe path matching, unbounded output, missing verification invalidation, dynamic MCP annotations, or unrelated cleanup.
- [ ] Run the normal repository `full` profile and require release-contract validation, Ruff formatting/lint, strict Mypy, all tests with coverage, source/wheel builds, and installed-wheel smoke.
- [ ] Confirm the final verification fingerprint matches the exact uncommitted tree.
- [ ] Commit with `feat(diagnostics): add typed workspace profiles`.
- [ ] Push without force.
- [ ] Create a draft PR describing selector safety, mutation/fingerprint invariants, parser/output behavior, compatibility, verification evidence, and deferred network sandbox/background work; include `Closes #11`.
- [ ] Read PR mergeability and check rollup; do not merge or mark ready.
