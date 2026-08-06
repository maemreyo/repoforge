# Managed CodeGraph Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, managed CodeGraph semantic provider that augments the existing baseline without weakening verification or changing disabled behavior.

**Architecture:** Keep `Fallback(TreeSitter, Syntax)` as the baseline and wrap it only when one repository explicitly selects a valid CodeGraph enrollment. The managed adapter projects allowed snapshot files outside the worktree, invokes the pinned one-shot CLI through the existing contained command executor, normalizes bounded JSON into provider-neutral evidence, and requires a matching semantic-canary promotion receipt before graph evidence can be current.

**Tech Stack:** Python 3.12, frozen dataclasses, strict TOML parsing, existing `CommandExecutor`, existing path policy and workspace lifecycle, pytest, Ruff, strict mypy.

## Global Constraints

- Disabled mode must return the existing baseline object unchanged.
- The provider composition is augmentation, never another fallback layer.
- Projection and index state live under `<state_root>/providers/codegraph/workspaces/<workspace_id>` and never inside the worktree.
- Use one-shot `version`, `init`, `sync`, `status --json`, `affected --json`, `query --json`, `callers --json`, `callees --json`, and `impact --json`; never daemon, watcher, MCP, installer, telemetry, upgrade, or download commands.
- Reuse the reviewed executable digest, exact-version check, subprocess containment, cancellation, egress, locks, and cleanup machinery.
- A failed sync invalidates the index and forces the next attempt to perform a full rebuild.
- Semantic graph uncertainty may widen verification only; it may not authorize operations, remove baseline candidates, or reduce gates.
- Every Python file remains at or below 400 lines.
- Standard tests use a deterministic fake CodeGraph executable and never download a real binary.

---

### Task 1: Provider-neutral semantic graph contract

**Files:**
- Create: `src/repoforge/domain/code_intelligence_model.py`
- Modify: `src/repoforge/domain/code_intelligence.py`
- Modify: `src/repoforge/application/dto.py`
- Modify: `src/repoforge/adapters/code_intelligence/fallback.py`
- Modify: `src/repoforge/application/code_intelligence.py`
- Create: `tests/test_code_intelligence_semantic_graph.py`
- Modify: `tests/test-groups.toml`
- Modify: `tests/catalog.toml`

**Interfaces:**
- Produces: `CodeRelationshipKind`, `CodeRelationshipFact`, `AffectedPathCandidate`, `SemanticGraphEvidence`, and `CodeIntelligenceResult.semantic_graph`.
- Preserves: `new_code_intelligence_result(...)` remains the only construction path and defaults `semantic_graph=None`.

- [x] **Step 1: Write failing domain and serialization tests**

```python
def test_semantic_graph_contract_normalizes_and_sorts_facts() -> None:
    graph = SemanticGraphEvidence(
        provider_id="codegraph",
        provider_version="1.5.0",
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(100, "Indexed requested paths."),
        confidence=CodeIntelligenceMeasure(100, "Semantic canary receipt matched."),
        relationships=(
            CodeRelationshipFact(CodeRelationshipKind.CALLS, "src/b.py", "b.run", "src/a.py", "a.run", 2, 90),
            CodeRelationshipFact(CodeRelationshipKind.CALLS, "src/a.py", "a.run", "src/b.py", "b.run", 1, 95),
        ),
        affected_paths=(AffectedPathCandidate("tests/test_a.py", "impact depth 2", 90, 2),),
    )
    result = new_code_intelligence_result(..., semantic_graph=graph)
    assert result.semantic_graph is graph
    assert to_data(result)["semantic_graph"]["status"] == "current"
```

```python
def test_baseline_result_serialization_keeps_semantic_graph_absent_value() -> None:
    result = new_code_intelligence_result(...)
    assert result.semantic_graph is None
    assert "semantic_graph" not in to_data(result)
```

- [x] **Step 2: Run focused tests and prove RED**

Run: `pytest tests/test_code_intelligence_semantic_graph.py -q`

Expected: collection fails because semantic graph types and the factory argument do not exist.

- [x] **Step 3: Split the shared model and add immutable bounded graph types**

