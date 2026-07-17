# Runtime Reliability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make RepoForge runtime health truthful and self-healing while improving forensic logs, commit failure diagnostics, verification telemetry, and guided recovery without adding any new MCP tool.

**Architecture:** Preserve the existing CLI, MCP, supervisor, workspace, and audit boundaries. Add typed internal health observations and tunnel probes behind the existing `TunnelClient` port; make `rf runtime status` actively probe the existing supervisor control socket; make the supervisor continuously probe the existing MCP/tunnel path and apply bounded restart/circuit-breaker policy. Enhance existing `runtime_logs_read`, `workspace_commit`, `workspace_run_profile`, `workspace_run_diagnostic`, and workspace errors compatibly rather than introducing new public tools.

**Tech Stack:** Python 3.10+, standard library, existing Unix runtime control protocol, existing tunnel-client admin HTTP endpoint, pytest, Ruff, strict Mypy.

## Global Constraints

- Use one RepoForge-managed worktree and one branch for the complete change.
- Do not add a new MCP tool, especially no commit-readiness tool.
- Preserve existing tool names and existing response fields; additions must be backward-compatible.
- Do not weaken path, branch, verification, command, logging, or process-identity safety invariants.
- Do not add dependencies.
- All subprocess and network probes must be bounded and fail closed.
- Audit and runtime logs must remain secret-safe and must not include file bodies, patches, or full environments.

---

### Task 1: Typed Runtime Health Observation

**Files:**
- Modify: `src/repoforge/domain/runtime.py`
- Modify: `src/repoforge/ports/tunnel.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_phase4_runtime_control.py`

**Interfaces:**
- Produces: `HealthState`, `HealthObservation`, `RuntimeHealthSnapshot`, and `TunnelClient.health(child, *, timeout_seconds) -> HealthObservation`.
- Consumers: supervisor watchdog and CLI status probing.

- [ ] Write failing tests for health-state validation, deterministic component ordering, stale observation handling, and backward-compatible `RuntimeRecord.health` rendering.
- [ ] Run the targeted tests and confirm failure.
- [ ] Implement immutable typed health observations with bounded detail strings and timestamps.
- [ ] Extend `RuntimeRecord` compatibly with observed health metadata while accepting old persisted records.
- [ ] Run the targeted tests and confirm pass.

### Task 2: Tunnel Health Probe Behind Existing Port

**Files:**
- Modify: `src/repoforge/adapters/runtime/tunnel_cli.py`
- Test: `tests/test_runtime_adapters_and_serve.py`
- Test: `tests/test_phase6_operational_hardening.py`

**Interfaces:**
- Consumes: `HealthObservation` from Task 1.
- Produces: bounded local HTTP/admin health probe plus process-liveness fallback; no new public command or MCP tool.

- [ ] Write failing tests using a local fake HTTP server for healthy, malformed, timeout, stale, and unavailable admin responses.
- [ ] Run targeted tests and confirm failure.
- [ ] Implement loopback-only health probing using `urllib.request`, strict timeout, bounded JSON parsing, and redacted details.
- [ ] Preserve PID/identity liveness as one component rather than the overall truth.
- [ ] Run targeted tests and confirm pass.

### Task 3: Continuous Supervisor Watchdog and Bounded Recovery

**Files:**
- Modify: `src/repoforge/application/runtime/supervisor.py`
- Modify: `src/repoforge/bootstrap.py`
- Test: `tests/test_phase4_runtime_control.py`
- Test: `tests/test_phase7_atomic_hot_reload.py`

**Interfaces:**
- Consumes: `TunnelClient.health`, MCP control `HEALTH`, runtime store.
- Produces: periodic health snapshots, `HEALTHY -> DEGRADED -> restart`, restart-window reset, and terminal circuit-open failure.

- [ ] Write failing tests for one transient failure, consecutive failures, MCP control failure while PID remains alive, recovery to healthy, restart after threshold, and bounded restart circuit opening.
- [ ] Run targeted tests and confirm failure.
- [ ] Implement one `_observe_health` path reused by startup, watchdog, and control handler.
- [ ] Replace PID-only monitor loop with periodic probe loop and configurable thresholds using existing server/runtime constants.
- [ ] Persist degraded and failure evidence before terminating the child.
- [ ] Reset consecutive failure/restart budget only after a stable healthy interval.
- [ ] Run targeted tests and confirm pass.

### Task 4: Truthful Existing Runtime Status Command

**Files:**
- Modify: `src/repoforge/interfaces/cli/main.py`
- Test: `tests/test_cli_runtime_commands.py`
- Test: `tests/test_cli_surface_coverage.py`

**Interfaces:**
- Consumes: existing supervisor control `HEALTH` and persisted runtime record.
- Produces: active observed state, persisted state, probe status, age, components, and meaningful exit status through the existing `rf runtime status` command.

- [ ] Write failing tests for healthy probe, persisted-healthy/probe-failed false health, stopped runtime, timeout, and degraded response.
- [ ] Run targeted tests and confirm failure.
- [ ] Make `_runtime_status` perform a bounded active supervisor probe and reconcile it with persisted state.
- [ ] Return non-zero from `rf runtime status` for unhealthy/stale/failed states while keeping JSON output available.
- [ ] Keep existing fields and add `persisted_state`, `observed_state`, `probe`, and component evidence.
- [ ] Run targeted tests and confirm pass.

