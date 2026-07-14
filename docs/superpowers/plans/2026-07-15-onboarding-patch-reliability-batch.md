# Issues #103, #105, #106, #104, #111, #112, #113, and #114 Implementation Plan

> Execute in this single RepoForge workspace. Follow test-driven development and preserve exact-tree verification.

**Goal:** Deliver the next eight Ready P0 tickets across onboarding/configuration UX and patch reliability without weakening RepoForge safety invariants.

**Design:** `docs/superpowers/specs/2026-07-15-onboarding-patch-reliability-batch-design.md`

## Task 1 — Establish failing contracts

**Create:**
- `tests/test_configuration_paths.py`
- `tests/test_docs_command_drift.py`
- `tests/test_local_setup.py`
- `tests/test_patch_normalization.py`

**Modify:**
- `tests/test_onboarding_ui.py`
- `tests/test_integration.py`
- `tests/test_mcp_contract.py`

Write failing tests for:
- `rf config path` and one path authority;
- local-only source config and setup parser;
- setup/onboarding files-written summaries;
- standard-install Rich/Inquirer imports;
- docs/scripts parser drift;
- whitespace-fixing Git apply pre-check;
- OpenAI envelope add/update/delete/move;
- unified-diff recount/relocation/whitespace repair;
- ambiguous/missing context and structured errors;
- no patch bodies in audit metadata.

Run only the new/changed tests and confirm RED for missing APIs/behavior.

## Task 2 — Configuration path authority and CLI discoverability (#106)

**Create:** `src/repoforge/application/configuration/paths.py`

**Modify:**
- `src/repoforge/interfaces/cli/main.py`
- `src/repoforge/interfaces/cli/contract.py`
- `src/repoforge/interfaces/cli/onboarding.py`
- `README.md`
- `docs/development/DEVELOPMENT.md`

Implement typed absolute path resolution, `rf config path`, doctor path payload, and files-written summaries. Keep path inspection read-only and available before an accepted generation exists.

Run path/CLI/onboarding tests.

## Task 3 — Local-first setup and serve (#104)

**Modify:**
- `src/repoforge/application/configuration/source.py`
- `src/repoforge/interfaces/cli/main.py`
- `src/repoforge/bootstrap.py`
- `README.md`
- `docs/getting-started/CHATGPT_SETUP.md`

Make tunnel configuration optional only for `rf setup --local`. Permit accepted local-only generation and direct `rf serve`; reject managed runtime start/reload/restart before mutation with actionable tunnel/client/API-key guidance.

Run source-config, setup, runtime, serve, and wheel-E2E tests.

## Task 4 — Standard-install interactive UI (#105)

**Modify:**
- `pyproject.toml`
- `uv.lock`
- `src/repoforge/interfaces/cli/onboarding_ui.py`
- `README.md`

Move Rich to runtime dependencies and add bounded InquirerPy. Preserve lazy plain/non-TTY behavior. Regenerate the lock file through an allowlisted repository command.

Run UI/import/build tests.

## Task 5 — Repair command/document drift (#103)

**Modify:**
- `scripts/bootstrap-macos.sh`
- `scripts/e2e-preflight.sh`
- `Makefile`
- `config.example.toml`
- `README.md`
- `docs/development/DEVELOPMENT.md`

Remove nonexistent commands/options and personal identifiers. Parameterize repository IDs and paths. Make the docs-command drift test authoritative over shell/Makefile/selected docs invocations.

Run drift and CLI parser tests.

## Task 6 — Patch domain, formats, and deterministic preprocessing (#113, #114)

**Create:**
- `src/repoforge/domain/patches.py`
- `src/repoforge/application/workspace/patch_input.py`

**Modify:**
- `src/repoforge/application/workspace/apply_patch.py`
- `src/repoforge/domain/policy.py`

Implement bounded format detection, envelope parsing, exact/whitespace-normalized unique context matching, desired-state construction, canonical unified-diff rendering, and repair metadata. Validate all candidate and normalized paths through existing policy.

Run the complete patch corpus.

## Task 7 — Git parity and actionable failures (#111, #112)

**Modify:**
- `src/repoforge/adapters/git/cli.py`
- `src/repoforge/domain/errors.py`
- `src/repoforge/interfaces/mcp/server.py`
- `src/repoforge/interfaces/cli/main.py`
- relevant error/contract tests

Use `--whitespace=nowarn` for pre-check and retain `--whitespace=fix` for apply. Add stable patch error codes, bounded structured details, class-specific safe actions, and tool guidance for unified diff/envelope plus write/replace alternatives.

Run real-Git, MCP, security, and audit tests.

## Task 8 — Documentation, release contracts, and ticket graph

**Modify:**
- `CHANGELOG.md`
- `BUILD_REPORT.md`
- `docs/roadmaps/REPOFORGE_MASTER_ROADMAP.md`
- `docs/roadmaps/REPOFORGE_TICKET_GRAPH.json`
- release-contract fixtures if required

Add #101–#116 to the machine graph with reciprocal parent/child and blocker edges. Mark merged prior work Done and this batch In progress until the draft PR exists. Validate the graph and next Ready selection.

## Task 9 — Full verification and publication

1. Review exact diff and remove unrelated changes.
2. Run RepoForge `full` and require a matching fingerprint receipt.
3. Commit the exact tree with a scoped Conventional Commit.
4. Push without force and create one draft PR with `Closes #103`, `Closes #105`, `Closes #106`, `Closes #104`, `Closes #111`, `Closes #112`, `Closes #113`, and `Closes #114`.
5. Update the checked-in graph from In progress to In review in a follow-up verified commit.
6. Update all eight issues, parents #101/#102, and program #3 master tracking with PR and verification evidence.
7. Read PR checks/status and report exact state. Do not merge.
