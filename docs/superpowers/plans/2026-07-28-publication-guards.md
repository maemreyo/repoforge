# Publication Guards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete issue #292 by binding every managed push and pull-request publication to an exact reviewed repository topology, operation auth lease, commit/tree/ref identity, permission/version evidence, and durable effect receipt.

**Architecture:** Add one deep publication module whose interface accepts an exact `PublicationIntent` and operation-identity reference. Its implementation owns topology inspection, cross-boundary policy, write-time revalidation, exact-refspec execution, lost-response reconciliation, and safe identity evidence; callers never assemble Git/GitHub publication commands themselves. Reuse `EffectReceiptStore` and operation-result reconciliation as the authoritative durable truth, embedding safe identity evidence in the existing effect identity rather than introducing another receipt store.

**Tech Stack:** Python 3.12, frozen dataclasses, Protocol seams, Git/GitHub adapters, JSON durable state, pytest, Ruff, strict Mypy.

## Global Constraints

- Work only in workspace `epic-284-repository-iden-9164d41f2f` and branch `ai/epic-284-repository-identity-9164d41f2f`.
- Preserve repository-bound stable IDs; owner/name is display metadata only.
- Never use global `gh auth switch`, host-global Git config mutation, ambient credential fallback, raw-secret persistence, or implicit publication targets.
- Company-to-personal publication is denied unless the exact `PublicationIntent` carries an approved cross-boundary policy identity.
- Revalidation failure cannot select another actor, lease, repository, ref, or remote.
- External-write truth remains the existing operation/result/effect-receipt model.
- Use RED/GREEN TDD for every behavior change and run the always-on safety bundle before commit.

---

### Task 1: Domain topology and publication review

**Files:**
- Create: `src/repoforge/domain/publication.py`
- Modify: `src/repoforge/domain/__init__.py`
- Test: `tests/test_publication_guards.py`

**Interfaces:**
- Consumes: `PublicationIntent`, `AuthLease`, `IdentitySurfaceEvidence`.
- Produces: `RepositoryEndpoint`, `RemoteTopology`, `PublicationEvidence`, `ReviewedPublication`, `review_publication(intent, evidence) -> ReviewedPublication`.

- [ ] **Step 1: Write RED tests for exact topology and target drift**

Create fixtures with distinct fetch/push/base/head stable repository IDs, exact refs, commit/tree SHA, URL-rewrite digest, capability/permission digest, actor/installation evidence, lease identity, and remote-version token. Assert changed pushurl, rewrite digest, fork target, transferred repository ID, source SHA, permission digest, remote version, and expired/mismatched lease fail before effect with typed identity/publication errors.

- [ ] **Step 2: Run the focused test file and verify expected failures**

Run: `pytest -q tests/test_publication_guards.py`
Expected: collection/import failure because the publication module is absent.

- [ ] **Step 3: Implement the minimal pure review module**

Implement immutable validated evidence values and one `review_publication` function that:
- matches operation ID, source/destination stable IDs, source/destination refs, expected commit SHA and exact refspec;
- requires one unambiguous push URL after rewrite inspection;
- validates tree SHA, topology digest, actor/installation identity, capability and permission digests, remote version and active target-bound lease;
- denies cross-boundary publication without the exact approval identity;
- returns a digest-bound `ReviewedPublication` with no secret-bearing fields.

- [ ] **Step 4: Run RED/GREEN verification**

Run: `pytest -q tests/test_publication_guards.py`
Expected: all Task 1 tests pass.

### Task 2: Topology inspection and exact effect adapter

**Files:**
- Create: `src/repoforge/ports/publication.py`
- Create: `src/repoforge/adapters/publication.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/__init__.py`
- Test: `tests/test_publication_adapter.py`

**Interfaces:**
- Consumes: reviewed operation auth material, `GitTransportGateway`, and explicit GitHub API identity.
- Produces: `PublicationGateway.inspect(...)`, `PublicationGateway.revalidate(...)`, `PublicationGateway.publish(...)`, and `PublicationGateway.reconcile(...)`.

- [ ] **Step 1: Write RED topology matrix tests**

Cover `remote.<name>.url`, all `remote.<name>.pushurl` entries, ordered `url.*.insteadOf` and `pushInsteadOf` rewrites, fork base/head repository IDs, rename/transfer, multiple push targets, and missing repository metadata.

- [ ] **Step 2: Write RED exact-effect tests**

Assert Git push receives only `intent.exact_refspec` and rejects `HEAD`, `--all`, `--mirror`, wildcard and delete refspecs. Assert PR creation receives explicit base repository, head repository, base ref and head ref after stable-ID revalidation.

- [ ] **Step 3: Run focused tests and verify the intended failures**

Run: `pytest -q tests/test_publication_adapter.py`
Expected: failures because the publication seam and adapter do not exist.

