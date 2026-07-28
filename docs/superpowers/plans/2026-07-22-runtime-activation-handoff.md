# Runtime Activation Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make runtime activation receipt-backed, drain-aware, reconnect-safe, and deterministically reconcilable after lost connector responses.

**Architecture:** Add a dedicated activation domain/store and a narrow `RuntimeActivationJournal` that owns `OperationTask` plus activation-receipt transitions without depending on `ApplicationContext`. Wire it through `GenerationActivator`, `HotReloadCoordinator`, runtime CLI composition, and the existing router/gate. Reuse current request pinning and contract handshake; add only typed reconnect evidence and continuation references.

**Tech Stack:** Python 3.12, frozen dataclasses, `JsonStateRepository`, `OperationStore`, Pydantic v2 contracts, MCP structured errors, pytest, Ruff, strict Mypy.

## Global Constraints

- Keep the public MCP roster at exactly 28 tools.
- Create operation and activation receipt before contract validation or candidate construction.
- Persist no raw config, argv, paths, environment values, tokens, or credentials.
- Existing admitted requests complete on their pinned generation.
- New requests never observe a partially built or partially committed generation.
- Do not run the full test suite or monitor CI; use exact nodes and quick/static gates only.
- Do not modify test-splitting files from `main`.

---

### Task 1: Activation receipt domain, store, and journal

**Files:**
- Create: `src/repoforge/domain/runtime_activation.py`
- Create: `src/repoforge/ports/runtime_activation_store.py`
- Create: `src/repoforge/adapters/persistence/json_runtime_activation_store.py`
- Create: `src/repoforge/application/runtime/activation_journal.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_phase7_atomic_hot_reload.py`

**Interfaces:**
- Produces `RuntimeActivationIdentity`, `RuntimeActivationClassification`, `RuntimeActivationReceipt`, `RuntimeActivationStore`, and `RuntimeActivationJournal`.
- Receipt IDs use `receipt-<24hex>` so `OperationTask.receipt_id` remains valid.
- Result references use `runtime-activation:<receipt_id>` and satisfy `_SAFE_ID`.

- [ ] **Step 1: Write the failing domain/store test**

Append an exact test proving the journal creates both records before running a supplied candidate callback:

```python
def test_activation_journal_persists_operation_and_receipt_before_candidate(tmp_path: Path) -> None:
    journal = _activation_journal(tmp_path, ids=("a" * 24, "b" * 24))
    observed: list[tuple[str, str]] = []

    attempt = journal.begin(
        target=_accepted_identity(2),
        previous=_runtime_identity(1),
        continuation_reference="issue-publication:42",
    )
    observed.append((attempt.operation.operation_id, attempt.receipt.value.receipt_id))

    assert observed == [("op-" + "a" * 24, "receipt-" + "b" * 24)]
    assert attempt.operation.phase == "accepted"
    assert attempt.receipt.value.classification.value == "accepted"
```

- [ ] **Step 2: Run the exact node and verify RED**

Run:

```text
pytest tests/test_phase7_atomic_hot_reload.py::test_activation_journal_persists_operation_and_receipt_before_candidate -q
```

Expected: FAIL because the activation domain/journal does not exist.

- [ ] **Step 3: Implement the domain model**

Create the following bounded types:

```python
class RuntimeActivationClassification(str, Enum):
    ACCEPTED = "accepted"
    BUILDING = "building"
    HOT_RELOAD = "hot_reload"
    RESTART_FALLBACK = "restart_fallback"
    RELOAD_FAILED = "reload_failed"
    ACTIVE_BUT_CLIENT_STALE = "active_but_client_stale"
    ROLLED_BACK = "rolled_back"

@dataclass(frozen=True, slots=True)
class RuntimeActivationIdentity:
    config_generation: int
    source_sha256: str
    resolved_sha256: str
    runtime_active_generation: int | None
    process_identity: str | None
    tool_surface_hash: str | None
    runtime_phase: str

@dataclass(frozen=True, slots=True)
class RuntimeActivationReceipt:
    receipt_id: str
    operation_id: str
    classification: RuntimeActivationClassification
    target_generation: int
    accepted_identity: RuntimeActivationIdentity
    previous_identity: RuntimeActivationIdentity | None
    active_identity: RuntimeActivationIdentity | None
    continuation_reference: str | None
    correlation_id: str
    effect_boundary_crossed: bool
    accepted_at: str
    updated_at: str
    error_code: str | None = None
    error_message: str | None = None
    schema_version: int = 1
```

Add pure constructors, transition validation, canonical payload encoding/decoding, timestamp validation, SHA/ID bounds, redaction checks, and legal transition sets.

- [ ] **Step 4: Implement persistence and journal**