```python
class CodeRelationshipKind(str, Enum):
    CALLS = "calls"
    IMPORTS = "imports"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    REFERENCES = "references"
    INSTANTIATES = "instantiates"
    OVERRIDES = "overrides"
    ROUTES_TO = "routes_to"

@dataclass(frozen=True, slots=True, order=True)
class CodeRelationshipFact:
    kind: CodeRelationshipKind
    source_path: str
    source_symbol: str
    target_path: str | None
    target_symbol: str
    depth: int
    confidence: int

@dataclass(frozen=True, slots=True, order=True)
class AffectedPathCandidate:
    path: str
    reason: str
    confidence: int
    depth: int | None = None

@dataclass(frozen=True, slots=True)
class SemanticGraphEvidence:
    provider_id: str
    provider_version: str
    status: CodeIntelligenceStatus
    coverage: CodeIntelligenceMeasure
    confidence: CodeIntelligenceMeasure
    relationships: tuple[CodeRelationshipFact, ...] = ()
    affected_paths: tuple[AffectedPathCandidate, ...] = ()
    limitations: tuple[str, ...] = ()
    truncated: bool = False
```

Move `CodeIntelligenceStatus`, `CodeIntelligenceMeasure`, and `AffectedTestCandidate` into `code_intelligence_model.py`, re-export them from `code_intelligence.py`, and keep `code_intelligence.py` below 400 lines. Add `semantic_graph: SemanticGraphEvidence | None = field(default=None, metadata={"omit_if_none": True})` to `CodeIntelligenceResult` and add the matching factory argument.

- [x] **Step 4: Preserve disabled serialization byte-for-byte**

Change `application.dto.to_data` to iterate dataclass fields and skip only fields explicitly marked `omit_if_none` whose value is `None`. All existing dataclass fields continue to serialize exactly as before.

- [x] **Step 5: Preserve semantic graph through result-copy helpers**

Pass `semantic_graph=result.semantic_graph` in `fallback._with_limitation` and `application.code_intelligence._with_listing_limitation`.

- [x] **Step 6: Run focused tests and prove GREEN**

Run: `pytest tests/test_code_intelligence_semantic_graph.py tests/test_code_intelligence.py -q`

Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add src/repoforge/domain/code_intelligence_model.py src/repoforge/domain/code_intelligence.py src/repoforge/application/dto.py src/repoforge/adapters/code_intelligence/fallback.py src/repoforge/application/code_intelligence.py tests/test_code_intelligence_semantic_graph.py tests/test-groups.toml tests/catalog.toml docs/superpowers/plans/2026-08-06-managed-codegraph-provider.md
git commit -m "feat(code-intelligence): add semantic graph contract"
```

### Task 2: CodeGraph enrollment and repository opt-in

**Files:**
- Create: `src/repoforge/adapters/codegraph/__init__.py`
- Create: `src/repoforge/adapters/codegraph/config.py`
- Create: `src/repoforge/domain/codegraph_config.py`
- Modify: `src/repoforge/domain/provider_manifest.py`
- Modify: `src/repoforge/domain/provider_config.py`
- Modify: `src/repoforge/config.py`
- Modify: `src/repoforge/application/configuration/document.py`
- Test: `tests/test_provider_config.py`
- Test: `tests/test_provider_manifest.py`

**Interfaces:**
- Produces: `CodeGraphOptions`, `ProviderManifest.codegraph`, `RepositoryConfig.code_intelligence_provider_id`.
- Consumes: existing provider manifest runtime digest, version, capability, filesystem, network, and output bounds.

`CodeGraphOptions` and its strict parser are defined in the domain module so `ProviderManifest` and the existing provider-config parser do not import an adapter. The adapter config module re-exports the reviewed surface for later CodeGraph runtime modules.

- [x] **Step 1: Write failing parser and validation tests**

Test exact defaults and ranges, reject `[providers.codegraph]` unless kind is `analyzer`, runtime is executable, capability includes `semantic_graph`, network is `none`, and filesystem capability is `managed_state_write`. Test repository default `""` and exact provider-id parsing.

- [x] **Step 2: Run focused tests and prove RED**

Run: `pytest tests/test_provider_config.py tests/test_provider_manifest.py -q`

Expected: FAIL because nested CodeGraph options are not parsed.

- [x] **Step 3: Implement bounded options**

```python
@dataclass(frozen=True, slots=True)
class CodeGraphOptions:
    init_timeout_seconds: int = 120
    sync_timeout_seconds: int = 60
    query_timeout_seconds: int = 15
    max_changed_paths: int = 256
    max_relationships: int = 1000
    max_affected_paths: int = 1000
    max_depth: int = 5
    projection_max_files: int = 20_000
    projection_max_bytes: int = 250_000_000
    canary_timeout_seconds: int = 180
