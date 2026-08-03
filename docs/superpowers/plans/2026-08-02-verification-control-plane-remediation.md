# Verification Control Plane Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace RepoForge's duplicated, full-suite-first verification loop with explicit affected/full/coverage/gate intents, fail-closed scheduling metadata, minimal explicit-mode preflight, a declarative shadow test catalog, bounded performance evidence, and one canonical CI coverage run.

**Architecture:** Preserve the existing selector and production gate as migration backstops while introducing small, testable boundaries: command intent routing, fail-closed lane loading, immutable workspace snapshot preflight, catalog validation/planning, and performance artifact emission. Cut over CI first to one canonical coverage observation and no-coverage compatibility shards; keep catalog planning in shadow mode until comparison evidence proves no false negatives.

**Tech Stack:** Python 3.10+, pytest/pytest-xdist/pytest-cov, TOML via `tomli`, POSIX shell, GNU Make, GitHub Actions, RepoForge durable operation and ports/adapters architecture.

## Global Constraints

- Preserve the branch coverage floor at 80.
- Preserve fail-closed workspace identity, durable operation, activation, package, and release-effect boundaries.
- Unknown test blast radius widens deliberately and records the reason.
- Invalid scheduling metadata must never make more tests parallel; fail before execution or use an explicit all-exclusive fallback.
- Explicit `profile`, `diagnostic`, and `adhoc` verification must not collect code intelligence, PR state, CI checks, or risk recommendations.
- Plan and auto modes retain rich assessment until the snapshot-token refactor is proven.
- Catalog planning remains shadow-only in this implementation; the existing selector remains execution authority.
- Keep new Python modules below 400 lines and avoid enlarging existing over-limit modules unless the change directly removes responsibility from them.
- Every task follows RED → GREEN → REFACTOR and ends in a focused commit.

---

## File Structure

- `scripts/verification_artifact.py`: bounded, deterministic JSON evidence schema and writer shared by test runners.
- `scripts/test_catalog.py`: catalog models, validation, canonical digest, deterministic shadow-plan compilation, and comparison.
- `tests/catalog.toml`: declarative capability/test ownership, isolation, resource, platform, Python, and cost metadata.
- `src/repoforge/domain/workspace_snapshot.py`: immutable `SnapshotToken` and validation.
- `src/repoforge/application/workspace/snapshot.py`: lock-scoped snapshot capture and cheap-currentness checks.
- `scripts/run_compatibility_tests.py`: stable no-coverage compatibility selectors for non-canonical Python/OS cells.
- Existing Make, shell, selector, runner, verification, workflow, configuration, and documentation files are modified only for the responsibility they already own.

---

### Task 1: Correct Command Intents and Fail-Closed Full-Suite Lanes

**Files:**
- Modify: `Makefile:14-121`
- Modify: `scripts/verify-change.sh:1-59`
- Modify: `scripts/run_test_suite.py:1-183`
- Modify: `tests/test_parallel_test_runner.py`
- Modify: `tests/test_docs_command_drift.py:114-126,213-227`
- Modify: `docs/development/DEVELOPMENT.md`

**Interfaces:**
- Produces: `scripts.run_test_suite.load_lane_plan(root: Path) -> LanePlan`
- Produces: `scripts.run_test_suite.run(root: Path, *, coverage_dir: Path | None, workers: int) -> int`
- Command contracts: `make test`, `make test-full`, `make coverage`, `make verify`, `make gate`

- [ ] **Step 1: Write failing command-contract and lane-metadata tests**

Add tests asserting:

```python
def test_make_exposes_distinct_affected_full_coverage_and_gate_intents() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^test:\s", makefile, re.MULTILINE)
    assert "select_affected_tests.py --run" in makefile
    assert re.search(r"^test-full:\s", makefile, re.MULTILINE)
    assert "select_affected_tests.py --full --run" in makefile
    assert re.search(r"^coverage:\s", makefile, re.MULTILINE)
    assert "run_test_suite.py --coverage-dir" in makefile
    assert re.search(r"^gate:\s", makefile, re.MULTILINE)
```

Replace the fail-open tests in `test_parallel_test_runner.py` with:

