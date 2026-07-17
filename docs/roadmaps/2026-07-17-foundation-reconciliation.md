# Foundation and Issue Reconciliation — 2026-07-17

This record captures the reviewed closure decisions after the v2 foundation and durable-state batches landed. GitHub remains authoritative; this repository does not add issue-write authority. The pull request carrying this record closes completed or superseded issues through explicit PR closure keywords.

## Completed initiatives

### #70 — Durable State Platform

Close as completed.

- #71 delivered shared typed envelopes, revisions, CAS, private atomic JSON persistence, corruption/future-schema failures, and OperationTask adoption.
- #72 delivered deterministic adjacent-version migrations, explicit reverse steps, backup-before-write journals, rollback, and restart recovery.
- #73 delivered explicit reference-aware retention, protected records, orphan detection, count/byte quotas, bounded private trash, resume, and idempotency.
- #74 delivered bounded integrity diagnostics plus strict backup/restore preview and recovery workflows.
- TaskCapsule and ApprovalRequest foundations consume the shared substrate.

### #102 — Robust patch application and low-latency mutations

Close as completed.

- #111–#114 delivered whitespace parity, actionable patch failures, OpenAI apply-patch input, and deterministic tolerant unified-diff normalization.
- #115 delivered lock-scoped fingerprint caching.
- #116 delivered fresh optimistic-lock tokens from mutating operations.
- The reproduced patch and round-trip failure classes are covered by regression tests and the production gate.

## Foundation tickets whose remaining public surface is a new concern

### #18 — TaskCapsule domain model and durable store

Close the foundation ticket as completed. `TaskCapsule`, bounded value objects, explicit transitions, resume projection, shared durable persistence, restart behavior, revisions, and application service are on `main`.

Public task tools remain intentionally outside the frozen v2 roster. Any future task surface must be a new additive ticket with consolidated typed operations, policy gating, client-capability presentation, and no reconstruction from chat history.

### #81 — ApprovalRequest domain model and durable store

Close the foundation ticket as completed. Typed approval subjects, bindings, capability deltas, decisions, private durable storage, revision checks, expiry/staleness behavior, and pending policy-change migration are on `main`.

Public approval/Elicitation adapters remain post-cutover additive work and must be specified independently from the completed foundation.

## Code-intelligence and evidence reconciliation

### Keep #35 open

#35 remains the umbrella for shared evidence, code intelligence, architecture drift, semantic risk, and knowledge-pack work. It is not completed by the baseline provider alone.

### Close #37 in favor of #189

The provider-neutral `CodeIntelligencePort`, typed evidence, snapshot binding, syntax fallback, and affected-test candidates landed already. #189 is the canonical remaining v2 ticket: tree-sitter as the primary provider, the landed syntax adapter as fallback, corpus-calibrated confidence, and verification routing.

### Keep #38 open as the post-cutover provider home

#38 remains the home for optional CodeGraph, Semble, LSP, or another reviewed sidecar behind the provider-neutral port. It must follow #189 and must pass local secret-safe egress before returning model-bound snippets.

## Consumer-efficiency reconciliation

- Close #134: explicit command failure codes and exit-code evidence are implemented.
- Close #135: failed profile step, command, exit code, and completed-step evidence are implemented without output bodies.
- Close #136: server guidance now directs iteration to diagnostics/quick profiles and reserves the full profile for the final gate.
- #137–#140 are already closed and remain valid completed slices.
- Close #141 as superseded by #190, whose consolidated `workspace_verify` design absorbs run-profile, diagnostic, and ad-hoc planning rather than reviving the older direction.
- Keep #133 open for remaining consumer-efficiency and usage-driven work not covered by these closures.

## GitHub-native governance reconciliation

- Close #159 as superseded. Project V2 mutation/apply is retired; GitHub-native issues, sub-issues, and dependencies are authoritative, while Project access remains read-only evidence.
- Close #161 as superseded. The checked-in ticket graph is no longer authoritative, so staging graph reconciliation workspaces would reintroduce a retired projection.
- Close #162 as superseded. Its workflow assumes proposal/publication/reconciliation and Project-write surfaces that are no longer the accepted architecture. Any future ticket intake must use new typed, consolidated, policy-gated GitHub-native contracts.

## Additional supersession cleanup

- Close #153 in favor of #188. Typed three-way conflict evidence belongs inside the journaled mutation/accept-resolution transaction rather than a standalone read-only surface.
- Close #16 in favor of #190. Unified verification planning already provides the assessment and recommended verification projection requested by `workspace_assess`.
- Close #27. Dynamic capability-grouped discovery conflicts with the frozen static v2 surface decision; future additive tools follow normal versioned contract evolution.

## #195 completion

#195 is completed by this reconciliation plus the requirement-evolution lifecycle delivered in the same pull request:

- stale and closed-blocker metadata is surfaced as repair evidence;
- supersession, split, merge, invalidation, and partial completion become typed and selection-aware;
- #37/#141/#153/#16/#27/#159/#161/#162 receive explicit canonical successors or retirement decisions;
- #35 and #38 retain their valid remaining scope;
- #18/#81 are closed as completed foundations while public surfaces remain separate post-cutover work.

## Pull-request closure set

The carrying pull request should close:

- #70, #102, #18, #81, #195;
- #37, #134, #135, #136, #141;
- #153, #16, #27;
- #159, #161, #162;
- #50 and #69 after their implementation commits pass the authoritative production gate.

It must not close #35, #38, #133, #189, or #190.
