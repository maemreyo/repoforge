# Nested Identity Routing Design

**Issue:** #294  
**Parent:** #284  
**Status:** Approved for implementation on 2026-07-30

## Objective

Route every nested repository or external provider endpoint through an identity lease bound to that exact target. Submodules, Git LFS, package registries, and release-upload endpoints must never inherit a stronger primary-repository credential merely because they are encountered inside the primary workspace.

This design composes the existing repository binding resolver, credential broker, Git/API transport adapters, `OperationIdentityContext`, `AuthLease`, capability preflight, and `PublicationIntent`. It does not create a parallel identity, capability, workspace-ownership, or receipt authority.

## Boundaries and invariants

- #22 remains authoritative for workspace mutation ownership and takeover. Nested auth leases do not grant workspace mutation ownership.
- #51 remains authoritative for credential consumption and external-write capabilities. A resolved profile does not imply permission.
- #52 remains authoritative for provider-neutral workload identity. Local repository routing must continue to work without an external identity provider.
- #232 remains authoritative for durable operation/effect truth. Nested routing records safe evidence in existing operation identity records and receipts.
- Repository names and URLs are discovery metadata. Stable provider repository IDs bind repository targets.
- One `(target_kind, target_id)` has exactly one pinned lease in an operation identity context.
- Public read-only resources may use an explicit anonymous policy decision. Private or write-capable resources require a resolved binding and profile.
- No recovery action selects another profile, expands a capability, changes an endpoint, or falls back to ambient credentials.
- Raw tokens, keys, headers, helper responses, signing material, and credential-bearing URLs never enter domain values, logs, exceptions, receipts, or fixtures.
- Every external write has an exact target contract. Repository/release writes use `PublicationIntent`; package or LFS writes use the same exact-target fields through a nested publication projection and are denied when that projection is absent.

## Architecture

The implementation adds one shared nested-resource pipeline rather than embedding routing in `PublicationCoordinator` or creating four unrelated adapter-specific policy systems.

### 1. Domain target model

`repoforge.domain.nested_identity` owns pure, secret-free values:

- `NestedResourceKind`: `submodule`, `lfs`, `package`, `release`.
- `NestedAccess`: `read`, `write`.
- `NestedResourceCandidate`: discovery evidence containing kind, canonical endpoint, source location, recursion depth, and requested access.
- `NestedResourceTarget`: resolved target containing provider host, stable target ID, optional stable repository ID, owner boundary, required capability IDs, and an endpoint digest.
- `NestedRoutingDecision`: `bound_profile`, `anonymous_read`, or `denied` plus safe evidence.
- `NestedIdentityReceipt`: one safe target/lease/routing projection per distinct provider boundary.

Constructors validate bounded strings, canonical endpoints, safe IDs, unique capabilities, recursion depth, and digests. Payloads never include credential references or endpoint userinfo.

A pure `route_nested_resource(...)` function applies these rules:

1. Exact stable-ID binding and eligible profile succeeds.
2. An explicitly public read target may return `anonymous_read` only when repository policy permits it.
3. Missing, ambiguous, disabled, transferred, cross-boundary, or write-without-intent targets fail closed.
4. Company/personal owner changes require an exact policy approval; the primary repository profile is never considered fallback evidence.
5. Required capabilities come from target kind and access, not from the selected profile's broader ceiling.

### 2. Discovery port and Git adapter

`repoforge.ports.nested_identity.NestedResourceDiscovery` exposes a read-only discovery seam. `GitNestedResourceDiscovery` implements it without mutating Git configuration or fetching content.

Submodule discovery:

- Parse `.gitmodules` through bounded `git config --file .gitmodules --get-regexp` output rather than a permissive handwritten URL parser.
- Normalize relative submodule URLs against the containing repository's reviewed canonical URL.
- Recurse only into already-present checked-out submodule worktrees.
- Track canonical path ancestry and stable endpoint digests to detect cycles.
- Enforce configured depth/resource-count/output bounds.
- Reject duplicate paths, URL userinfo, local/file transports, shell-style URL injection, and path escape.

LFS discovery:

- Read reviewed repository configuration and `.lfsconfig` through bounded Git configuration commands.
- Represent the effective LFS API endpoint explicitly.
- Never invoke `git lfs fetch`, smudge, upload, or credential lookup during discovery.

Package and release discovery:

- Package endpoints are supplied by reviewed ecosystem adapters as canonical registry targets; this issue does not perform dependency resolution or package installation.
- Release endpoints are derived from reviewed provider repository metadata and exact release operation input.
- Environment variables, ambient CLI accounts, credential helpers, and home-directory registry configuration are not discovery authorities.

### 3. Resolution and lease acquisition ports

`NestedTargetResolver` resolves a candidate to safe provider metadata and stable IDs. Repository-like targets reuse the existing repository metadata and binding resolver. Non-repository endpoints resolve through an explicit provider endpoint registry keyed by provider host and stable target ID.

`NestedLeaseProvider` acquires a least-privilege `AuthLease` for one resolved target and exact capability tuple. It delegates credential access to the existing broker and provider adapters. Anonymous reads produce no credential lease and are represented separately; they cannot be upgraded to writes.

A lease must match:

- operation ID;
- target kind and target ID;
- provider and stable repository ID when present;
- selected profile and actor class;
- exact capability request;
- policy/config revisions;
- safe material and provider metadata digests;
- bounded issue/expiry timestamps.

