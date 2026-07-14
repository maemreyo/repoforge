# Structured CI Failure Evidence Design

## Context

Issue #7 requires two read-only workspace operations that turn a RepoForge-issued CI selector into bounded, secret-safe GitHub check evidence. The implementation must preserve the existing compact `workspace_pr_checks` contract, bind every result to the exact pushed workspace SHA, and keep all GitHub API/CLI parsing inside the GitHub adapter.

## Considered approaches

### 1. Enrich existing `gh pr checks` results with REST Check Run identities — recommended

Keep `gh pr checks` as the compatibility source for name/state/bucket/link/workflow. Resolve the PR head SHA, list Check Runs for that exact commit through `gh api`, and match each compact check to a Check Run. Add an opaque `check-run:<id>` selector and exact `head_sha` without removing existing fields. The new tools use only that selector.

This preserves existing behavior, supports external Check Runs as well as GitHub Actions jobs, and avoids a brittle replacement of the compact status parser.

### 2. Replace `workspace_pr_checks` with a custom GraphQL query

A single query could expose Check Run database IDs and PR identity, but it would replace stable behavior, duplicate GitHub CLI status bucketing, and couple the adapter to a larger GraphQL schema. This is rejected for compatibility and maintenance risk.

### 3. Parse only Actions run/job IDs from check URLs

This is simple but excludes non-Actions Check Runs and makes URLs an accidental public identifier. This is rejected because selectors must be typed and provider-neutral at the public boundary.

## Public contract

`workspace_pr_checks` retains all existing fields and adds:

- result-level `head_sha`, `pushed_sha`, and `stale`;
- per-check `selector`, `check_run_id`, `head_sha`, and `selector_available` when Check Run enrichment succeeds.

New operations:

```text
workspace_pr_check_details(workspace_id, check_selector)
workspace_pr_failure_evidence(workspace_id, check_selector, max_excerpt_lines=80)
```

Only selectors matching `check-run:<positive integer>` are accepted. The model cannot provide URLs, API paths, or arbitrary `gh` arguments.

## Architecture and data flow

1. The workspace use case loads the registry record and repository policy.
2. It requires `last_pushed_sha`, verifies current local HEAD still equals that SHA, and parses the selector.
3. The typed GitHub port loads the exact Check Run, bounded annotations, and optional GitHub Actions job metadata.
4. The use case rejects a Check Run whose `head_sha` differs from the pushed workspace SHA with a stable stale-evidence error.
5. Details return identity, status, attempt, failed step, annotations, source URL, and evidence availability.
6. Failure evidence prefers annotations, then Check Run output and failed-step metadata, then a bounded job log.
7. Model-visible text passes through CI-specific redaction and denied-path withholding before excerpt selection and hashing.

The GitHub adapter owns all subprocess argv, REST endpoint construction, JSON parsing, link parsing, retries permitted by the existing command executor, and source-size bounds. MCP handlers remain thin service calls.

## Typed models

The GitHub port gains immutable models for:

- `GitHubCheckRun`;
- `GitHubCheckAnnotation`;
- `GitHubActionsJob` and `GitHubActionsStep`;
- `GitHubJobLog`.

The public selector remains an opaque string; internal parsing returns a positive Check Run ID.

## Evidence classification

The application layer classifies sanitized evidence into:

- `test`, `lint`, `type`, `build`, `dependency`, `environment`, `timeout`, `policy`, `network`, `cancellation`, or `unknown`.

Classification is deterministic and ordered from explicit cancellation/timeout/policy signals through tool-specific failures to unknown. Network, timeout, environment, and cancellation failures are retryable; other classes default to non-retryable.

## Secret-safe egress

CI text processing:

- reuses existing assignment, bearer-token, credential-URL, and explicit-secret redaction;
- removes complete private-key blocks;
- replaces high-confidence long entropy-bearing tokens;
- withholds complete lines that expose paths denied by repository policy;
- bounds source characters, annotation count, excerpt lines, and final tool output;
- records only selector IDs, counts, status, and truncation in audit metadata—never excerpts or raw API errors.

`excerpt_sha256` is computed after redaction and withholding so it identifies exactly what the model received.

## Partial evidence and errors

The Check Run identity is required. Annotation, job-metadata, and log failures are non-fatal for `workspace_pr_failure_evidence`; the result contains `coverage`, `uncertainty`, and stable `source_errors`. Passing, pending, skipped, and cancelled checks return deterministic evidence availability rather than pretending a failure excerpt exists.

Stable errors cover invalid selectors, missing pushed state, stale workspace/check SHAs, and unavailable primary Check Run evidence.

## Testing

Tests use only temporary repositories and deterministic fake `gh` responses. Coverage includes:

- selector enrichment and compatibility of `workspace_pr_checks`;
- exact SHA binding and stale rejection;
- annotations, failed steps, multiple attempts, and log fallback;
- pass, pending, cancelled, and retried checks;
- missing/forbidden logs and partial coverage;
- private keys, credentials, high-entropy secrets, and denied source snippets;
- large log and annotation truncation;
- service and in-memory MCP invocation/annotations;
- release-contract and tool-count updates.

Final verification is `scripts/verify-production.sh --allow-dirty` through RepoForge exact-tree `full` verification before commit.
