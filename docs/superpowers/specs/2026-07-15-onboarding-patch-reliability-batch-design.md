# Onboarding and Patch Reliability Batch Design

**Issues:** #103, #105, #106, #104, #111, #112, #113, #114  
**Parents:** #101, #102  
**Workspace:** one isolated RepoForge worktree

## Goals

Deliver two bounded P0 streams in one reviewable PR:

1. Make setup, onboarding, configuration discovery, and local-first usage internally consistent and usable from a standard installation.
2. Make `workspace_apply_patch` accept both canonical unified diffs and the OpenAI `apply_patch` envelope, repair only deterministic bounded drift, and return actionable structured failures.

## Non-goals

- No new network or GitHub write capabilities.
- No changes to managed tunnel authentication semantics.
- No fuzzy edit-distance patch guessing.
- No fingerprint cache; issue #115 remains separate.
- No audit logging of source or patch bodies. RepoForge records hashes and repair metadata only.

## Architecture

### Configuration paths

Add `domain/user_paths.py` as the single path-resolution authority, with `application/configuration/paths.py` providing the application-facing re-export. The authority derives the absolute editable config path, state root, configuration-generation root, onboarding store, workspace registry, audit log, metrics, managed-runtime log, and diagnostics directory without performing writes.

`rf config path`, `rf doctor`, setup output, onboarding output, and resolved-config defaults consume that shared authority.

### Local-first setup

The editable source configuration represents tunnel capability explicitly:

- `tunnel_id: str | None`;
- a missing `[tunnel]` section means local-only operation;
- `rf setup --local PATH` may create and smoke-test an accepted generation without a tunnel;
- `rf serve` may use the accepted generation directly;
- managed runtime start/reload/restart fail before mutation with a clear requirement for tunnel ID, tunnel client, and API key.

The existing tunnel-enabled path remains unchanged when a tunnel ID is supplied.

### Interactive UI dependencies

`rich` and bounded `InquirerPy` versions become runtime dependencies. Non-TTY and explicit plain UI behavior remains lazy and unchanged.

### Patch normalization

Add a pure deterministic patch model under `domain/patches.py` and an application normalizer under `application/workspace/patch_input.py`.

The normalizer:

1. bounds input size;
2. detects canonical unified diff or OpenAI envelope;
3. extracts and policy-validates every source/destination path;
4. reads only policy-approved regular UTF-8 files from the unchanged workspace snapshot;
5. converts envelope directives into desired file states;
6. applies unified hunks in memory using exact context first and whitespace-normalized context second;
7. accepts only one candidate location;
8. renders a canonical unified diff with recomputed hunk counts and line numbers;
9. sends the canonical diff through the existing mode, symlink, submodule, path, Git check/apply, rollback, and post-apply change-budget gates.

No best-match scoring is used. Missing or ambiguous context fails closed.

### Errors and audit

Add stable patch error codes and bounded structured details. CLI and MCP envelopes expose details such as format, target path, hunk ordinal/header, failure class, and bounded Git stderr.

Audit metadata contains only:

- workspace ID;
- input and normalized SHA-256;
- detected format;
- deterministic repair action names;
- changed paths.

Patch/source bodies never enter audit logs.

## Compatibility

- MCP tool name and arguments remain unchanged.
- Existing valid unified diffs retain semantics.
- Existing optimistic HEAD/fingerprint locks and rollback behavior remain unchanged.
- Existing tunnel-enabled configurations remain parseable.
- Plain/non-interactive onboarding remains available.

## Verification

- Parser and docs-command drift tests.
- Local-only source/config/setup/serve/runtime tests.
- Standard-install UI import and backend-selection tests.
- Patch unit corpus: add/update/delete/move, recount, relocation, whitespace normalization, ambiguity, missing context, denied paths, malformed envelope, deterministic output.
- Real Git integration for whitespace fixing and both input formats.
- MCP structured error contract.
- Full RepoForge production gate, source/wheel build, and installed-wheel E2E.
