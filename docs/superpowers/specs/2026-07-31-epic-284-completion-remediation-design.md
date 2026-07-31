# EPIC 284 Completion Remediation Design

## Status and decision

This design closes the gap between the identity primitives already present on PR #303 and a production-complete implementation of EPIC #284. It is based on the comprehensive review of PR #303 and the acceptance criteria of issues #285 through #296.

The selected approach is **production-path-first remediation**: connect the existing domain and adapter components into one real application path, propagate the public selector through that path, then fix the security and durability defects exposed by end-to-end tests before repairing the authoritative release gate.

## Goals

1. Make `auth_profile` and `actor_class` determine the exact identity used by every relevant write operation.
2. Build the production composition for repository observation, credential acquisition, operation-scoped leases, Git/GitHub process contexts, capability preflight, publication review, exact effects, and durable receipts.
3. Preserve typed provider and policy failures instead of collapsing them into generic broker outages.
4. Guarantee same-identity refresh semantics, race-safe operation binding, and durable restart/resume round trips.
5. Prove personal/company isolation through production adapters and process boundaries.
6. Make the authoritative production gate green on the exact PR fingerprint before any completion claim.

## Non-goals

- Replacing the existing repository identity, publication, capability, or nested-resource domain models.
- Adding a new MCP tool or a second durable receipt authority.
- Introducing interactive secret entry, global `gh auth switch`, host-global Git/SSH mutation, or ambient credential fallback.
- Refactoring unrelated control-plane, verification, or repository APIs.

## Approaches considered

### A. Big-bang identity rewrite

Replace the current identity and publication modules with a single new orchestration layer.

This could produce a visually simpler end state, but it would discard already-tested domain work, enlarge the regression surface, and make it difficult to map changes back to #285–#296. It is rejected.

### B. Production-path-first remediation — selected

Keep the existing domain models and adapters. Add the missing composition and request factories, thread selectors through command boundaries, and harden defects one seam at a time with RED/GREEN tests.

This approach minimizes new concepts, makes the currently disconnected components useful, and gives each issue concrete acceptance evidence.

### C. Gate-only repair

Fix the failing durability fixtures and production-gate inventory without changing runtime wiring.

This would make CI greener while leaving `auth_profile` semantically ignored and publication unavailable on the standard composition root. It is rejected because it would create false completion evidence.

## Architecture

### 1. Admission identity request

Effectful public contracts continue to accept:

- `auth_profile = auto | <declared-profile-id>`
- `actor_class = human | agent`

The application boundary converts these values exactly once into `AuthProfileSelector`. The selector is then carried by the application command or a dedicated admission request. It must never be validated and discarded.

Read-only branches continue to reject non-default selectors because they cannot truthfully claim to have acted as that identity.

### 2. Repository observation and profile resolution

Repository observation must not depend on whichever account is active in the user's global `gh` configuration.

The observation flow will use provider-neutral local facts first: configured repository path, canonical remote metadata, reviewed provider host, and stable repository binding. Where a live GitHub repository ID is required, the observation must run under an explicitly selected or explicitly named account context. It may not consult ambient `GH_TOKEN`, `GITHUB_TOKEN`, active `gh` account state, credential helpers, or an inherited SSH agent.

For an unbound repository where profile selection itself needs provider evidence, the resolver must evaluate eligible named profiles independently and fail closed on zero, ambiguity, mismatch, or provider unavailability. No profile may be selected by order or by the currently active account.

### 3. Credential and process composition

`build_application()` will construct the production identity stack from reviewed configuration:

1. named-account and GitHub App token sources;
2. `GitHubApiAuthProvider` or equivalent provider-neutral material router;
3. `RepositoryAuthBroker`;
4. isolated Git transport and GitHub API process contexts;
5. capability preflight gateway;
6. operation identity manager and durable store;
7. publication request factory;
8. publication adapter and coordinator;
9. coordinated workspace publication service.

Tests may still override these ports, but the default production composition must no longer require `AdapterOverrides.publications` to enable ordinary managed push and pull-request creation.

### 4. Operation-scoped identity propagation

Before a durable write operation is admitted, the selected profile is resolved, bound, and converted into one or more exact `AuthLease` values. The resulting `OperationIdentityRecord` is propagated through worker bindings, task capsules, resumable receipts, and publication requests.

The race recovery path in `OperationIdentityManager.bind()` must apply the same equality check as the non-racing path: reference, context, and capability requests must all match. A matching context digest alone is insufficient because capability requests are intentionally stored outside that digest.

Running operations never re-resolve from current configuration or global account state. Configuration changes affect future operations unless an explicit revocation invalidates a referenced identity.

### 5. Refresh and typed failures

Credential refresh is permitted only when immutable identity fields remain equal:

- profile;
- actor class and actor ID;
- target kind and stable target ID;
- capability ceiling;
- immutable provider metadata.

Renewable lifecycle metadata, including a fresh preflight observation timestamp, is excluded consistently at the provider and broker layers.

The broker preserves existing `RepoForgeError` values from providers. It maps only unknown provider exceptions to `CREDENTIAL_BROKER_UNAVAILABLE`. This keeps SSO, token approval, ruleset, workflow, network, target, and capability failures typed and recoverable.

### 6. Publication path

Workspace push and draft-PR creation use a production `WorkspacePublicationRequestFactory` that creates an exact `PublicationRequest` from:

