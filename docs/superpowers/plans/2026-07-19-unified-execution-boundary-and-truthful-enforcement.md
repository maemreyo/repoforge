# Unified Execution Boundary and Truthful Enforcement — Updated Implementation Plan

**Status:** Implemented and production-verified; final evidence commit is prepared for draft-PR publication  
**Source plan:** `ai/unified-execution-boundary-spec-dc59d003db`  
**Rebuilt workspace:** `unified-execution-bounda-ee632b490d`  
**Branch:** `ai/unified-execution-boundary-v2-rebuild-ee632b490d`  
**Base:** `main@9f01d7c691c4891055988a582c1cf83234fd82d8`

## Revalidation against current Forge v2

The original plan assumed `ExecutionEnvironmentPort` was optional in `ApplicationContext`, profile had a raw-executor fallback, diagnostic/ad-hoc bypassed the port, hygiene owned a raw `CommandExecutor`, plan cache used a synthetic platform digest, and verification receipts did not bind commit-time environment truth. Those assumptions were confirmed against the rebuilt workspace before implementation.

Forge v2 had also advanced since the source plan: the static 28-tool contracts, durable accepted plans, typed diagnostics, baseline-aware hygiene, and release-contract v2 were already present. The updated implementation therefore preserves those systems and changes their execution/evidence bindings rather than introducing parallel abstractions.

## Dependency slices and evidence

### Slice 1 — Truthful domain policy and native sessions

**Commit:** `cb80f3898660ce1cfc25c8dedab2fad6e56960c5`  
**Message:** `feat(execution): define truthful policy and native sessions`

- Added requested/effective policy, enforcement assessment, identity schema v2, reuse eligibility, session request/receipt contracts, and bounded public evidence.
- Native backend reports `host_inherited` network and `host_account_access` filesystem behavior.
- Enforcement-required mismatch is rejected before process start.
- RED/GREEN: execution identity, native adapter, and command error-code tests.

### Slice 2 — Coordinator-owned lifecycle

**Commit:** `a0835f1a447b2b557a4af207ca384e0fc42d2394`  
**Message:** `feat(execution): add coordinator-owned sessions`

- Coordinator owns prepare, exact argv admission, execution, inspection, artifact collection, and exactly-once cleanup.
- Added RETURN/RAISE failure semantics and bounded binary execution primitive.
- RED/GREEN: exact argv rejection, cleanup paths, artifact collection, and failure mode tests.

### Slice 3 — Required wiring and repository command routing

**Commit:** `d8c003b29d90dc6574c805505cba089d1780b47e`  
**Message:** `refactor(execution): route repository commands through coordinator`

- Made coordinator required in application context/bootstrap.
- Migrated profile, diagnostic, ad-hoc, and plan environment inspection.
- Removed application fallback to `commands.run`.

### Slice 4 — Formatter, hygiene, and binary snapshots

**Commit:** `1d31b16543157c79de641164cf77fb2a331c147c`  
**Message:** `refactor(hygiene): route formatter commands through execution boundary`

- Migrated formatter checks/fixes and bounded `git archive` bytes through the coordinator.
- Preserved unsafe-tar rejection and stable baseline-cache identity.
- RED/GREEN: 29 hygiene tests plus binary coordinator tests.

### Slice 5 — Verification receipt and commit truth

**Commit:** `e23aab0bbdf401ad8544d2eeacf16336051b96c4`  
**Message:** `feat(verification): bind receipts and commits to execution truth`

- Verification receipts persist environment, requested-policy, and effective-policy hashes.
- Commit recompiles the exact profile request, re-inspects current execution truth, and rejects drift.
- RED/GREEN: integration and service commit-gate tests.

### Slice 6 — Diagnostic and ad-hoc public evidence

**Commit:** `2d52357a4c18e1a13a1f3d4d81bf64f4fcbd817c`  
**Message:** `feat(execution): expose diagnostic and adhoc execution evidence`

- Diagnostic and ad-hoc results project the same typed evidence builder used by profiles.
- Existing strict/relaxed and evidence-only commit-gate semantics remain unchanged.

### Slice 7 — Plan receipt/cache schema v2

**Commit:** `1bd90ac64d2a1c059fc29f86a0c09ee9561c7e9b`  
**Message:** `feat(execution): bind plan cache to delegated evidence`

- Stage receipts and iteration cache advanced to schema v2.
- Plan executor consumes delegated profile/diagnostic evidence and rejects admission-to-execution drift.
- Added bounded raw-envelope reading and intentional schema-v1 miss classification.
- Focused GREEN: plan execution, execution plans, failure intelligence, and verification DAG/cache suites.

### Slice 8 — Closed public contracts and documentation

**Commit:** `36c4846c899787fe5e850bdb6e1219be070a3a01`  
**Message:** `feat(execution): expose truthful enforcement evidence`

- Forward profile/diagnostic/ad-hoc evidence through unified `workspace_verify`.
- Add typed formatter/hygiene execution evidence.
- Add closed `EnforcementEvidenceModel` and `ExecutionEvidenceModel` to v2 outputs.
- Regenerate reviewed tool-schema and release-contract v2 artifacts.
- Preserve exactly 28 tools, existing annotations, and unchanged inputs.
- Update tool, integrity, plugin-test, design, and implementation documentation.

### Slice 9 — Required-wiring regression compatibility

**Commit:** `4b3faacb1ce4572684fdd3e3caf763834e3ae122`  
**Message:** `test(execution): update required wiring fixtures`

- Updated direct `ApplicationContext` test fixtures to supply the required coordinator without reintroducing an optional production fallback.
- Updated legacy result-shape assertions and fake delegated results to include truthful execution evidence.
- Re-ran every formerly failing test file; the background cancellation suite passed both focused and full-file runs.

## Final proof checklist

- [x] Search application source for raw executor and legacy execution-port calls; review every hit.
- [x] Add/confirm architecture regression tests proving no execution bypass.
- [x] Run formatter over all changed Python files.
- [x] Run Ruff and strict Mypy.
- [x] Run focused execution, hygiene, plan/cache, commit, contract, MCP, and docs suites.
- [x] Run release-contract drift check.
- [x] Run authoritative `full` profile on a clean committed implementation tree (`4b3faac`, operation `op-26fea5a6f2194b7f9e85e011`).
- [x] Review `workspace_diff`, generated artifacts, changed paths, and change budget.
- [x] Commit final public-evidence/docs and required-wiring regression slices.
- [x] Re-run authoritative `full` profile on the final evidence commit before publication.
- [x] Push the branch without force.
- [x] Create or update a draft PR with commit list, verification evidence, truthful native-backend caveat, and no-merge statement.

## Publication constraints

- Never force-push.
- Never merge or mark ready for review automatically.
- Never weaken protected-branch, path, verification, or release-contract gates to obtain GREEN.
- Any failure after a tree mutation invalidates prior final verification and requires a fresh full run.