```python
def test_missing_manifest_refuses_to_build_a_parallel_lane(tmp_path: Path) -> None:
    with pytest.raises(runner_module.SelectionMetadataError, match="missing"):
        runner_module.load_lane_plan(tmp_path)


def test_unreadable_manifest_refuses_to_build_a_parallel_lane(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test-groups.toml").write_text("not = [valid", encoding="utf-8")
    with pytest.raises(runner_module.SelectionMetadataError, match="invalid"):
        runner_module.load_lane_plan(tmp_path)
```

- [ ] **Step 2: Run focused tests and prove RED**

Run:

```bash
uv run pytest -q \
  tests/test_parallel_test_runner.py \
  tests/test_docs_command_drift.py::test_make_exposes_distinct_affected_full_coverage_and_gate_intents
```

Expected: FAIL because the new targets and `load_lane_plan`/`SelectionMetadataError` do not exist, and invalid metadata currently returns an empty serial set.

- [ ] **Step 3: Implement command contracts and runner modes**

In `scripts/run_test_suite.py`, introduce:

```python
@dataclass(frozen=True, slots=True)
class LanePlan:
    serial: tuple[Path, ...]
    parallel: tuple[Path, ...]


class SelectionMetadataError(ValueError):
    pass


def load_lane_plan(root: Path) -> LanePlan:
    manifest_path = root / "tests" / "test-groups.toml"
    if not manifest_path.is_file():
        raise SelectionMetadataError(f"selection metadata missing: {manifest_path}")
    try:
        manifest = selector.load_manifest(manifest_path)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise SelectionMetadataError(f"selection metadata invalid: {exc}") from exc
    violations = selector.check_completeness(manifest, root / "tests")
    if violations:
        raise SelectionMetadataError("selection metadata incomplete: " + "; ".join(violations[:10]))
    tests = tuple(sorted((root / "tests").rglob("test_*.py")))
    serial_files = {(root / item).resolve() for item in manifest.serial_files()}
    return LanePlan(
        serial=tuple(path for path in tests if path.resolve() in serial_files),
        parallel=tuple(path for path in tests if path.resolve() not in serial_files),
    )
```

Make coverage optional in `lane_command` and `run`; include pytest-cov flags only when `coverage_dir` is not `None`. Keep branch coverage and floor 80 for coverage mode.

Update Make targets:

```make
test:
	uv run --extra dev python scripts/select_affected_tests.py --run --base "$${REPOFORGE_TEST_AFFECTED_BASE:-main}"

test-full:
	uv run --extra dev python scripts/select_affected_tests.py --full --run

coverage:
	uv run --extra dev python scripts/run_test_suite.py --coverage-dir "$${REPOFORGE_COVERAGE_DIR:-.cache/verification/coverage}"

gate:
	scripts/verify-production.sh
```

Keep `test-fast` as a documented transition alias to `test-full`. Change the `suite` stage in `verify-change.sh` to affected execution and add a `metadata` stage running `--check-completeness` and `--check-map-freshness` against the configured base.

- [ ] **Step 4: Run focused tests and prove GREEN**

Run:

```bash
uv run pytest -q tests/test_parallel_test_runner.py tests/test_docs_command_drift.py
```

Expected: PASS.

- [ ] **Step 5: Run command smoke checks**

Run:

```bash
make -n test
make -n test-full
make -n coverage
make -n gate
uv run python scripts/run_test_suite.py --help
```

Expected: each intent resolves to its documented command; no tests execute under `make -n`.

- [ ] **Step 6: Commit**

```bash
git add Makefile scripts/verify-change.sh scripts/run_test_suite.py \
  tests/test_parallel_test_runner.py tests/test_docs_command_drift.py \
  docs/development/DEVELOPMENT.md
git commit -m "refactor(test): separate verification intents"
```

---

### Task 2: Add Bounded Verification Performance Evidence

**Files:**
- Create: `scripts/verification_artifact.py`
- Modify: `scripts/select_affected_tests.py`
- Modify: `scripts/run_test_suite.py`
- Create: `tests/test_verification_artifact.py`
- Modify: `tests/test_select_affected_tests.py`
- Modify: `tests/test_parallel_test_runner.py`

**Interfaces:**
- Produces: `VerificationArtifact`, `LaneTiming`, `write_artifact(path: Path, artifact: VerificationArtifact) -> None`
- Adds CLI: `--report-path PATH` to affected/full selector and full/coverage runner.

- [ ] **Step 1: Write failing deterministic-artifact tests**

Create tests for:

