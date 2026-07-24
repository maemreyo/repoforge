# Runtime Activation Handoff Design

## Context

RepoForge already pins each MCP request to one immutable `GenerationServiceContainer`, commits the durable configuration pointer before an atomic router swap, and drains the old `OperationGate` before restart fallback. The remaining gap is outcome truth across connector replacement: activation has no durable receipt created before candidate construction, a lost reload response can strand the caller without knowing whether the new generation became active, and drain rejection does not carry the exact new runtime contract identity required for deterministic rediscovery.

Issue #245 is the approved product specification for this design.

## Decisions

### 1. Dedicated activation receipt, shared operation identity

Runtime activation receives a focused `RuntimeActivationReceipt` rather than overloading the generic `EffectReceipt`. Activation must preserve classifications and identities that are not generic mutation fields:

- `hot_reload`
- `restart_fallback`
- `reload_failed`
- `active_but_client_stale`
- `rolled_back`

Every attempt creates both `op-<24hex>` and `receipt-<24hex>` before contract validation or candidate construction. The operation uses `kind="runtime_activation"` and binds `receipt_id` plus `result_reference="runtime-activation:<receipt_id>"` at terminal success.

### 2. Activation identities are explicit and bounded

A receipt stores three optional `RuntimeActivationIdentity` values:

- `previous_identity`: the runtime/config identity observed before activation.
- `accepted_identity`: the accepted target generation and resolved/source digests.
- `active_identity`: the authoritative identity observed after activation or reconciliation.

An identity contains only redaction-safe facts:

- configuration generation
- configuration source and resolved SHA-256
- runtime active generation
- process identity, when observed
- tool-surface hash, when observed
- runtime phase

No argv, host path, environment value, token, or raw configuration is persisted.

### 3. One activation journal owns lifecycle transitions

`RuntimeActivationJournal` is a narrow coordinator over:

- `OperationStore`
- `RuntimeActivationStore`
- `IdGenerator`
- `Clock`

It does not depend on `ApplicationContext` or the full coding-service graph. It creates the operation and receipt atomically in ordering terms before any candidate work, advances phases with compare-and-swap persistence, and finalizes both records from one classification.

If receipt creation fails after operation creation, the operation is marked failed before effect. No candidate is built.

### 4. Candidate validation precedes routing changes

Hot reload preserves the current sequence and strengthens its evidence:

1. Create activation operation and receipt.
2. Validate generated contract artifacts.
3. Build the candidate immutable service container.
4. Verify generation, repository set, resolved config identity, and contract/tool-surface identity.
5. Begin bounded reconnect drain on the old gate.
6. Commit the durable active-generation pointer and install the candidate while router acquisition is serialized.
7. Persist active identity and terminal receipt.
8. Retire the previous container after pinned requests finish.

Existing admitted requests remain pinned to the old container. New requests during the bounded handoff receive a typed reconnect outcome; after the swap they acquire only the new complete generation.

### 5. Lost response reconciliation uses authoritative state

Before applying a target generation, the activator searches for a non-terminal receipt for the same accepted identity. It then compares:

- configuration store active generation and resolved digest
- runtime record active generation and process identity
- router active generation for in-process reload

If the target is already active and identities match, the receipt is finalized as `active_but_client_stale`; the candidate/effect is not repeated. If evidence conflicts, the receipt remains non-terminal or is classified `reload_failed` with manual recovery guidance rather than guessed success.

### 6. Drain returns `RECONNECT_REQUIRED`

`InProcessOperationGate` gains an optional immutable reconnect payload when entering drain. Calls not yet admitted receive `RepoForgeError(code=RECONNECT_REQUIRED)` with:

- new active/config generation
- new tool-surface hash
- input/output contract digests
- process-start identity when available
- activation operation and receipt IDs
- `rediscovery_action="reconnect_and_repo_list"`

The MCP error boundary exposes only these closed typed fields. After reconnection, the existing contract handshake remains authoritative and rejects stale clients with `CLIENT_CONTRACT_STALE` before mutation.

### 7. Continuation reference is durable but execution remains owned by existing reconcilers

Activation accepts an optional bounded `continuation_reference`. The receipt persists it unchanged after validation. Runtime replacement does not execute arbitrary continuation data. On startup or reconnect, existing durable operation/outcome reconcilers resume the referenced issue/config workflow; activation only preserves the pointer and exposes it in its terminal result.

This keeps issue publication and configuration reconciliation in their current subsystems while preventing connector replacement from losing the handoff reference.

## Error and rollback semantics

- Candidate/contract/config probe failure before swap: `reload_failed`, effect boundary false, previous runtime unchanged.
- Hot reload commit succeeds but response is lost: reconcile to `active_but_client_stale`.
- Incompatible or unavailable hot reload: explicit `restart_fallback` path.
- Restart failure with safe previous generation restored: `rolled_back`.
- Restrictive activation that cannot restore safety: existing fail-closed behavior remains authoritative; receipt records `reload_failed` and the exact runtime identity retained.
- No automatic retry crosses an unknown activation effect boundary.

## Public surface

No 29th MCP tool is added. Existing surfaces carry the evidence:

- `operation` exposes the activation operation lifecycle.
- runtime/config commands return `operation_id`, `activation_receipt_id`, classification, identities, and continuation reference.
- typed MCP errors expose reconnect identity during drain.

The static tool roster remains 28.

## Test strategy

Focused tests are added to existing tracked runtime modules to avoid coupling this branch to the test-suite split on `main`:

- `tests/test_phase7_atomic_hot_reload.py`
- `tests/test_phase7_regressions.py`
- `tests/test_mcp_contract_v2.py`
- `tests/test_operation_tasks.py` only when public operation integrity is affected

Fault cases cover:

- receipt and operation exist before candidate construction
- candidate failure before effect
- successful hot reload
- explicit restart fallback
- lost IPC response reconciled from active generation/process identity
- rollback classification
- in-flight request completion and typed drain rejection
- stale client rediscovery lag
- continuation reference survives connector replacement

Only exact pytest nodes, formatter, schema generation, release-contract validation, Ruff, and strict Mypy are required locally. Full-suite and CI monitoring are excluded per user instruction.

## Non-goals

- Keeping an incompatible runtime alive indefinitely.
- Introducing a second request router or tracing subsystem.
- Treating reconnect or client cache invalidation as authorization.
- Executing arbitrary continuation payloads from activation receipts.
- Weakening generation-scoped request pinning or restrictive fail-closed policy.