```

Expose a canonical `options_digest` computed from sorted JSON.

- [x] **Step 4: Parse and bind one enrollment**

Extend `ProviderManifest` with `codegraph: CodeGraphOptions | None`; include its digest in `manifest_hash`. Add `RepositoryConfig.code_intelligence_provider_id: str = ""` and parse it without environment expansion.

- [x] **Step 5: Preserve nested tables in resolved config**

Ensure `render_resolved` recursively emits `[providers.codegraph]` and does not flatten or drop it.

- [x] **Step 6: Run focused tests and commit**

Run: `pytest tests/test_provider_config.py tests/test_provider_manifest.py -q`

Commit: `feat(codegraph): add reviewed enrollment configuration`

Evidence: focused RED failed on the absent adapter/config binding; focused GREEN passed 48 tests, the broader provider/config regression set passed 72 tests, and the fail-closed `test` profile completed successfully.

### Task 3: Snapshot projection and invalidation

**Files:**
- Create: `src/repoforge/adapters/codegraph/projection.py`
- Create: `src/repoforge/adapters/codegraph/manifest.py`
- Test: `tests/test_codegraph_projection.py`

**Interfaces:**
- Produces: `ProjectionManifest`, `ProjectionResult`, `CodeGraphProjection.prepare(request, options)`, `mark_complete(workspace_id, manifest_digest)`, and `invalidate(workspace_id)`.
- Consumes: `CodeIntelligenceRequest`, repository-relative allowed paths, state root, and lock manager.

- [x] **Step 1: Write failing projection tests**

Cover allowed regular files, denied paths, symlinks, oversized files, file/byte budgets, deleted files, deterministic manifest digest, atomic incomplete marker, unchanged worktree Git status, and failed-sync invalidation.

- [x] **Step 2: Run tests and prove RED**

Run: `pytest tests/test_codegraph_projection.py -q`

- [x] **Step 3: Implement external managed layout**

```text
<state_root>/providers/codegraph/workspaces/<workspace_id>/
  source/
  home/
  projection.json
  INCOMPLETE
```

Materialize through sibling temporary files plus `os.replace`, never follow symlinks, and reject any resolved path escaping `workspace_root`.

- [x] **Step 4: Implement manifest identity and invalidation**

The manifest records snapshot id, selection-policy digest, options digest, ordered path/content digests, file/byte totals, and adapter schema version. Create `INCOMPLETE` before mutation; `prepare()` publishes only the source manifest and leaves the marker in place. Only `mark_complete()` with the exact manifest digest, called after validated CodeGraph status, removes it. Any failure removes the published manifest and leaves `INCOMPLETE`; `invalidate()` also removes the managed index to force a full rebuild.

- [x] **Step 5: Run tests and commit**

Run: `pytest tests/test_codegraph_projection.py -q`

Commit: `feat(codegraph): add managed snapshot projection`

Evidence: initial RED failed on absent projection modules; follow-up RED cycles caught denied-path overlap, managed-state/index symlinks, unknown manifest fields, premature completion, and missing policy binding. Focused GREEN passed 61 projection/provider tests with Ruff, strict mypy, catalog ownership, and every new file below 400 lines.

### Task 4: Contained one-shot command boundary

**Files:**
- Create: `src/repoforge/adapters/codegraph/command.py`
- Create: `src/repoforge/adapters/codegraph/command_contract.py`
- Create: `tests/fixtures/fake_codegraph.py`
- Test: `tests/test_codegraph_command.py`
- Test: `tests/test_codegraph_command_subprocess.py`

**Interfaces:**
- Produces: `CodeGraphCommandRunner.version/init/sync/status/affected/query/callers/callees/impact` and the pure validation/environment contract used by that runner.
- Consumes: verified absolute executable, `CommandExecutor.run_isolated`, CodeGraph options, projection root, cancellation token.

- [x] **Step 1: Write fake-binary fault matrix tests**

Cover exact argv, exact environment, digest/version mismatch, timeout, cancellation, descendant cleanup, malformed UTF-8 replacement, stdout/stderr truncation, non-zero exit, stale lock, forbidden command rejection, and symlinks in any managed-root path component.

- [x] **Step 2: Run tests and prove RED**

Run: `pytest tests/test_codegraph_command.py tests/test_codegraph_command_subprocess.py -q`

- [x] **Step 3: Implement minimal managed environment**

```python
{
    "CODEGRAPH_DIR": ".index",
    "CODEGRAPH_NO_DAEMON": "1",
    "CODEGRAPH_NO_DOWNLOAD": "1",
    "CODEGRAPH_NO_UPDATE_CHECK": "1",
    "CODEGRAPH_TELEMETRY": "0",
    "DO_NOT_TRACK": "1",
    "HOME": str(home),
    "XDG_CONFIG_HOME": str(home / "config"),
    "XDG_CACHE_HOME": str(home / "cache"),
    "XDG_DATA_HOME": str(home / "data"),
    "LC_ALL": "C.UTF-8",
}
```

Invoke only the allowlisted command names with argv sequences and `run_isolated`.

- [x] **Step 4: Verify identity before semantic use**

Require registry availability, an absolute resolved executable, matching SHA-256, exact manifest-version output, and managed projection/home paths with no symlink component.

- [x] **Step 5: Run tests and commit**

Run: `pytest tests/test_codegraph_command.py tests/test_codegraph_command_subprocess.py -q`

Commit: `feat(codegraph): add contained CLI boundary`

Evidence: fake-provider RED established the absent boundary and later exposed parent-component symlink traversal. GREEN passed 32 focused command/subprocess/registry tests; Ruff, strict mypy, test-group completeness, shadow catalog, and the 400-line module budget all passed.

### Task 5: Strict normalization and managed provider

**Files:**
- Create: `src/repoforge/adapters/codegraph/normalize.py`
- Create: `src/repoforge/adapters/codegraph/provider.py`
- Test: `tests/test_codegraph_normalize.py`
- Test: `tests/test_codegraph_provider.py`

**Interfaces:**
- Produces: strict `normalize_status`, `normalize_affected`, `normalize_relationships`, and `ManagedCodeGraphProvider.analyze`.
- Consumes: projection result, command runner, baseline request symbols and changed paths.

- [ ] **Step 1: Write failing fixture tests**

Cover duplicate JSON keys, trailing data, unknown relationship kinds, absolute/`./`/escaping paths, excessive nesting, duplicate facts, deterministic sorting, fan-out bounds, partial results, and no raw provider text in returned evidence.

- [ ] **Step 2: Run tests and prove RED**

Run: `pytest tests/test_codegraph_normalize.py tests/test_codegraph_provider.py -q`

- [ ] **Step 3: Implement strict JSON decoding**

Use `json.JSONDecoder(object_pairs_hook=...)`, reject duplicate keys and trailing data, walk the decoded tree with explicit depth/collection limits, and validate every path through the same normalized relative-path contract.

- [ ] **Step 4: Implement bounded query strategy**

Run status, affected paths, then seeded query/callers/callees/impact only for baseline symbols in requested or changed paths. Enforce changed-path, symbol, relationship, affected-path, aggregate-byte, depth, and total-wall-time bounds. Reaching a bound yields `partial` evidence with a limitation.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/test_codegraph_normalize.py tests/test_codegraph_provider.py -q`