```python
def test_artifact_is_bounded_and_canonical(tmp_path: Path) -> None:
    artifact = VerificationArtifact(
        schema_version=1,
        intent="affected",
        head_sha="a" * 40,
        selected_count=2,
        escalated=False,
        escalation_reason=None,
        lanes=(LaneTiming("parallel", 2, 125.5, 0),),
    )
    target = tmp_path / "artifact.json"
    write_artifact(target, artifact)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["selected_count"] == 2
    assert target.stat().st_size < 64_000
```

Add selector and runner tests asserting `--report-path` writes a report even when selection widens or a lane fails.

- [ ] **Step 2: Run focused tests and prove RED**

Run:

```bash
uv run pytest -q tests/test_verification_artifact.py \
  tests/test_select_affected_tests.py tests/test_parallel_test_runner.py
```

Expected: FAIL because the artifact module and CLI options do not exist.

- [ ] **Step 3: Implement artifact schema and instrumentation**

Use frozen dataclasses and canonical `json.dumps(..., sort_keys=True, separators=(",", ":"))`. Bound reasons to 2,000 characters, lanes to 32 entries, and output to 64 KB. Capture monotonic lane durations and return codes. Emit reports in `finally` paths so a failed subprocess still leaves evidence.

- [ ] **Step 4: Run focused tests and prove GREEN**

Run the same focused command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/verification_artifact.py scripts/select_affected_tests.py \
  scripts/run_test_suite.py tests/test_verification_artifact.py \
  tests/test_select_affected_tests.py tests/test_parallel_test_runner.py
git commit -m "feat(test): emit bounded verification evidence"
```

---

### Task 3: Introduce Minimal Snapshot Preflight for Explicit Verification

**Files:**
- Create: `src/repoforge/domain/workspace_snapshot.py`
- Create: `src/repoforge/application/workspace/snapshot.py`
- Modify: `src/repoforge/application/workspace/assessment.py`
- Modify: `src/repoforge/application/workspace/verify.py`
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_workspace_assessment.py`
- Modify: `tests/test_durable_verification_dispatch.py`
- Modify: `tests/test_fingerprint_cache.py`
- Modify: `tests/test_root_module_architecture.py` only if the new domain/application modules require an explicit architectural assertion.

**Interfaces:**
- Produces: `SnapshotToken`
- Produces: `WorkspaceSnapshotReader.capture(workspace_id: str, impact_paths: tuple[str, ...] = ()) -> SnapshotToken`
- Produces: `WorkspaceSnapshotReader.assert_current(token: SnapshotToken) -> None`
- Changes explicit verify results to `assessment=None`, `recommendations=[]`, `impact_evidence=None` while preserving head/fingerprint and durable admission.

- [ ] **Step 1: Write failing domain and orchestration tests**

Add tests asserting:

```python
def test_explicit_profile_does_not_collect_rich_assessment(...):
    monkeypatch.setattr(
        forge_env.service._verify._assessment,
        "execute",
        lambda _command: (_ for _ in ()).throw(AssertionError("rich assessment must not run")),
    )
    result = forge_env.service.workspace_verify(
        workspace_id, mode="profile", profile_name="quick", background=True
    )
    assert result["assessment"] is None
    assert result["recommendations"] == []


def test_explicit_diagnostic_uses_at_most_one_full_fingerprint_scan(tmp_path: Path) -> None:
    service, executor = _service_with_counting_executor(tmp_path)
    workspace_id = service.workspace_create("demo", "snapshot-preflight")["workspace_id"]
    executor.full_fingerprint_scans = 0
    service.workspace_verify(
        workspace_id,
        mode="diagnostic",
        diagnostic_id="pytest-target",
        selector="hello.txt",
        background=True,
    )
    assert executor.full_fingerprint_scans <= 1
```

Also test stale expected head/fingerprint refusal before admission.

- [ ] **Step 2: Run focused tests and prove RED**

Run:

```bash
uv run pytest -q \
  tests/test_workspace_assessment.py \
  tests/test_durable_verification_dispatch.py \
  tests/test_fingerprint_cache.py
```

Expected: new explicit-mode tests fail because `_execute` always calls rich assessment and repeatedly fingerprints.

- [ ] **Step 3: Implement `SnapshotToken`**

Define a frozen dataclass containing:

```python
@dataclass(frozen=True, slots=True)
class SnapshotToken:
    token_id: str
    workspace_id: str
    head_sha: str
    workspace_fingerprint: str
    validity_token: str
    changed_paths: tuple[str, ...]
    config_generation: str
    policy_hash: str
    captured_at: str
```