### 4. Application coordinator

`NestedIdentityCoordinator` is the application seam. Its request includes the primary operation identity context, primary repository metadata, reviewed discovery bounds, requested nested operations, policy/config revisions, and any exact cross-boundary approvals.

The coordinator:

1. Discovers and canonicalizes candidates.
2. De-duplicates by `(kind, endpoint_digest)` while preserving every source location for diagnostics.
3. Resolves each candidate independently.
4. Applies capability and owner-boundary policy.
5. Acquires one least-privilege child lease for every credentialed target.
6. Produces a replacement immutable `OperationIdentityContext` containing the unchanged primary lease plus child leases.
7. Produces one `LeaseCapabilityRequest` for every current lease.
8. Binds the complete context once through `OperationIdentityManager.bind` before mutation, verification requiring private resources, or external effects.
9. Emits safe nested routing receipts.

The coordinator never mutates an already-bound operation identity decision. Discovery and binding happen before the operation sidecar is created. Retry/resume uses the original context reference. A newly discovered target after binding is an identity mismatch requiring a new operation, not an in-place privilege expansion.

### 5. Exact write targets

Release publication reuses `PublicationIntent(kind=RELEASE)` and extends durable publication support without weakening push/PR validation.

For LFS and package writes, `NestedPublicationIntent` contains:

- publication and operation IDs;
- resource kind;
- stable source repository ID;
- stable destination target ID;
- endpoint digest;
- exact object/package/version digest;
- capability and permission digests;
- optional exact cross-boundary approval ID.

It projects into the existing effect-receipt/idempotency boundary. Revalidation immediately before effect requires the same lease, target, endpoint digest, capability/permission evidence, policy/config revisions, and payload digest. Endpoint redirects or repository transfers fail before effect.

This issue provides the contract and revalidation seam; it does not implement general package publishing, dependency management, repository mirroring, or release asset generation.

## Failure and recovery

Failures use existing `RepoForgeError` and `RepositoryAuthFailureCode`/`ErrorCode` families, adding typed codes only where current codes cannot distinguish safe operator action:

- malformed or unsafe nested endpoint;
- recursive discovery limit or cycle;
- binding required/ambiguous/stale;
- public anonymous access denied;
- cross-boundary nested resource denied;
- capability denied;
- target or endpoint changed before effect;
- child lease missing, expired, revoked, or mismatched;
- provider metadata unavailable.

Every failure names the safe target kind/ID or endpoint digest, missing capability, and source location where available. Recovery actions are limited to reconciling that exact binding, reauthorizing the selected profile, approving the exact boundary, refreshing the same identity, retrying after provider/network recovery, or aborting.

Failures must state that no nested credentialed action or external effect was admitted. They must never include raw URLs containing userinfo or secrets.

## Public test seams

Tests observe behavior only through these public seams:

1. `GitNestedResourceDiscovery.discover(...)` for recursive submodule and LFS fixtures.
2. `route_nested_resource(...)` for deterministic mixed-owner/public/private policy matrices.
3. `NestedIdentityCoordinator.prepare(...)` for child lease acquisition and immutable operation-context composition.
4. `OperationIdentityManager.bind/resume/require_write` for persistence, retry, and exact-target lease consumption.
5. Nested publication review/revalidation for endpoint, capability, and boundary TOCTOU cases.
6. Safe payloads/receipts and corpus leak scans for credential propagation.

Required matrices include:

- company primary repository with public, company-private, and personal-private submodules;
- nested and cyclic submodules, relative URLs, duplicate endpoints, depth/count bounds, unsafe URL schemes, and path escape;
- default/custom LFS endpoints with read/write capability separation;
- GitHub package read/write endpoints and release-upload targets;
- anonymous public read allowed/denied;
- missing, ambiguous, stale, disabled, renamed, and transferred bindings;
- explicit exact cross-boundary policy and default deny;
- child lease expiry/revocation/refresh, operation mismatch, restart/resume, and unchanged-primary-lease behavior;
- endpoint redirect/transfer between preflight and effect;
- secret canaries across output, errors, state, receipts, logs, and subprocess captures.

## Composition and compatibility

Existing single-account repositories without nested resources preserve current behavior. Existing persisted operation identity records remain decodable because nested leases use the existing additive `auth_leases` collection and capability request collection.

Production bootstrap receives explicit nested discovery, resolver, and lease-provider dependencies. There is no ambient default implementation that consults global `gh`, Git helpers, SSH agents, home-directory package credentials, or environment tokens. If required nested routing dependencies are unavailable, private access and writes fail closed while explicitly allowed public anonymous reads remain possible.

The maintained repository identity surface inventory is updated for every new Git/provider/subprocess adapter. A structural test prevents future nested-resource commands from bypassing the reviewed coordinator.

## Delivery slices

1. Pure target/routing contracts and mixed-owner policy matrices.
2. Bounded recursive submodule and LFS discovery.
3. Resolution, lease acquisition, immutable context composition, and durable sidecar integration.
4. Package/release endpoint contracts and exact write revalidation.
5. Production composition, inventory/contracts/docs, leak scans, and authoritative verification.

Each slice follows red → green at the public seam and ends with focused tests plus the always-on repository identity safety bundle.