Commit: `feat(codegraph): normalize semantic graph evidence`

### Task 6: Baseline-preserving augmentation and routing

**Files:**
- Create: `src/repoforge/adapters/codegraph/augment.py`
- Modify: `src/repoforge/adapters/code_intelligence/__init__.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `src/repoforge/application/workspace/assessment.py`
- Modify: `src/repoforge/application/workspace/verify.py`
- Test: `tests/test_codegraph_augment.py`
- Test: `tests/test_workspace_assessment.py`
- Test: `tests/test_workspace_verify.py`

**Interfaces:**
- Produces: `CodeGraphAugmentedProvider(base, graph)`.
- Preserves: baseline result identity in disabled mode; all baseline facts in enabled failure modes.

- [ ] **Step 1: Write failing augmentation tests**

Prove disabled returns `base_result is result`; current graph is additive; unavailable, partial, stale, malformed, and exception cases retain all baseline fields; graph affected tests only union after path and diagnostic mapping; uncertainty forces final-profile fallback.

- [ ] **Step 2: Run tests and prove RED**

Run: `pytest tests/test_codegraph_augment.py tests/test_workspace_assessment.py tests/test_workspace_verify.py -q`

- [ ] **Step 3: Implement deterministic merge**

The augmenter evaluates base first. If graph is disabled, return the same object. Otherwise attach `semantic_graph`, union validated affected tests, retain baseline status/coverage/confidence, and append typed widening limitations without lowering baseline measures.

- [ ] **Step 4: Wire per-repository selection**

Resolve `RepositoryConfig.code_intelligence_provider_id` against the registry. Missing or invalid enrollment constructs the baseline plus unavailable graph evidence; empty selection constructs the exact old baseline only.

- [ ] **Step 5: Widen verification explicitly**

Assessment serializes semantic status and a widening reason. `_auto_target` may use graph-added candidates only when baseline evidence and semantic graph are current, complete, non-truncated, and above the existing confidence threshold; otherwise it selects the final profile.

- [ ] **Step 6: Run tests and commit**

Run: `pytest tests/test_codegraph_augment.py tests/test_workspace_assessment.py tests/test_workspace_verify.py -q`

Commit: `feat(codegraph): augment baseline and widen verification`

### Task 7: Semantic canaries, receipts, calibration, and lifecycle

**Files:**
- Create: `src/repoforge/adapters/codegraph/canaries.py`
- Create: `src/repoforge/adapters/codegraph/receipts.py`
- Create: `src/repoforge/adapters/codegraph/lifecycle.py`
- Create: `tests/fixtures/codegraph_canary/**`
- Modify: `src/repoforge/adapters/code_intelligence/calibration-v1.json`
- Modify: workspace disposal/startup cleanup integration points discovered in implementation
- Test: `tests/test_codegraph_canaries.py`
- Test: `tests/test_codegraph_lifecycle.py`

**Interfaces:**
- Produces: `PromotionIdentity`, `PromotionReceipt`, `CodeGraphCanaryRunner.ensure_promoted`, and cleanup hooks.
- Consumes: executable/version/platform/manifest/options/schema/corpus digests and the managed command/projection layers.

- [ ] **Step 1: Write failing canary tests**

Cover required and forbidden edges, affected-test recall, deterministic clean reruns, schema drift, unsupported file handling, incremental deletion, timeout/cancellation cleanup, denied file exclusion, unchanged worktree status, and receipt invalidation for every promotion identity field.

- [ ] **Step 2: Run tests and prove RED**

Run: `pytest tests/test_codegraph_canaries.py tests/test_codegraph_lifecycle.py -q`

- [ ] **Step 3: Implement canonical receipt identity**

Hash exact executable digest/version, platform/architecture, manifest hash, options digest, adapter schema version, and canary corpus digest. Persist only canonical identities, bounded metrics, gate outcomes, and timestamps.

- [ ] **Step 4: Gate provider use and lifecycle cleanup**

Before graph evidence can be current, require a matching successful receipt or run canaries within `canary_timeout_seconds`. Cleanup incomplete temporary projections on startup and remove workspace provider state only after contained commands stop. Never inspect or delete a worktree `.codegraph` directory.

- [ ] **Step 5: Add calibration evidence and commit**

Add a `codegraph` calibration entry backed by canary affected-test recall.

Run: `pytest tests/test_codegraph_canaries.py tests/test_codegraph_lifecycle.py -q`

Commit: `feat(codegraph): gate promotion with semantic canaries`

### Task 8: Operator surfaces, documentation, and release gates

**Files:**
- Modify: config examples and `rf init` templates located by repository search
- Modify: doctor/show-config modules located by repository search
- Modify: `CHANGELOG.md`
- Modify: operator documentation under `docs/`
- Test: corresponding init/doctor/show-config tests

**Interfaces:**
- Documents: enrollment fields, external state layout, receipt identity, failure behavior, rollback, and no-daemon/no-download guarantees.

- [ ] **Step 1: Add failing operator-surface tests**

Prove init examples include disabled default and reviewed nested provider options; doctor reports executable/version/receipt validity without raw environment/provider output; show-config emits only secret-free values and digests.

- [ ] **Step 2: Implement docs and operator surfaces**

Update changelog and architecture/operator docs. State that no MCP tool is added and disabling `code_intelligence_provider_id` restores exact baseline construction.

- [ ] **Step 3: Run affected and repository gates**

Run in order:

```bash
pytest tests/test_code_intelligence.py tests/test_provider_config.py tests/test_provider_manifest.py tests/test_codegraph_projection.py tests/test_codegraph_command.py tests/test_codegraph_normalize.py tests/test_codegraph_provider.py tests/test_codegraph_augment.py tests/test_codegraph_canaries.py tests/test_codegraph_lifecycle.py tests/test_workspace_assessment.py tests/test_workspace_verify.py -q
ruff format --check src tests
ruff check src tests
mypy --strict src
make test
./scripts/test-all.sh
```

The separately marked real-binary canary gate must run on supported release platforms with the pinned artifact already provisioned; it must never self-download.

- [ ] **Step 4: Final commit**

```bash
git add CHANGELOG.md docs src tests
git commit -m "docs(codegraph): document managed semantic provider"
```

## Plan self-review

- Every design-spec implementation slice maps to one task above.
- Disabled baseline compatibility is tested in Tasks 1 and 6.
- Projection atomicity, failed-sync invalidation, process containment, strict normalization, canary promotion, upgrade invalidation, verification widening, cleanup, operator surfaces, docs, and release gates each have an explicit task and focused test command.
- New production modules are split by responsibility to remain below the repository 400-line limit.
