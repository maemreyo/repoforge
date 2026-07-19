# Issue #215 Post-Mutation Syntax Gate Implementation Plan

## Goal

Add bounded, non-blocking Tree-sitter syntax evidence to every `workspace_mutate` dry-run, no-op, apply, keyed receipt, and keyed replay response.

## Constraints

- Preserve the static 28-tool Forge v2 roster.
- Support only the pinned Python, JavaScript, JSX, TypeScript, and TSX grammars.
- Unsupported grammar, invalid UTF-8, parser exceptions, and observed parse-budget overruns return `unknown`, never false success.
- Never echo source bodies or absolute paths.
- Cap diagnostics at 100 and messages at 500 characters.
- Diagnostics never alter mutation readiness, transaction commit, rollback, or change-budget semantics.
- Receipt schema v2 must read historical schema v1 receipts without rewriting them.
- Final branch includes or refreshes onto PR #227's `ChangeMetrics` contract fix before publication.

## Task 1 — Typed syntax analyzer

Files:
- Create `src/repoforge/domain/syntax_diagnostics.py`
- Create `src/repoforge/application/syntax_diagnostics.py`
- Create `tests/test_syntax_diagnostics.py`

Steps:
1. Write RED tests for valid Python, malformed Python, unsupported Markdown, invalid UTF-8, mixed known/unknown paths, deleted paths, diagnostic cap, and observed timeout.
2. Run the exact pytest node/file and confirm failures are caused by missing analyzer behavior.
3. Implement immutable enums/dataclasses and a deterministic Tree-sitter analyzer.
4. Run GREEN and refactor with tests still passing.

## Task 2 — Mutation and receipt integration

Files:
- Modify `src/repoforge/application/workspace/mutate_enhanced.py`
- Modify `tests/test_workspace_mutate.py`

Steps:
1. Write RED tests for dry-run malformed Python without disk change, applied malformed Python, repair-to-ok, unsupported file unknown, no-op semantics, and keyed replay parity.
2. Add planner access to final changed virtual bytes.
3. Analyze once after planning and bind the evidence into every result branch.
4. Advance receipts to schema v2 and decode schema v1 as explicit legacy `unknown` evidence.
5. Run focused GREEN tests, including corruption and crash-recovery regressions.

## Task 3 — Forge v2 contract

Files:
- Modify `src/repoforge/contracts/v2.py`
- Modify `tests/test_v2_contract_models.py`
- Modify `tests/test_mcp_contract_v2.py`
- Regenerate `docs/contracts/tool-schemas-v2.json`
- Regenerate `docs/contracts/release-contract-v2.json`

Steps:
1. Write RED contract tests for closed state/severity enums, 100-item bounds, explicit truncation, and complete `WorkspaceMutateOutput` validation.
2. Add `SyntaxDiagnosticItem` and `SyntaxDiagnosticsEvidence` to the output model.
3. Regenerate deterministic contract goldens and run `release-contract-diff` GREEN.

## Task 4 — Documentation, latency evidence, and publication

Files:
- Modify `docs/development/TOOL_REFERENCE.md`
- Modify `CHANGELOG.md`
- Add or extend benchmark/acceptance coverage only where deterministic.

Steps:
1. Document advisory semantics and `unknown` behavior.
2. Run focused tests, quick profile, then authoritative full profile on the exact tree.
3. Review structured diff and verify no source bodies, host paths, unrelated files, or handwritten schema drift.
4. Commit, push, create a draft PR, and watch CI.