Validate SHA formats, sorted unique allowed paths, safe workspace id, and derive `token_id` from canonical JSON.

- [ ] **Step 4: Implement lock-scoped capture and cheap currentness**

`WorkspaceSnapshotReader.capture` must acquire the workspace lock, call `read_fingerprint(..., persist=True)` once, compute the cheap validity token once, collect changed paths, config identity, and policy identity, then return the token. `assert_current` compares head, validity token, config generation, and policy hash without recomputing the full binary-diff fingerprint.

Extract shared config/policy identity helpers from assessment into the snapshot reader; make rich assessment consume a token rather than owning identity capture.

- [ ] **Step 5: Route explicit modes through minimal preflight**

In `WorkspaceVerifier._execute`:

1. validate mode-specific arguments;
2. for `plan`/`auto`, run rich assessment;
3. for `profile`/`diagnostic`/`adhoc`, capture a `SnapshotToken`, validate expected identity, select a named/default profile directly from repository config when required, enforce profile guards, and admit durable work using token head/fingerprint;
4. return no assessment/recommendations for explicit modes.

Do not change public output schemas because those fields are already nullable/list-valued.

- [ ] **Step 6: Run focused tests and prove GREEN**

Run the same focused command plus:

```bash
uv run pytest -q tests/test_mcp_contract_v2.py tests/test_v2_contract_models.py
```

Expected: PASS and output contract validation remains green.

- [ ] **Step 7: Commit**

```bash
git add src/repoforge/domain/workspace_snapshot.py \
  src/repoforge/application/workspace/snapshot.py \
  src/repoforge/application/workspace/assessment.py \
  src/repoforge/application/workspace/verify.py \
  src/repoforge/application/service.py src/repoforge/bootstrap.py \
  tests/test_workspace_assessment.py tests/test_durable_verification_dispatch.py \
  tests/test_fingerprint_cache.py tests/test_root_module_architecture.py
git commit -m "perf(verify): bypass rich assessment for explicit runs"
```

---

### Task 4: Add Declarative Test Catalog and Deterministic Shadow Planner

**Files:**
- Create: `tests/catalog.toml`
- Create: `scripts/test_catalog.py`
- Create: `tests/test_test_catalog.py`
- Modify: `tests/test-groups.toml`
- Modify: `scripts/select_affected_tests.py`
- Modify: `tests/test_select_affected_tests.py`

**Interfaces:**
- Produces: `load_catalog(path: Path) -> TestCatalog`
- Produces: `validate_catalog(catalog: TestCatalog, root: Path) -> tuple[str, ...]`
- Produces: `compile_plan(catalog: TestCatalog, changed_paths: Sequence[str], intent: str) -> VerificationPlan`
- Produces: `compare_shadow(authoritative: Selection, shadow: VerificationPlan) -> ShadowComparison`
- Adds selector CLI: `--catalog`, `--check-catalog`, `--shadow-catalog`.

- [ ] **Step 1: Write failing catalog validation and plan tests**

Create tests covering exactly-one ownership, missing test files, invalid isolation/resource values, deterministic digest independent of TOML table ordering, deterministic selected tests, explicit widening reasons, and no-false-negative comparison.

Representative test:

```python
def test_shadow_plan_must_cover_every_authoritative_test(tmp_path: Path) -> None:
    comparison = compare_shadow(
        authoritative_files=("tests/test_a.py", "tests/test_b.py"),
        shadow_files=("tests/test_a.py",),
    )
    assert comparison.false_negatives == ("tests/test_b.py",)
    assert comparison.safe_to_cut_over is False
```

- [ ] **Step 2: Run focused tests and prove RED**

Run:

```bash
uv run pytest -q tests/test_test_catalog.py tests/test_select_affected_tests.py
```

Expected: FAIL because catalog functions/options do not exist.

- [ ] **Step 3: Implement catalog schema**

Use a compact capability-level catalog with per-test overrides:

```toml
version = 1

[defaults]
platforms = ["linux", "macos"]
python = ["3.10", "3.11", "3.12", "3.13"]
cost = "medium"

[capabilities.operations]
source_globs = ["src/repoforge/application/operations/**", "src/repoforge/domain/operations.py"]
test_files = ["tests/test_operation_tasks.py"]
isolation = "exclusive:operation-state"
resources = ["operation-state"]
```

