# Issues #163 and #164: Intent-aware verification and baseline hygiene implementation plan

> **Scope:** implement #163 and #164 in one isolated worktree with separate deployable commits. Do not implement #165. Preserve the exact-tree final verification and commit gates.

## Context and baseline

- Workspace: `verification-intent-hygi-b54920139a`
- Branch: `ai/verification-intent-hygiene-b54920139a`
- Base HEAD: `36ddddac70a2bdf0c486746ccf04f84542393410`
- Linked issues: #163 and #164
- The clean base fails the `full` profile before any task change because Ruff reports four pre-existing formatting findings. Those findings are already addressed by draft PR #157. Keep any unavoidable baseline repair isolated from functional commits and drop it during refresh/rebase when #157 lands.
- The currently active RepoForge runtime exposes only the `full` profile and does not expose the checked-in `pytest-target` diagnostic. During this implementation, write tests first and verify their intended failure through the available reviewed profile, recording the exact failing test evidence rather than treating an earlier base-format failure as RED.

## Invariants

1. No caller-supplied executable, argv fragment, environment value, absolute path, or path list.
2. Diagnostic expectation is evidence interpretation only; it never changes command authority or commit eligibility.
3. A non-zero command result is not automatically valid TDD RED.
4. Risk determines required final breadth; verification intent determines the cheapest immediate evidence.
5. Formatter authority exists only in reviewed repository configuration.
6. Changed-path formatting may mutate only server-derived, policy-allowed paths matching the reviewed formatter contract.
7. Exact-base hygiene inspection never mutates or trusts the source clone working tree.
8. Hygiene evidence never satisfies `workspace_verify` or `workspace_commit`.
9. Existing diagnostics, profiles, configs, service callers, and MCP clients remain compatible when new fields are omitted.

## Task 1 — #163 domain contracts and pytest outcome classification

### Tests first

Modify `tests/test_workspace_diagnostics.py` to add focused tests proving:

- assertion failures classify as `test_failure` and count as business tests;
- collection errors classify as `collection_error` and are invalid RED;
- syntax errors classify as `syntax_error` and are invalid RED;
- import errors distinguish `import_error` from missing dependencies;
- missing dependencies, missing tools, timeouts, and environment mismatches remain distinct;
- zero collected tests are not business-test evidence;
- `expectation=fail` is met only by the expected failure class;
- `expectation=pass` is met only by a clean pass;
- omitted expectation preserves the old result shape semantically.

Modify `tests/test_workspace_risk.py` to prove `tdd_red` and `tdd_green` recommend the narrow diagnostic for low, high, and critical risk while retaining every required final profile and manual-review rule.

Run the narrowest available reviewed check and confirm the tests fail because the new contracts/fields are absent, not because of the known base Ruff findings.

### Implementation

- Add `VerificationIntent` to `src/repoforge/domain/verification.py`.
- Add `DiagnosticExpectation` and bounded expected-failure validation to `src/repoforge/domain/diagnostics.py`.
- Extend `ParsedDiagnostic` in `src/repoforge/application/workspace/diagnostic_parser.py` with collected/business-test evidence and deterministic failure classification.
- Add one pure expectation evaluator returning `expectation_met` and `valid_tdd_red_evidence`.
- Extend `recommend_verification` in `src/repoforge/application/workspace/risk.py` with an optional intent defaulting to final-compatible behavior.

### Green check

Run focused diagnostics/risk tests and then the available reviewed profile. Refactor only after the new tests pass.

## Task 2 — #163 application, service, MCP, docs, and compatibility

### Tests first

Add runner/service/MCP tests proving:

- `WorkspaceRunDiagnosticCommand` accepts optional `intent`, `expectation`, and `expected_failure_class`;
- result includes `expectation_met`, `business_tests_ran`, `valid_tdd_red_evidence`, and intent;
- invalid enum values and impossible expected-failure combinations fail before command execution;
- stale fingerprint and mutation protections are unchanged;
- MCP schema is additive and annotations remain local mutation;
- legacy callers omitting all new inputs receive compatible behavior;
- agent guidance routes RED/GREEN to `workspace_run_diagnostic`, not `quick`/`full`.

### Implementation

- Extend `src/repoforge/application/workspace/run_diagnostic.py` typed command/result and next-action routing.
- Extend `src/repoforge/application/service.py` and `src/repoforge/interfaces/mcp/server.py` as thin adapters.
- Update `docs/development/TOOL_REFERENCE.md`, `docs/testing/PLUGIN_TEST_CASES.md`, and relevant starter/golden prompts.
- Update MCP contract fixtures and reviewed release contract only after reviewing the exact tool-schema delta.

### Commit boundary

Commit as `feat(verification): add intent-aware diagnostic evidence` and reference #163.

## Task 3 — #164 hygiene domain and reviewed formatter configuration

### Tests first

Create `tests/test_workspace_hygiene.py` and extend config/config-delta tests to prove:

