# Typed Diagnostic Profiles and Constrained Selectors Design

## Context

Issue #11 requires RepoForge to run narrow, repository-approved diagnostics without asking the model to edit `Makefile`, scripts, or verification profiles. The public caller chooses only a configured `diagnostic_id` and an optional typed selector. RepoForge resolves the exact argv, working directory, timeout, parser, output bound, and mutability policy server-side.

This is a diagnostic capability, not a second generic command runner and not a replacement for final exact-tree verification.

## Considered approaches

### 1. Typed repository diagnostics with server-side argv resolution — selected

Add a dedicated `diagnostics` map to each repository configuration. Each profile is an immutable typed value containing the diagnostic ID, argv template, selector schema, working directory, timeout, local-only network declaration, mutability, parser, output limit, and optional artifact path patterns.

The application use case validates the workspace and expected fingerprint, resolves a selector into one bounded argv token, executes through the existing constrained command port, compares exact pre/post workspace state, parses bounded output, invalidates stale verification evidence when files change, and returns structured next actions.

This keeps policy in config/domain code, execution in the subprocess adapter, orchestration in application code, and MCP as a thin adapter.

### 2. Extend `workspace_run_profile` with free-form selector arguments — rejected

This would turn an allowlisted profile into a partially model-authored argv surface. It would also mix setup/build/fix/verification semantics with diagnostic parsing and mutation detection.

### 3. Hard-code one MCP tool per diagnostic — rejected

This avoids generic argv but duplicates policy and parsing in the interface layer, makes repository-specific diagnostics impossible, and produces an unnecessarily broad tool surface.

## Public contract

```text
workspace_run_diagnostic(
  workspace_id,
  diagnostic_id,
  selector?,
  expected_fingerprint?
)
```

The public caller never supplies argv, executable, environment, working directory, parser, timeout, output limit, or artifact paths.

## Configuration model

Each `RepositoryConfig` gains:

```text
diagnostics: dict[str, DiagnosticProfileConfig]
```

`DiagnosticProfileConfig` contains:

```text
diagnostic_id
summary
argv_template
selector
working_directory?
timeout_seconds
network_policy: local_only
mutability: read_only | artifacts
parser: pytest | release_contract | text
output_limit
artifact_paths
```

`argv_template` is one non-empty string array. It may contain the placeholder `{selector}` at most once and only when the selector schema is not `none`. No other braces or placeholders are accepted.

The initial selector schemas are:

```text
none
tracked_path
pytest_node
package_name
enum
check_id
```

A selector schema may also define a bounded enum allowlist. `tracked_path` and the path part of `pytest_node` must resolve to policy-allowed tracked repository content. `package_name`, `check_id`, and enum selectors use closed character and length rules. Selectors cannot begin with `-`, contain NUL/control characters, use shell metacharacters, escape with `..`, or expand into multiple argv elements.

The initial network policy is `local_only`. It means RepoForge supplies no additional network credentials or free-form environment and only runs the reviewed local argv. It does not claim an operating-system network sandbox; future execution-environment work may add one without changing this profile contract.

## Configuration compatibility and review

Existing configurations remain valid because `diagnostics` defaults to an empty mapping.

Resolved-config rendering, semantic delta classification, repository listing, and doctor output understand diagnostics:

- adding a diagnostic or widening selector values/artifact paths is a capability expansion;
- removing a diagnostic or narrowing selectors/artifact paths is a restriction;
- changing executable/argv template, parser, mutability, network policy, or working directory is incompatible and requires explicit review;
- increasing timeout or output limit is an expansion; decreasing either is a restriction;
- descriptions are metadata-only.

Guided onboarding does not infer diagnostics in this ticket. They must be configured through an explicitly reviewed source configuration.

## Built-in reviewed examples

The RepoForge development configuration and tests represent at least:

1. `pytest-target` — argv template `uv run --extra dev pytest {selector} -q`, selector `pytest_node`, parser `pytest`, read-only.
2. `release-contract-diff` — argv `uv run --extra dev python scripts/check_release_contracts.py`, selector `none`, parser `release_contract`, read-only.

These are configuration examples, not hard-coded public capabilities. Another repository may define different diagnostics using the same closed profile schema.

## Application flow

1. Resolve workspace, repository policy, and diagnostic profile.
2. Acquire the workspace lock.
3. Reload the workspace record under the lock.
4. Compute the pre-execution fingerprint and changed-path set.
5. If `expected_fingerprint` is present and differs, fail before execution.
6. Validate and resolve the selector to exactly one argv token or no token.
7. Resolve and validate the configured working directory inside the workspace.
8. Run the configured argv through `CommandExecutor.run` with `check=False`, the configured timeout, no extra environment, and the configured output limit.
9. Compute post-execution fingerprint and changed paths.
10. Classify newly changed paths relative to the pre-execution changed-path set.
11. Enforce mutability policy.
12. Clear any stored verification receipt whenever the fingerprint changed.
13. Parse the bounded result and return structured diagnostics, failure class, excerpt, refreshed fingerprint, changed paths, and next actions.