- [ ] **Step 4: Implement inspection, revalidation, exact execution and reconciliation**

Use local Git inspection without mutating config. Execute Git through `GitTransportGateway` with the pinned `ProcessAuthContext`. Execute GitHub writes through the explicit operation-scoped API identity. Re-read topology, actor, permissions and remote version immediately before effect. Reconciliation must query the exact destination ref/PR identity for the same publication ID and never retry through another credential.

- [ ] **Step 5: Run focused RED/GREEN tests**

Run: `pytest -q tests/test_publication_adapter.py`
Expected: all Task 2 tests pass.

### Task 3: Durable publication coordinator

**Files:**
- Create: `src/repoforge/application/publication.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_publication_coordinator.py`

**Interfaces:**
- Consumes: `PublicationGateway`, `OperationIdentityManager`, `EffectReceiptStore`, `OperationResultStore`, clock and ID generator.
- Produces: `PublicationCoordinator.execute(request) -> PublicationOutcome`.

- [ ] **Step 1: Write RED coordinator tests**

Assert the order is inspect → lease consume → review → revalidate → effect. Assert TOCTOU drift stops before boundary. Assert successful and reconciled writes store safe identity metadata in the existing effect receipt and result. Assert lost response reconciles only the same `PublicationIntent`; unknown outcome remains manual and is never blindly retried.

- [ ] **Step 2: Run focused tests and verify expected failures**

Run: `pytest -q tests/test_publication_coordinator.py`
Expected: failures because the coordinator is absent.

- [ ] **Step 3: Implement the coordinator**

Compose existing receipt-first operation lifecycle and `IdempotencyEffectBoundary`. Capture the publication ID, stable repository IDs, exact commit/tree/ref, profile/actor/installation IDs, lease ID, topology/capability/permission/config/policy/remote-version digests and evidence digests in bounded safe effect identities.

- [ ] **Step 4: Run focused RED/GREEN tests**

Run: `pytest -q tests/test_publication_coordinator.py`
Expected: all Task 3 tests pass.

### Task 4: Replace legacy workspace publication paths

**Files:**
- Modify: `src/repoforge/application/workspace/push.py`
- Modify: `src/repoforge/application/workspace/create_draft_pr.py`
- Modify: `src/repoforge/application/workspace/pr.py`
- Modify: `src/repoforge/adapters/github/gh_cli.py`
- Modify: `src/repoforge/ports/github.py`
- Modify: `tests/test_service_tools.py`
- Modify: `tests/test_v2_shipping.py`

**Interfaces:**
- Consumes: `PublicationCoordinator.execute`.
- Produces: unchanged public tool result shape plus durable operation/receipt identity evidence.

- [ ] **Step 1: Write RED integration tests**

Assert `workspace_push` cannot call ambient `GitRepository.push`; it must use exact source/destination refs and the stable destination ID. Assert draft PR creation cannot infer repository from cwd and passes explicit base/head repositories and refs. Assert missing publication identity fails closed.

- [ ] **Step 2: Run focused integration tests and verify expected failures**

Run: `pytest -q tests/test_service_tools.py tests/test_v2_shipping.py`
Expected: new assertions fail against the legacy ambient paths.

- [ ] **Step 3: Route push and PR creation through the coordinator**

Keep current result contracts, optimistic remote-head semantics, idempotency keys, and lost-response behavior. Remove the legacy ambient publication calls from managed paths.

- [ ] **Step 4: Run affected integration and safety tests**

Run: `pytest -q tests/test_publication_guards.py tests/test_publication_adapter.py tests/test_publication_coordinator.py tests/test_service_tools.py tests/test_v2_shipping.py tests/test_operation_identity_leases.py tests/test_git_transport_identity.py tests/test_github_api_identity.py tests/test_security.py`
Expected: all pass.

### Task 5: Documentation, formatting and authoritative verification

**Files:**
- Modify: `docs/development/REPOSITORY_IDENTITY.md`
- Modify: `docs/development/REPOSITORY_IDENTITY_SURFACES.json`
- Modify: `tests/test-groups.toml`

- [ ] **Step 1: Update maintained architecture and release-gate inventory**

Record the publication module seam, adapter identity surfaces, exact-refspec rule, topology evidence, and receipt composition. Add every new test to exactly one group.

- [ ] **Step 2: Format changed files**

Run the reviewed changed-file formatter.

- [ ] **Step 3: Run issue verification**

Run affected tests, Ruff, Ruff format check, strict Mypy, generated-contract checks, test-group manifest validation, and the always-on safety bundle through RepoForge verification.

- [ ] **Step 4: Review and commit issue #292**

Inspect the exact diff, run the code-review workflow, then commit only the verified tree with message `feat(identity): guard exact publication targets (#292)`.