`JsonRuntimeActivationStore` wraps `JsonStateRepository` with collection `runtime-activations`, a `receipt-<24hex>` validator, a 256 KiB record bound, CAS save, read, and bounded list.

`RuntimeActivationJournal.begin()` must:

1. allocate `op-*` and `receipt-*` IDs;
2. create `OperationTask(kind="runtime_activation", phase="accepted", cancel_supported=False, snapshot_binding=OperationSnapshotBinding(config_generation=target_generation))`;
3. create the activation receipt;
4. if receipt creation fails, transition the operation to failed-before-effect;
5. return both persisted envelopes.

Expose transition methods `mark_building`, `mark_effect`, `complete`, and `fail` that CAS both stores and never claim success without a result reference.

- [ ] **Step 5: Run exact domain/store tests GREEN**

Run the new creation, CAS conflict, malformed receipt, and secret-redaction nodes individually. Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```text
git commit -m "feat(runtime): add durable activation journal (#245)"
```

---

### Task 2: Receipt-backed hot reload, restart fallback, and lost-response reconciliation

**Files:**
- Modify: `src/repoforge/application/runtime/activation.py`
- Modify: `src/repoforge/application/runtime/hot_reload.py`
- Modify: `src/repoforge/interfaces/runtime/host.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_phase7_atomic_hot_reload.py`

**Interfaces:**
- `GenerationActivator` receives `activation_journal: RuntimeActivationJournal`.
- `ActivationResult` adds `operation_id`, `activation_receipt_id`, `classification`, `previous_identity`, `accepted_identity`, `active_identity`, and `continuation_reference`.
- `activate(..., continuation_reference: str | None = None)` remains backward compatible.

- [ ] **Step 1: Write RED tests for ordering and lost response**

Add exact tests proving:

- receipt/operation exist when contract validation or candidate build fails;
- a hot reload response lost after `store.activate()` returns `active_but_client_stale` on reconciliation without a second swap;
- an incompatible generation classifies the supervisor path `restart_fallback`;
- successful rollback classifies `rolled_back` and records the restored identity.

- [ ] **Step 2: Run each RED node**

Expected failures: missing result fields/journal injection and duplicate application after lost response.

- [ ] **Step 3: Wire the journal into `GenerationActivator.activate()`**

At the first executable line after argument validation:

```python
attempt = self._activation_journal.begin(
    target=identity_from_generation(generation),
    previous=identity_from_runtime(previous, running),
    continuation_reference=continuation_reference,
)
```

Then:

- mark `building` before contract/candidate probes;
- finalize `reload_failed` on pre-effect exceptions;
- mark the effect boundary immediately before durable active-generation commit/router swap or before supervisor shutdown/start changes externally observable runtime ownership;
- finalize each existing return path with the exact classification and identity;
- preserve current restrictive fail-closed semantics.

- [ ] **Step 4: Add deterministic reconciliation**

Before a new effect, locate a non-terminal receipt matching target generation and resolved digest. If config store, runtime record, and router all prove the target active, finalize `active_but_client_stale` and return the existing operation/receipt IDs. If evidence conflicts, fail closed with manual guidance; do not repeat the candidate effect blindly.

- [ ] **Step 5: Wire all production constructors**

Add `build_runtime_activation_store()` and `build_runtime_activation_journal()` in `bootstrap.py`. Inject one journal into `_activate()`, `rf runtime reload/restart`, and in-process `reload_in_process()` composition.

- [ ] **Step 6: Verify exact hot-reload/fallback/reconciliation nodes GREEN**

Run only the new nodes plus existing:

```text
tests/test_phase7_atomic_hot_reload.py::test_generation_activator_uses_atomic_hot_reload_for_compatible_generation
tests/test_phase7_atomic_hot_reload.py::test_generation_activator_rejects_tampered_contract_before_any_runtime_effect
```

- [ ] **Step 7: Commit Task 2**

```text
git commit -m "feat(runtime): reconcile activation outcomes (#245)"
```

---

### Task 3: Drain-aware typed reconnect contract

