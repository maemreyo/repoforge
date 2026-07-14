# Structured CI Failure Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, read-only GitHub Check Run details and failure evidence that are selected only through RepoForge-issued IDs and bound to the exact pushed workspace SHA.

**Architecture:** Preserve `workspace_pr_checks` as the compact compatibility operation and enrich its items with opaque Check Run selectors. New workspace use cases consume typed GitHub-port models, verify pushed/head identity, sanitize all model-visible evidence locally, and return deterministic partial coverage when optional GitHub sources are unavailable.

**Tech Stack:** Python 3.10+, dataclasses, GitHub CLI/REST through `gh api`, FastMCP, pytest, deterministic fake `gh`.

## Global Constraints

- No arbitrary URLs, API paths, shell commands, or free-form `gh` arguments are public inputs.
- Both new MCP tools are read-only external reads and closed to arbitrary external targets.
- Every result is bound to `last_pushed_sha`; stale local or Check Run SHAs fail closed.
- Annotation, job, log, excerpt, byte, line, attempt, and output counts remain bounded.
- Secrets, private keys, credential URLs, high-confidence tokens, and denied source snippets never reach model output, audit records, or diagnostics.
- Existing `workspace_pr_checks` fields and behavior remain compatible.
- Reruns, cancellation/watching, workflow administration, and semantic source analysis are excluded.

---

### Task 1: Specify selectors and CI evidence behavior with failing tests

**Files:**
- Create: `tests/test_ci_failure_evidence.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_service_tools.py`
- Modify: `tests/test_mcp_contract.py`

**Interfaces:**
- Consumes: existing `ForgeEnvironment`, workspace lifecycle, fake `gh`, and MCP in-memory client.
- Produces: executable contracts for selector enrichment, check details, failure evidence, SHA binding, redaction, truncation, partial coverage, and metadata.

- [ ] Extend fake `gh` state with deterministic Check Runs, annotations, Actions jobs, job logs, permission failures, and per-attempt metadata while preserving current lifecycle defaults.
- [ ] Write a failing compatibility test asserting compact checks retain name/state/bucket/link/workflow and add a usable `check-run:<id>` selector plus exact `head_sha`.
- [ ] Write failing service tests for failed annotation evidence, failed-step/log fallback, pass/pending/cancelled/retried states, unavailable logs, and stale Check Run SHA rejection.
- [ ] Write failing redaction tests covering assignment secrets, bearer tokens, credential URLs, private-key blocks, high-entropy tokens, and denied paths.
- [ ] Extend MCP tool inventory and invocation tests with both new read-only operations.
- [ ] Run `uv run pytest tests/test_ci_failure_evidence.py tests/test_service_tools.py tests/test_mcp_contract.py -q` and confirm failures are caused by missing selectors/tools/use cases.

### Task 2: Add typed GitHub Check Run primitives and adapter behavior

**Files:**
- Modify: `src/repoforge/ports/github.py`
- Modify: `src/repoforge/ports/__init__.py`
- Modify: `src/repoforge/adapters/github/gh_cli.py`

**Interfaces:**
- Produces:
  - `GitHubCheckRun`;
  - `GitHubCheckAnnotation`;
  - `GitHubActionsStep`;
  - `GitHubActionsJob`;
  - `GitHubJobLog`;
  - `check_run(cwd, check_run_id)`;
  - `check_annotations(cwd, check_run_id, max_annotations)`;
  - `actions_job(cwd, job_id)`;
  - `actions_job_log(cwd, job_id, max_chars)`.

- [ ] Add immutable typed models with normalized snake-case fields and no raw response dictionaries.
- [ ] Add strict JSON-object/list parsing helpers and bounded `gh api --method GET` calls for Check Runs, annotations, Actions jobs, and logs.
- [ ] Enrich `checks()` by resolving the PR `headRefOid`, listing latest Check Runs for that commit, matching by exact details URL then unique name, and adding selector/head identity without removing legacy fields.
- [ ] Parse Actions run/job IDs only from GitHub-owned `details_url` shapes; never accept the URL back as a public selector.
- [ ] Return bounded annotation/log truncation metadata and convert unavailable optional sources into deterministic adapter errors that application code may downgrade to partial evidence.
- [ ] Run focused adapter and CI evidence tests until typed parsing and enrichment pass.

### Task 3: Add selector validation, secret-safe egress, and failure classification

**Files:**
- Create: `src/repoforge/domain/ci_evidence.py`
- Modify: `src/repoforge/domain/errors.py`
- Test: `tests/test_ci_failure_evidence.py`

**Interfaces:**
- Produces:
  - `parse_check_selector(value: str) -> int`;
  - `sanitize_ci_text(value, repo, max_chars) -> SanitizedCiText`;
  - `classify_ci_failure(parts) -> CiFailureClassification`;
  - stable selector/stale/unavailable error codes.