The use case records only safe metadata in audit: workspace ID, diagnostic ID, selector kind, return code, parser, mutability, changed-path count, failure class, and fingerprints. It never audits selector bodies when they can contain repository paths, stdout/stderr, excerpts, or source content.

## Mutation policy

### Read-only

The post-execution fingerprint must equal the pre-execution fingerprint. Any mutation fails with `DIAGNOSTIC_UNEXPECTED_MUTATION`, reports every policy-visible changed path, invalidates stale verification evidence, and recommends reviewing/restoring the paths.

### Artifacts

Every newly changed path must match at least one configured `artifact_paths` pattern and pass repository path policy. The result reports all changed paths and whether the change budget remains valid. A changed path outside the reviewed artifact patterns fails closed as `DIAGNOSTIC_UNEXPECTED_MUTATION`.

Diagnostics never automatically restore files.

## Parsers and failure classification

Parsers are typed adapters over `CommandResult`; they do not execute commands.

### `pytest`

Returns outcome, passed/failed/error/skipped counts when present, and a bounded excerpt. Non-zero output is classified as `test_failure`, `dependency_missing`, `environment_mismatch`, `timeout`, `tool_missing`, or `parser_failure` using deterministic markers.

### `release_contract`

Recognizes contract match, reviewed drift, malformed output, and tool/dependency failures. Contract drift is a diagnostic failure and does not update golden files.

### `text`

Returns return code, bounded combined excerpt, and a deterministic generic failure class.

All parsers operate on already bounded output. Parser failure is explicit and never converted to success.

## Error model

Stable errors cover:

```text
DIAGNOSTIC_NOT_FOUND
DIAGNOSTIC_SELECTOR_REQUIRED
DIAGNOSTIC_SELECTOR_INVALID
DIAGNOSTIC_STALE_WORKSPACE
DIAGNOSTIC_TOOL_MISSING
DIAGNOSTIC_TIMEOUT
DIAGNOSTIC_PARSER_FAILED
DIAGNOSTIC_UNEXPECTED_MUTATION
DIAGNOSTIC_OUTPUT_INVALID
```

Existing command/config/workspace errors remain compatible where they are already the authoritative error.

## MCP annotation

`workspace_run_diagnostic` is a local, non-destructive mutation-capable operation because artifact-producing profiles may intentionally create reviewed files and read-only profiles must detect accidental mutation. The result declares the selected profile mutability. MCP annotations do not vary dynamically and therefore use the conservative local mutation annotation.

## Result model

```text
workspace_id
diagnostic_id
summary
selector_kind
resolved_selector?
argv
working_directory
network_policy
mutability
parser
returncode
outcome
failure_class?
parsed
excerpt
output_truncated
fingerprint_before
fingerprint_after
fingerprint_changed
changed_paths
unexpected_paths
change_metrics
verification_invalidated
next_safe_actions
```

Output, changed paths, parsed fields, and next actions are bounded and deterministically ordered.

## Testing

TDD coverage includes:

- config loading, invalid templates, duplicate/missing placeholders, invalid enums, timeouts, output limits, mutability, artifact patterns, and compatibility defaults;
- semantic capability-delta classification and deterministic resolved TOML rendering;
- tracked pytest path and node selectors, traversal, denied paths, untracked files, leading dash, shell characters, package/check/enum selectors;
- exact argv and cwd resolution without shell expansion;
- missing executable, timeout, dependency/environment failure, parser failure, non-zero diagnostics, and output truncation;
- stale expected fingerprint and no-command execution on stale state;
- read-only unchanged success and unexpected mutation failure;
- artifact mutation reporting, allowlist enforcement, change-budget reporting, and verification receipt invalidation;
- service wiring and actual MCP protocol invocation/annotations;
- golden release contract and documentation updates.

Final verification is the repository `full` profile, including release-contract validation, Ruff, strict Mypy, all tests and coverage, source/wheel builds, and installed-wheel lifecycle smoke, followed by RepoForge exact-tree verification before commit.

## Deferred work

- arbitrary environment variables or argv;
- operating-system network sandboxing;
- automatic diagnostic discovery from repository scripts;
- remote diagnostics and GitHub workflow administration;
- automatic source fixes;
- replacing final verification or granting commit eligibility;
- durable background diagnostic execution.