Valid isolation values: `pure`, `sandboxed_fs`, `sandboxed_process`, `system`, or `exclusive:<safe-resource-id>`. Cost values: `small`, `medium`, `large`, `system`.

Populate the catalog from every existing manifest group and preserve one owner per test file. Treat source overlap as explicit cross-capability breadth, not duplicate test ownership.

- [ ] **Step 4: Implement deterministic planning and shadow comparison**

The catalog planner selects changed tests themselves, matching capabilities, and the safety bundle. Unknown paths widen to all catalog tests with a stable reason. It outputs sorted lanes and canonical digest. It does not execute tests.

Integrate selector report-only mode: after authoritative selection, compile the shadow plan, print false-negative/extra counts, and include the comparison in the performance artifact. A false negative fails `--check-catalog` but does not replace authoritative selection.

- [ ] **Step 5: Run catalog and selector tests and prove GREEN**

Run:

```bash
uv run pytest -q tests/test_test_catalog.py tests/test_select_affected_tests.py
uv run python scripts/test_catalog.py --check tests/catalog.toml
uv run python scripts/select_affected_tests.py --path src/repoforge/domain/verification.py \
  --shadow-catalog tests/catalog.toml
```

Expected: tests PASS; catalog is complete; shadow comparison reports zero false negatives.

- [ ] **Step 6: Commit**

```bash
git add tests/catalog.toml scripts/test_catalog.py tests/test_test_catalog.py \
  tests/test-groups.toml scripts/select_affected_tests.py \
  tests/test_select_affected_tests.py
git commit -m "feat(test): add declarative shadow catalog"
```

---

### Task 5: Reuse Canonical Coverage for Coverage-Map Observation

**Files:**
- Modify: `scripts/run_test_suite.py`
- Modify: `scripts/build_coverage_map.py`
- Modify: `Makefile`
- Modify: `tests/test_parallel_test_runner.py`
- Create: `tests/test_coverage_map_reuse.py`

**Interfaces:**
- Coverage runner writes combined data at `<coverage-dir>/.coverage` with per-test contexts.
- `build_coverage_map.py --from-existing-coverage --coverage-file PATH --check` performs no pytest subprocess.
- `make test-map-check-existing COVERAGE_FILE=...` validates the committed map from canonical coverage.

- [ ] **Step 1: Write failing reuse tests**

Test that coverage lane commands include `--cov-context=test`; `--from-existing-coverage` never calls `_record_coverage`; and the Make target requires `COVERAGE_FILE`.

- [ ] **Step 2: Run focused tests and prove RED**

Run:

```bash
uv run pytest -q tests/test_parallel_test_runner.py tests/test_coverage_map_reuse.py
```

Expected: FAIL because canonical coverage lacks test contexts and no Make target exists.

- [ ] **Step 3: Implement canonical observation reuse**

Add `--cov-context=test` only to coverage mode. Keep lane-specific files, combine into `<coverage-dir>/.coverage`, and leave the combined file available after reporting. Add a Make target:

```make
test-map-check-existing:
	@test -n "$${COVERAGE_FILE:-}" || { echo "COVERAGE_FILE is required" >&2; exit 2; }
	uv run --extra dev python scripts/build_coverage_map.py \
	  --from-existing-coverage --coverage-file "$${COVERAGE_FILE}" --check
```

- [ ] **Step 4: Run focused tests and prove GREEN**

Run the same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_test_suite.py scripts/build_coverage_map.py Makefile \
  tests/test_parallel_test_runner.py tests/test_coverage_map_reuse.py
git commit -m "perf(ci): reuse canonical coverage observations"
```

---

### Task 6: De-duplicate CI into Canonical Coverage and Compatibility Shards

**Files:**
- Create: `scripts/run_compatibility_tests.py`
- Modify: `.github/workflows/production-gate.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_phase8_program_completion.py`
- Create: `tests/test_ci_verification_topology.py`

**Interfaces:**
- `scripts/run_compatibility_tests.py` invokes a reviewed no-coverage selector list and accepts pytest passthrough arguments only through a fixed parser.
- CI jobs: `affected-tests`, `canonical-coverage`, `compatibility`, `live-activation`, `package`, stable `production-gate`.
- Environment switch: `REPOFORGE_CI_FULL_MATRIX=1` restores full no-coverage behavioral execution in compatibility cells.

- [ ] **Step 1: Write failing workflow-topology tests**

Assert:

```python
def test_protected_push_has_one_canonical_coverage_command() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("--cov=repoforge") == 1
    assert "canonical-coverage:" in workflow
    assert "compatibility:" in workflow
    assert "test-map-check-existing" in workflow
    assert "make test-map-check" not in workflow