### Task 5: Incident-Grade Existing Runtime Log Reads

**Files:**
- Modify: `src/repoforge/adapters/runtime/local_runtime.py`
- Modify: `src/repoforge/application/config_admin/service.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_config_admin.py`
- Test: `tests/test_mcp_contract.py`

**Interfaces:**
- Enhances: existing `runtime_logs_read` and `rf runtime logs`; no new tool.
- Produces: merged bounded tail across active and rotated files, source-file metadata, restart/session boundary summaries, and stable ordering.

- [ ] Write failing tests for `.3 -> .2 -> .1 -> active` chronological merge, global line bound, missing files, malformed UTF-8, redaction, and no absolute-path leakage in MCP output.
- [ ] Run targeted tests and confirm failure.
- [ ] Replace single-file reader with a compatible result model that can read rotations while preserving a simple line-list adapter for old callers.
- [ ] Enhance existing MCP response with relative file labels and rotation coverage.
- [ ] Enhance existing CLI logs response with the same evidence.
- [ ] Run targeted tests and confirm pass.

### Task 6: Structured Commit Failure Diagnostics in Existing Tool

**Files:**
- Modify: `src/repoforge/adapters/subprocess/command_executor.py`
- Modify: `src/repoforge/adapters/git/cli.py`
- Modify: `src/repoforge/application/workspace/commit.py`
- Modify: `src/repoforge/domain/errors.py`
- Test: `tests/test_command_executor_error_codes.py`
- Test: `tests/test_phases1_4_real_git_integration.py`
- Test: `tests/test_service_tools.py`

**Interfaces:**
- Enhances: existing `workspace_commit` result/error only.
- Produces: bounded command argv identity, exit code, stderr excerpt/digest, hook-mutation detection, changed paths after failure, verification invalidation, and specific commit-stage error codes.

- [ ] Write failing integration tests with pre-commit success, pre-commit rejection, and hook-mutated-tree scenarios.
- [ ] Run targeted tests and confirm failure.
- [ ] Preserve bounded stdout/stderr excerpts in `CommandError.details` with redaction and truncation flags.
- [ ] Wrap Git add/commit stages with specific error codes and stage metadata.
- [ ] Detect tree mutation after failed commit, invalidate stale verification receipt, and return exact changed paths plus safe next action.
- [ ] Run targeted tests and confirm pass.

### Task 7: Verification Stage Telemetry and Faster Failure Evidence

**Files:**
- Modify: `src/repoforge/application/workspace/run_profile.py`
- Modify: `src/repoforge/domain/errors.py`
- Test: `tests/test_retry_guidance.py`
- Test: `tests/test_background_run_profile.py`
- Test: `tests/test_command_source_integrity.py`

**Interfaces:**
- Enhances: existing `workspace_run_profile` success and failure payloads.
- Produces: per-command stage index, duration, status, cumulative duration, first failing stage, and targeted diagnostic suggestions.

- [ ] Write failing tests for successful stage timing, first-stage failure, later-stage failure, background parity, and bounded telemetry.
- [ ] Run targeted tests and confirm failure.
- [ ] Measure every approved command with monotonic time and attach structured telemetry to success results and errors.
- [ ] Preserve current retry guidance and add exact first-failing-stage evidence.
- [ ] Run targeted tests and confirm pass.

### Task 8: Guided Diagnostic and Workspace Error Contracts

**Files:**
- Modify: `src/repoforge/application/workspace/diagnostic_selector.py`
- Modify: `src/repoforge/application/context.py`
- Modify: `src/repoforge/domain/errors.py`
- Test: `tests/test_workspace_diagnostics.py`
- Test: `tests/test_workspace_stale_cleanup.py`
- Test: `tests/test_service_tools.py`

**Interfaces:**
- Enhances: existing `workspace_run_diagnostic`, workspace reads/mutations, and workspace listing/error responses.
- Produces: selector schema details/examples and distinct workspace error codes for missing path, outside root, invalid Git metadata, branch mismatch, and repository orphaning.

- [ ] Write failing tests for selector detail payloads and each workspace invariant failure.
- [ ] Run targeted tests and confirm failure.
- [ ] Add specific stable error codes and `why` mappings.
- [ ] Add selector kind/name/max-values and safe examples to diagnostic errors without exposing host paths.
- [ ] Keep existing workspace-list stale cleanup guidance and align it with specific codes.
- [ ] Run targeted tests and confirm pass.

### Task 9: Documentation, Compatibility, and Full Verification

**Files:**
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/architecture/phase6-operational-hardening.md`
- Modify: `CHANGELOG.md`
- Modify as required: `docs/contracts/release-contract-v1.json`
- Test: `tests/test_docs_command_drift.py`
- Test: `tests/test_mcp_contract.py`
- Test: `tests/test_tool_contract.py`

**Interfaces:**
- Produces: documented behavior for enhanced existing tools/commands and a reviewed release contract.

- [ ] Document truthful status, watchdog recovery, rotated-log evidence, commit diagnostics, and verification telemetry.
- [ ] Run formatter over changed Python files.
- [ ] Run targeted tests for all touched modules.
- [ ] Run the `quick` profile.
- [ ] Run the full default verification profile once on the final exact tree.
- [ ] Review `workspace_diff` and confirm no new MCP tool exists.
- [ ] Commit with a scoped Conventional Commit message.