**Files:**
- Modify: `src/repoforge/domain/errors.py`
- Modify: `src/repoforge/ports/operation_gate.py`
- Modify: `src/repoforge/adapters/runtime/operation_gate.py`
- Modify: `src/repoforge/application/runtime/hot_reload.py`
- Modify: `src/repoforge/contracts/common.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify generated contract artifacts through the reviewed schema generator
- Test: `tests/test_phase7_atomic_hot_reload.py`
- Test: `tests/test_mcp_contract_v2.py`

**Interfaces:**
- Add `ErrorCode.RECONNECT_REQUIRED`.
- Extend `OperationGate.begin_drain(..., reconnect_details: Mapping[str, object] | None = None)`.
- Closed error fields include activation operation/receipt IDs, generation, contract digests, tool-surface hash, process-start identity, and rediscovery action.

- [ ] **Step 1: Write RED concurrency and MCP tests**

Prove that:

- an already admitted request completes on generation N while N+1 is committed;
- a request attempting admission during reconnect drain receives `RECONNECT_REQUIRED`;
- the structured MCP envelope contains exact bounded new identity and validates against the existing tool output union;
- after swap, new requests observe only N+1;
- stale rediscovery still returns `CLIENT_CONTRACT_STALE` before service mutation.

- [ ] **Step 2: Implement typed reconnect gate state**

Store an immutable sanitized reconnect-details tuple in `InProcessOperationGate`. During `DRAINING`, raise `RepoForgeError` directly:

```python
raise RepoForgeError(
    "RECONNECT_REQUIRED: runtime generation changed",
    code=ErrorCode.RECONNECT_REQUIRED,
    retryable=False,
    safe_next_action="Reconnect, rediscover the active contract, then resume the durable operation.",
    unchanged_state=("The rejected request was not admitted to either generation.",),
    details=reconnect_details,
)
```

Clear reconnect details on `reopen()`.

- [ ] **Step 3: Supply exact identity before router commit**

Build reconnect details from the candidate contract identity and activation attempt, begin drain, wait for already admitted operations, then call `commit_swap`. Do not expose the candidate before all probes pass.

- [ ] **Step 4: Extend closed public error contract and regenerate schemas**

Whitelist only the exact typed fields in `ToolErrorDetails` and `_raise_structured_error`. Run the reviewed `schemas` profile and `release-contract-diff`; verify 28 tools.

- [ ] **Step 5: Run exact concurrency/MCP tests GREEN**

Also rerun existing router pinning and contract-handshake nodes.

- [ ] **Step 6: Commit Task 3**

```text
git commit -m "feat(runtime): return typed reconnect handoff (#245)"
```

---

### Task 4: Durable continuation reference and startup reconciliation

**Files:**
- Modify: `src/repoforge/application/runtime/activation.py`
- Modify: `src/repoforge/application/runtime/activation_journal.py`
- Modify: `src/repoforge/application/outcome_reconciliation.py`
- Modify: `src/repoforge/interfaces/cli/main.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_phase7_atomic_hot_reload.py`
- Test: `tests/test_outcome_records.py`

**Interfaces:**
- `continuation_reference` is a bounded opaque ID, not executable data.
- Terminal activation result returns the same reference.
- Startup reconciliation resumes through existing durable operation/outcome reconciliation only.

- [ ] **Step 1: Write RED continuation tests**

Create a pending issue/config operation, activate with its continuation reference, simulate connector replacement/lost response, rebuild composition, and assert reconciliation finds the same pending operation and does not duplicate publication.

- [ ] **Step 2: Persist and validate the reference**

Allow only the existing safe ID grammar and 256-character bound. Reject paths, whitespace/control characters, URLs, and embedded payloads. Persist it from acceptance through every terminal classification.

- [ ] **Step 3: Add activation reconciliation hook**

At startup, reconcile non-terminal activation receipts before generic outcome reconciliation. Return a bounded report containing scanned, activated, failed-before-effect, unknown, and continuation-resumable counts. Then run existing outcome reconciliation so the referenced workflow resumes from its durable operation/receipt.

- [ ] **Step 4: Verify no duplicate effect**

Run exact issue/config continuation tests and existing outcome receipt restart tests. Expected: one authoritative publication/effect and one terminal activation classification.

- [ ] **Step 5: Commit Task 4**

```text
git commit -m "feat(runtime): resume workflows after activation (#245)"
```

---

### Task 5: Final focused verification and issue evidence

**Files:**
- Modify generated contract artifacts only through reviewed generators
- No production behavior changes

- [ ] **Step 1: Format changed paths**

Run the reviewed formatter on server-derived changed paths.

- [ ] **Step 2: Regenerate and verify contracts**

Run `schemas` and `release-contract-diff`. Expected: exactly 28 MCP tools.

- [ ] **Step 3: Run focused runtime tests**

Run exact new nodes plus at most these directly related modules:

```text
tests/test_phase7_atomic_hot_reload.py
tests/test_phase7_regressions.py
```

Do not run the full suite.

- [ ] **Step 4: Run quick static gate**

Expected:

- Ruff format/check PASS
- strict Mypy PASS

- [ ] **Step 5: Review diff and commit any generated-only finalization**

No unrelated refactor, CI/test-splitting files, or 29th tool.

- [ ] **Step 6: Push non-force with remote-head lock**

Do not monitor CI. Add implementation evidence to issue #245 while leaving bookkeeping state consistent with declared blockers until merge/reconciliation.