- normalized finding identity is deterministic;
- base/workspace set comparison yields pre-existing, introduced, resolved, and changed-path findings;
- formatter IDs, summaries, argv templates, include globs, timeout, output bounds, network policy, and parser are typed and bounded;
- check/fix argv must be fixed reviewed arrays and cannot contain shell/path placeholders;
- adding formatter capability is expansion, removing it is restriction, and changing executable/argv/scope is incompatible;
- resolved config round-trips formatters deterministically;
- repository listing exposes safe formatter metadata but never argv.

### Implementation

- Add `src/repoforge/domain/hygiene.py` with formatter policy, finding, comparison, parser, and contract-hash types.
- Extend `src/repoforge/config.py` with `RepositoryConfig.formatters` and a strict `_load_formatters` parser.
- Extend `src/repoforge/application/configuration/document.py`, `src/repoforge/domain/config_generation.py`, and `src/repoforge/application/repository/list.py`.
- Add reviewed Ruff formatter configuration to `config.repoforge.toml` without inferring authority from installed binaries.

## Task 4 — #164 exact-base hygiene status and bounded cache

### Tests first

Add tests proving:

- exact base is materialized from the exact commit into a disposable directory while a dirty source clone remains untouched;
- archive extraction rejects absolute paths, traversal, links, devices, and oversized entries;
- baseline cache identity changes with repo ID, base SHA, config generation, environment identity, formatter contract hash, or expiry;
- corrupt/unsupported cache entries fail closed and recompute;
- workspace status returns explicit unavailable/proposal-ready guidance when no formatter is reviewed;
- findings and outputs are bounded and deterministic.

### Implementation

- Add `src/repoforge/ports/hygiene.py`.
- Add `src/repoforge/adapters/hygiene/command.py` for fixed-argv check/fix execution, safe archive materialization, and Ruff-format output parsing.
- Add a checksum-framed private baseline cache under `src/repoforge/adapters/persistence/json_hygiene_cache.py` with atomic writes, private modes, TTL, bounds, and deterministic keys.
- Wire overrides/composition through `src/repoforge/bootstrap.py` and `src/repoforge/application/context.py`.
- Add `src/repoforge/application/workspace/hygiene_status.py` as a read-only snapshot-bound use case.

## Task 5 — #164 changed-path formatter mutation

### Tests first

Add positive and negative integration tests proving:

- only Git-derived changed paths matching reviewed include globs become separate argv tokens;
- filenames with spaces and safe special characters stay one token;
- caller cannot select paths or argv;
- path-count/output/time limits are enforced;
- denied, symlink, submodule, unchanged, and non-matching paths are never formatted;
- no-op preserves fingerprint and existing verification receipt;
- expected mutation changes fingerprint and invalidates verification;
- unexpected mutation outside the approved set fails closed and reports bounded paths;
- stale fingerprints fail before formatter execution;
- audit contains IDs, counts, digests, and outcome only.

### Implementation

- Add `src/repoforge/application/workspace/format_changed.py`.
- Derive approved paths server-side from `GitRepository.changed_paths` and formatter include globs.
- Snapshot file digests before and after execution, enforce the approved mutation set, and update fingerprint cache/verification state.
- Add `workspace_hygiene_status` and `workspace_format_changed` facade/MCP tools with accurate annotations.
- Add both actions to the correct application policy sets; only formatting is mutating.

### Commit boundary

Commit as `feat(hygiene): add baseline-aware changed-path formatting` and reference #164.

## Task 6 — integration, documentation, release contract, and final verification

- Update server instructions and golden prompts to use:
  `write test -> diagnostic expectation=fail -> implement -> format changed paths -> diagnostic expectation=pass -> final verify`.
- Update `docs/development/TOOL_REFERENCE.md`, testing docs, README capability summary, and config examples.
- Update MCP expected tool set from 47 to 49 and review annotations/schema for both new tools.
- Review the release-contract diff manually; regenerate only for intentional schema/tool changes.
- Run focused suites for diagnostics, risk, config, hygiene, service, MCP, security, import boundaries, and release contracts.
- Run `workspace_diff` and verify no unrelated #157-only formatting changes remain unless required to make the exact tree verifiable.
- Refresh from latest `main` before final verification. If #157 has merged, drop any duplicate format repair automatically through the merge result; otherwise keep it as an isolated dependency commit and explain it in the PR.
- Run the authoritative `full` verification on the exact final tree.
- Commit the exact verified tree, push without force, and create one draft PR closing #163 and #164. Do not close #165.

## Expected commit/PR shape

1. Optional isolated baseline dependency commit only if #157 has not merged and the local gate cannot otherwise produce an exact-tree receipt.
2. `feat(verification): add intent-aware diagnostic evidence` — closes #163 behavior.
3. `feat(hygiene): add baseline-aware changed-path formatting` — closes #164 behavior.
4. Small docs/contract follow-up only when it cannot be kept in the corresponding feature commit.

Draft PR body must report:

- baseline Ruff drift and relation to #157;
- tool/config capability changes;
- threat-model impact and why no arbitrary command/path authority was added;
- focused RED/GREEN evidence actually observed;
- exact final verification result;
- live checks not performed;
- `Closes #163` and `Closes #164`.