- [ ] Accept only `check-run:<positive decimal ID>` and reject whitespace, URLs, signs, overflow-length IDs, and alternate prefixes.
- [ ] Redact private-key blocks before line processing, apply existing credential redaction, replace high-confidence long tokens, and withhold lines that expose denied repository paths.
- [ ] Preserve deterministic sanitized text, `redacted`, `withheld_lines`, and `truncated` metadata.
- [ ] Implement ordered classification for cancellation, timeout, policy, lint, type, build, dependency, environment, network, test, and unknown.
- [ ] Mark network, timeout, environment, and cancellation classes retryable.
- [ ] Add operation-error explanations for invalid selector, stale evidence, and unavailable primary Check Run evidence.
- [ ] Run pure domain tests and confirm no raw secret values appear in assertions, audit fixtures, or failure messages.

### Task 4: Add workspace details and failure-evidence use cases

**Files:**
- Create: `src/repoforge/application/workspace/pr_check_context.py`
- Create: `src/repoforge/application/workspace/pr_check_details.py`
- Create: `src/repoforge/application/workspace/pr_failure_evidence.py`
- Modify: `src/repoforge/application/workspace/pr_checks.py`

**Interfaces:**
- Produces:
  - `WorkspacePrCheckDetailsCommand(workspace_id, check_selector)`;
  - `WorkspacePrFailureEvidenceCommand(workspace_id, check_selector, max_excerpt_lines=80)`;
  - exact identity, status, annotations, failed step, failure class, excerpt/hash, retryability, coverage, uncertainty, truncation, and redaction fields.

- [ ] Add a shared context loader that requires `last_pushed_sha`, verifies local HEAD equality, loads the Check Run, and rejects mismatched Check Run `head_sha`.
- [ ] Load at most 50 annotations and one Actions job; expose run/job/attempt IDs and deterministic failed-step selection.
- [ ] Return check details without downloading logs.
- [ ] Build failure evidence from sanitized annotations first, Check Run output/failed-step metadata second, and a bounded log only when earlier sources are insufficient.
- [ ] Bound `max_excerpt_lines` to 1–200, hash the final model-visible excerpt, and report `coverage=complete|partial|none`, uncertainty, and stable source-error labels.
- [ ] Return deterministic no-failure evidence for passing, pending, skipped, and cancelled checks; never fabricate an excerpt.
- [ ] Ensure audited details include only workspace ID, selector, line limit, counts, and statuses—not excerpt or raw external errors.
- [ ] Run service tests for every specified state and source fallback.

### Task 5: Wire service/MCP contracts and reviewed documentation

**Files:**
- Modify: `src/repoforge/application/service.py`
- Modify: `src/repoforge/interfaces/mcp/server.py`
- Modify: `tests/test_phase5_mcp_contract.py`
- Modify: `docs/contracts/release-contract-v1.json`
- Modify: `docs/development/TOOL_REFERENCE.md`
- Modify: `docs/testing/TESTING.md`
- Modify: `docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md`

**Interfaces:**
- Produces:
  - `workspace_pr_check_details(workspace_id, check_selector)`;
  - `workspace_pr_failure_evidence(workspace_id, check_selector, max_excerpt_lines=80)`.

- [ ] Add two facade readers and two thin MCP tools whose descriptions begin with `Use this` and annotations use external read semantics.
- [ ] Update tool-count tests from 31 to 33 and invoke both operations in the in-memory MCP lifecycle.
- [ ] Regenerate the release contract only after reviewing the exact two-tool schema addition and enriched compatibility behavior.
- [ ] Document selector acquisition, exact SHA binding, evidence priority, redaction, bounds, partial coverage, and non-goals.
- [ ] Mark issue #7 capability implemented in the roadmap without claiming check watching/cancellation from #10.
- [ ] Run focused MCP, release-contract, service, and CI evidence tests.

### Task 6: Final review, production verification, and publication

**Files:**
- Review all changed files; no new implementation files beyond Tasks 1–5.

**Interfaces:**
- Consumes: final exact workspace tree.
- Produces: verified commit, pushed `ai/*` branch, and draft PR closing issue #7.

- [ ] Review `workspace_diff` for unrelated cleanup, accidental raw logs/secrets, generic GitHub inputs, and public-contract drift.
- [ ] Run RepoForge `full`, which executes `scripts/verify-production.sh --allow-dirty`; require formatting, lint, strict typing, all tests, coverage, release contracts, source/wheel builds, and installed-wheel smoke to pass.
- [ ] Confirm the verification fingerprint still matches after all documentation and generated-contract changes.
- [ ] Commit the exact verified tree with `feat(ci): add structured failure evidence`.
- [ ] Push without force and create a draft PR whose body includes scope, safety, compatibility, verification evidence, deferred watching/cancellation, and `Closes #7`.
- [ ] Read PR status and report any pending or failed CI checks without merging or marking ready.