def test_compatibility_matrix_is_no_coverage() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    compatibility = workflow.split("compatibility:", 1)[1].split("live-activation:", 1)[0]
    assert "--cov" not in compatibility
    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert version in compatibility
    assert "macos-latest" in compatibility
```

- [ ] **Step 2: Run focused tests and prove RED**

Run:

```bash
uv run pytest -q tests/test_ci_verification_topology.py tests/test_phase8_program_completion.py
```

Expected: FAIL because the current matrix runs coverage in every push cell and has a separate full map job.

- [ ] **Step 3: Implement compatibility runner**

Use reviewed selectors covering configuration parsing, contract identity, onboarding real Git, repository discovery, process/runtime portability, and one package import smoke. Default no coverage and `-q`; support `--full` only for the emergency environment switch.

- [ ] **Step 4: Rewrite production workflow topology**

Pull request:

- static contracts and catalog checks;
- one affected-tests job on Ubuntu/Python 3.13;
- path-triggered live activation and package checks remain conservative.

Protected push:

- one Ubuntu/Python 3.13 canonical coverage job running `make coverage`, `make test-map-check-existing`, and uploading coverage/performance artifacts;
- no-coverage compatibility matrix for Ubuntu 3.10-3.13 and macOS 3.13;
- live activation and package jobs;
- umbrella `production-gate` explicitly validates all required results.

Keep `.github/workflows/ci.yml` static-only and remove overlapping checks already guaranteed by `static-contracts` only if the stable check contract permits it; otherwise document the intentional quick duplicate.

- [ ] **Step 5: Run focused tests and prove GREEN**

Run:

```bash
uv run pytest -q tests/test_ci_verification_topology.py tests/test_phase8_program_completion.py
uv run python scripts/run_compatibility_tests.py --help
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_compatibility_tests.py .github/workflows/production-gate.yml \
  .github/workflows/ci.yml tests/test_phase8_program_completion.py \
  tests/test_ci_verification_topology.py
git commit -m "perf(ci): run coverage once per protected sha"
```

---

### Task 7: Resource-Isolate a Serial Capability and Preserve Exclusive Fallback

**Files:**
- Modify: `tests/test-groups.toml`
- Modify: `tests/catalog.toml`
- Modify: `scripts/test_catalog.py`
- Modify: `scripts/run_test_suite.py`
- Modify: `tests/test_parallel_test_runner.py`
- Modify: `tests/test_test_catalog.py`
- Create or modify: `tests/test_ticket_graph_resource_isolation.py`

**Interfaces:**
- Plan lanes include `exclusive:<resource>` keys.
- Existing serial groups compile to exclusive resources.
- Ticket graph pure-domain tests become `pure`; adapter/publication/system tests retain `exclusive:ticket-provider-state`.

- [ ] **Step 1: Identify and codify the tracer split in failing tests**

Create a test asserting the catalog places pure ticket domain files in the parallel lane while provider/workflow files remain exclusive. Add a stress test that runs the pure subset repeatedly under xdist with isolated temporary repositories and asserts no fixed external state is used.

- [ ] **Step 2: Run tracer tests and prove RED**

Run:

```bash
uv run pytest -q tests/test_ticket_graph_resource_isolation.py \
  tests/test_test_catalog.py tests/test_parallel_test_runner.py
```

Expected: FAIL because the entire ticket graph group is currently serial and the runner has only one serial bucket.

- [ ] **Step 3: Split test ownership and compile exclusive lanes**

Move pure ticket graph/domain tests to a new parallel capability in both manifest and catalog. Keep GitHub adapter, publication executor, workflow, project sync, and real-provider-bound tests exclusive. Extend `VerificationPlan` and full runner lane plan to name exclusive resources deterministically; execute exclusive lanes sequentially, then the pure xdist lane.

If stress evidence finds shared state in a proposed pure file, leave that file exclusive and record the limitation in the catalog rather than weakening isolation.

- [ ] **Step 4: Run focused and repeated stress tests and prove GREEN**

Run:

```bash
uv run pytest -q tests/test_ticket_graph_resource_isolation.py \
  tests/test_test_catalog.py tests/test_parallel_test_runner.py
