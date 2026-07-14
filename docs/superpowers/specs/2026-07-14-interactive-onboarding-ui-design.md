# Interactive Guided Onboarding UI Design

## Purpose

Make `rf onboard` fast to review without weakening its explicit approval model. The interactive path presents one bounded batch workflow, proposes only fail-closed defaults, and keeps ambiguous or capability-expanding choices under operator control. The non-interactive path remains deterministic and independent of terminal UI packages.

## Architecture

The feature has three boundaries:

1. `application/onboarding/recommendations.py` owns pure, fail-closed recommendation policy. It does not import CLI or terminal libraries.
2. `interfaces/cli/onboarding_review.py` builds presentation-only discovery rows, compact repository summaries, defaults-mode semantics, and deterministic source-config diffs.
3. `interfaces/cli/onboarding_ui.py` provides interchangeable plain and Rich terminal adapters. Rich and InquirerPy are discovered and imported lazily. `onboarding.py` orchestrates the workflow through the adapter protocol without changing coordinator or proposal contracts.

No UI adapter may create proposals, verify approvals, mutate configuration, activate a runtime, or infer an ambiguous security decision.

## Backend Selection

`--ui` accepts:

- `auto`: use Rich when installed and attached to a TTY; otherwise use plain output.
- `rich`: require Rich on a TTY and return an actionable error when unavailable. InquirerPy is used automatically when installed on the real terminal.
- `plain`: use the dependency-free terminal adapter.

Non-TTY input always uses the plain adapter. The module itself has no static import of Rich or InquirerPy, so importing or running the non-interactive CLI does not require either package.

## Defaults Policy

`--defaults` accepts:

- `safe`: apply all available fail-closed recommendations without asking separately.
- `ask`: show available recommendations as a multi-select list with safe items preselected.
- `none`: do not apply recommendations; ask every unresolved decision.

Interactive mode defaults to `ask`. Non-interactive mode defaults to `none` and rejects explicit `safe` or `ask`, because automation must supply exact `--decision`, `--policy-override`, and `--approve` inputs.

Safe defaults may disable publishing, dependency setup, autofix, risky commands, writable LFS/submodule behavior, or policy replacement. They never select among multiple remotes, package managers, base branches, monorepo scopes, or working directories.

## Batch Workflow

The interactive flow is fixed at six stages:

1. **Discovery** — table of eligible repositories and exclusions, followed by duplicate-ID resolution.
2. **Safe defaults** — apply or review bounded recommendations according to `--defaults`.
3. **Ambiguous decisions** — ask only unresolved choices and collect scoped working-directory overrides.
4. **Repository summaries** — show compact policy summaries and select repositories for exact proposal approval. Approval choices are never preselected.
5. **Config diff** — show a unified diff from the current source configuration to the proposed source configuration plus batch capability impact.
6. **Apply** — require a negative-by-default confirmation. `--plan-only` exits after the diff with no mutation.

The coordinator remains the authority for session transitions, exact approval verification, candidate smoke tests, atomic acceptance, and activation.

## Failure and Fallback Behavior

- Explicit `--ui rich` without Rich produces a stable operator-facing error with installation and `--ui plain` recovery actions.
- Missing InquirerPy does not fail Rich mode; prompts fall back to Rich Prompt and the plain multi-select parser.
- Non-TTY interactive invocation fails with the existing instruction to use `--non-interactive` and explicit inputs.
- Invalid config diff reads are treated as an empty current source for display only; the coordinator still enforces source-generation guards before mutation.
- Pausing preserves the resumable session and never stores raw approval tokens.

## Testing

Tests cover backend selection without optional dependency imports, plain multi-select behavior, Rich rendering without InquirerPy, defaults-mode isolation, stable config diff labels, compact fail-closed summaries, recommendation boundaries, tunnel validation before review, parser and frozen CLI contract changes, and non-interactive avoidance of UI construction.