- workspace and repository stable IDs;
- selected operation identity reference;
- exact source/destination refs;
- commit and tree SHA;
- remote topology and rewrite evidence;
- target-bound authorization and process auth context;
- exact capability requests;
- idempotency key and reviewed PR content.

The coordinator retains the order:

`inspect topology -> bind/require lease -> capability and identity revalidation -> begin effect boundary -> exact effect -> durable result/receipt`

No managed write may fall back to legacy ambient `git push`, inferred GitHub repository context, another profile, or another target.

### 7. Nested targets

Nested-resource routing remains a separate coordinator over the same operation identity context. Each submodule, LFS endpoint, package target, or release target receives its own least-privilege lease. The primary repository profile is not fallback evidence.

The production request factory must consume nested identity receipts where a publication or operation references nested targets. Missing or changed nested targets require a new operation rather than mutating an existing identity decision.

## Error handling

Every failure before the effect boundary reports unchanged external state. Errors remain typed by domain:

- profile ambiguity or mismatch;
- repository binding stale/missing;
- credential reference missing, expired, revoked, or broker unavailable;
- SSO/token approval/enterprise-policy denial;
- capability or permission evidence unavailable or denied;
- operation identity or capability mismatch;
- publication target/topology/remote-version drift;
- effect outcome unknown after the boundary.

Only unknown provider failures are converted to generic provider-unavailable errors. Recovery actions must never suggest switching a global account or broadening to an unrelated credential.

## Implementation sequence

### Slice 1 — Baseline and production-gate durability repair

Add the missing round-trip cases and complete fixtures for `RepositoryIdentityBinding`, `OperationIdentityRecord`, `OperationWorkerBinding`, and `TaskCapsule`. This makes the current gate failures explicit and gives a stable baseline for later changes.

### Slice 2 — Selector propagation

Thread `AuthProfileSelector` through relevant write commands and admission services. Add tests proving two explicit profiles produce different operation identity decisions and that the selector is not ignored.

### Slice 3 — Broker and lease hardening

Fix typed-error preservation, renewable metadata equality, and race-safe bind equality. Add focused concurrency and refresh regressions.

### Slice 4 — Account-independent repository observation

Remove dependence on the active global `gh` account. Add wrong-active-account, private-repository, missing-profile, and ambiguity matrices through the real command executor boundary.

### Slice 5 — Production identity composition

Construct the default broker, GitHub API provider, Git transport provider, preflight gateway, identity manager, and request factory from accepted configuration.

### Slice 6 — Production publication composition

Build the publication adapter/coordinator/service by default and route workspace push and draft PR through it. Prove exact refs, targets, actors, capability evidence, and receipts through service-level tests.

### Slice 7 — Nested and durable end-to-end scenarios

Exercise worker handoff, restart/resume, nested target routing, expiry/revocation races, and wrong-global-account changes while operations are running.

### Slice 8 — Cross-profile release gate

Run the #296 fault matrix across personal/company profiles, stored account/GitHub App modes, HTTPS/SSH transports, foreground/background/restart paths, publication target changes, broker outages, and secret canaries. Update the authoritative production profile and test inventory only from real production-path evidence.

## Testing strategy

Every production behavior change follows RED -> observed expected failure -> GREEN -> focused refactor.

Required layers:

1. pure domain tests for selection, equality, digest, and error classification;
2. adapter tests using scripted or real bounded process boundaries;
3. application composition tests with no identity/publication override;
4. service and MCP contract tests proving selector propagation;
5. durable round-trip, worker handoff, restart, and reconciliation tests;
6. concurrent personal/company isolation tests;
7. secret-corpus scans across argv, environment captures, logs, exceptions, receipts, fixtures, and serialized state;
8. authoritative RepoForge production verification on the exact final fingerprint.

A passing unit suite is not sufficient if the default composition root still denies all publication or ignores selectors.

## Acceptance mapping

- **#285:** contract inventory and stable identity boundaries remain authoritative and are exercised through production composition.
- **#286:** deterministic account-independent observation, resolution, and durable binding round trips.
- **#287:** production broker wiring, process isolation, typed failures, refresh, revocation, concurrency, and leak scans.
- **#288:** production GitHub API identity provider and exact actor/installation evidence.
- **#289:** production pinned Git transport under isolated process context.
- **#290:** exact durable lease propagation, race safety, restart/resume, and write-time revalidation.
- **#291:** commit author/committer/signer policy remains pinned through worker handoff and configuration changes.
- **#292:** exact publication topology, refs, commit/tree, actor, target, effect and receipt on the real workspace path.
- **#293:** exact capability preflight with preserved typed enterprise failures.
- **#294:** separate least-privilege nested-target leases with no primary-profile fallback.
- **#295:** functional CLI/MCP selectors and diagnostics without global account mutation.
- **#296:** cross-profile fault matrix and authoritative final production gate.

## Completion criteria

EPIC #284 is complete only when all of the following are true on the same final commit:

1. default production composition performs managed identity-bound writes without test-only overrides;
2. explicit and automatic selectors affect the exact operation identity and cannot be ignored;
3. all identified security and durability regressions have focused tests;
4. every child issue has acceptance evidence and a truthful disposition;
5. CI and the authoritative production gate are green;
6. the PR is no longer draft and its description matches the implementation actually present;
7. no raw secret corpus value appears in model-visible or persisted evidence.