uv run pytest -q -n 3 --count=5 \
  tests/test_ticket_graph.py tests/test_ticket_readiness.py
```

If `pytest-repeat` is not installed, replace the final command with a POSIX loop invoking the same two files five times. Expected: every run PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test-groups.toml tests/catalog.toml scripts/test_catalog.py \
  scripts/run_test_suite.py tests/test_parallel_test_runner.py \
  tests/test_test_catalog.py tests/test_ticket_graph_resource_isolation.py
git commit -m "perf(test): isolate ticket graph resources"
```

---

### Task 8: Align Profiles, Documentation, and Final Verification

**Files:**
- Modify: `config.example.toml`
- Modify: `docs/development/DEVELOPMENT.md`
- Modify: `docs/testing/TESTING.md`
- Modify: `docs/testing/FULL_FLOW_TESTING.md`
- Modify: `README.md`
- Modify: `tests/test_docs_command_drift.py`
- Modify: `tests/test_expensive_profile_guards.py` if profile discovery semantics change.
- Modify generated contracts only if public schemas changed; otherwise assert no schema drift.

**Interfaces:**
- Model-facing profiles map to affected/quick/verify intents.
- Authoritative `full`/`gate` remains `model_invocable = false` with minimum interval.
- Operator docs publish one command per intent and rollback switches.

- [ ] **Step 1: Write failing documentation/profile contract tests**

Assert documentation describes:

- `make test` as affected, no coverage;
- `make test-full` as full, no coverage;
- `make coverage` as canonical branch coverage;
- `make gate` as operator/CI authority;
- catalog shadow mode and artifact locations;
- `REPOFORGE_CI_FULL_MATRIX=1` rollback.

Assert example profile commands match the Make contracts and reserve the gate from model initiation.

- [ ] **Step 2: Run focused tests and prove RED**

Run:

```bash
uv run pytest -q tests/test_docs_command_drift.py tests/test_expensive_profile_guards.py
```

Expected: FAIL on stale command semantics.

- [ ] **Step 3: Update profiles and docs**

Document the migration state honestly: catalog is shadow authority, the old selector still executes, and cutover requires zero false negatives over the evidence window. Document safe rollback for command aliases, rich assessment, all-exclusive lanes, old selector, and full CI matrix.

- [ ] **Step 4: Run focused tests and prove GREEN**

Run the same command. Expected: PASS.

- [ ] **Step 5: Run complete metadata/static gates**

Run:

```bash
uv run python scripts/select_affected_tests.py --check-completeness
uv run python scripts/test_catalog.py --check tests/catalog.toml
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy --strict src/repoforge
```

Expected: PASS.

- [ ] **Step 6: Run affected and full behavioral verification**

Run:

```bash
make test
make test-full
```

Expected: PASS; artifacts show the selected count and lane durations.

- [ ] **Step 7: Run final repository verification and production gate**

Run:

```bash
make verify
make check
```

Expected: PASS on the exact current fingerprint. If `make check` is operator-only through RepoForge policy, run the operator-owned `full` profile and retain its durable operation/receipt evidence.

- [ ] **Step 8: Review exact diff and commit**

```bash
git diff --check
git status --short
git add README.md config.example.toml docs tests scripts src Makefile .github
git commit -m "docs(test): publish verification control plane"
```

- [ ] **Step 9: Push and create a draft PR**

Push the exact verified branch without force. Create a draft PR summarizing each migration slice, command semantics, CI compute reduction, shadow-planner status, final gate evidence, and rollback controls. Do not claim catalog cutover if it remains shadow-only.

---

## Plan Self-Review

- **Spec coverage:** Tasks 1-8 cover command contracts, fail-closed metadata, snapshot preflight, catalog/shadow planning, performance artifacts, canonical coverage reuse, CI de-duplication, one resource-isolation tracer, profile/docs alignment, rollback, and final gates.
- **Scope:** Catalog cutover and removal of the legacy selector are deliberately not claimed; this plan delivers the required shadow evidence and leaves removal gated by the design's evidence window.
- **Type consistency:** `SnapshotToken`, `LanePlan`, `VerificationArtifact`, `TestCatalog`, `VerificationPlan`, and `ShadowComparison` are defined once and consumed by later tasks using the same names.
- **Placeholder scan:** No TBD/TODO or unspecified error-handling steps remain.
