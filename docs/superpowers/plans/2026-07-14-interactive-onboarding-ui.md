# Interactive Guided Onboarding UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a visual, low-friction six-stage interactive onboarding workflow with safe defaults, optional Rich/InquirerPy support, plain fallback, and a deterministic dependency-free non-interactive path.

**Architecture:** Keep recommendation policy pure in the application layer, keep display models and diff generation pure in the CLI interface layer, and hide optional terminal packages behind a lazy adapter protocol. The existing coordinator and proposal verifier remain unchanged except for the previously required early tunnel invariant.

**Tech Stack:** Python 3.10+, argparse, Rich as an optional runtime enhancement, InquirerPy as an optional real-terminal selector, pytest, Ruff, Mypy.

## Global Constraints

- `--ui` values are exactly `auto`, `rich`, and `plain`.
- `--defaults` values are exactly `safe`, `ask`, and `none`.
- Interactive default policy is `ask`; non-interactive default policy is `none`.
- Non-interactive execution must not import Rich or InquirerPy.
- Exact proposal approvals are never preselected.
- Apply confirmation defaults to false.
- Coordinator, proposal, persistence, and activation contracts remain authoritative.

---

### Task 1: Fail-closed recommendation policy

**Files:**
- Create: `src/repoforge/application/onboarding/recommendations.py`
- Modify: `src/repoforge/application/onboarding/__init__.py`
- Test: `tests/test_onboarding_recommendations.py`

**Interfaces:**
- Produces: `DecisionRecommendation` and `recommend_safe_decisions(required_decisions)`.

- [x] Write a failing test proving network setup, autofix, risky commands, and single bounded choices are recommended while package-manager and working-directory choices are not guessed.
- [x] Run `pytest tests/test_onboarding_recommendations.py -v` and confirm the missing-module failure.
- [x] Implement the immutable recommendation model and explicit safe-default map.
- [x] Export the public recommendation symbols.
- [x] Run the focused test and confirm it passes.

### Task 2: Optional terminal adapter boundary

**Files:**
- Create: `src/repoforge/interfaces/cli/onboarding_ui.py`
- Test: `tests/test_onboarding_ui.py`

**Interfaces:**
- Produces: `ChoiceItem`, `OnboardingUI`, `PlainOnboardingUI`, `RichOnboardingUI`, `UiBackendUnavailable`, and `build_onboarding_ui`.

- [x] Write failing tests for non-TTY plain fallback without dependency probes, multi-select defaults and numeric selection, explicit Rich failure guidance, Rich panel/table/diff rendering, and auto Rich selection.
- [x] Run the focused tests and confirm import failure.
- [x] Implement the dependency-free plain adapter.
- [x] Implement lazy Rich rendering and optional InquirerPy select/checkbox/confirm prompts.
- [x] Add a regression test and implementation preserving case-sensitive choice values.
- [x] Run `pytest tests/test_onboarding_ui.py -v` and confirm all tests pass.

### Task 3: Review presentation models

**Files:**
- Create: `src/repoforge/interfaces/cli/onboarding_review.py`
- Test: `tests/test_onboarding_review.py`

**Interfaces:**
- Produces: `DefaultsMode`, `resolve_defaults_mode`, `RepositorySummary`, `discovery_rows`, `proposal_summary`, and `configuration_diff`.

- [x] Write failing tests for interactive/non-interactive defaults behavior, discovery formatting, compact fail-closed proposal summaries, and stable diff labels.
- [x] Implement the pure presentation helpers without terminal dependencies.
- [x] Run `pytest tests/test_onboarding_review.py -v` and confirm all tests pass.

### Task 4: Six-stage CLI orchestration

**Files:**
- Modify: `src/repoforge/interfaces/cli/onboarding.py`
- Modify: `tests/test_onboarding_cli.py`

**Interfaces:**
- Consumes: the recommendation, review, and UI interfaces from Tasks 1–3.
- Produces: `--ui`, `--defaults`, and the Discovery → Safe defaults → Ambiguous decisions → Repository summaries → Config diff → Apply workflow.

- [x] Add parser tests for both new options.
- [x] Add a test proving non-interactive execution never constructs an interactive UI.
- [x] Add a test proving non-interactive `--defaults ask|safe` is rejected.
- [x] Replace the concrete terminal dependency with the adapter protocol while retaining `TerminalOperatorIO` as a compatibility alias.
- [x] Reuse the discovery result for duplicate-ID review.
- [x] Add batch safe-default selection, unresolved-decision prompts, compact approval tables, exact approval selection, config diff display, and negative-by-default apply confirmation.
- [x] Make interactive `--plan-only` exit after the reviewed diff without mutation.

### Task 5: Public contract and operator documentation

**Files:**
- Modify: `src/repoforge/interfaces/cli/contract.py`
- Modify: `docs/contracts/release-contract-v1.json`
- Create: `docs/INTERACTIVE_ONBOARDING.md`
- Create: `docs/superpowers/specs/2026-07-14-interactive-onboarding-ui-design.md`
- Create: `docs/superpowers/plans/2026-07-14-interactive-onboarding-ui.md`
- Test: `tests/test_onboarding_contract.py`

**Interfaces:**
- Produces: frozen contract entries for `--ui` and `--defaults` plus operator installation and fallback guidance.

- [x] Write and run a failing contract test.
- [x] Add both options to the generated and frozen CLI contract.
- [x] Document optional installation, mode semantics, batch stages, and deterministic automation behavior.

### Task 6: Verification and patch packaging

**Files:**
- Verify all files listed above.
- Produce: `repoforge-onboarding-tui-complete.patch`.

- [x] Run focused pytest suites for UI, review, recommendation, contract, CLI, and coordinator behavior where the reconstructed repository permits.
- [x] Run Ruff on all changed Python files.
- [x] Run Python bytecode compilation on all changed Python files.
- [x] Run `git diff --check`.
- [x] Validate the cumulative patch with `git apply --check` against the exact reconstructed `fe9b801` baseline.
