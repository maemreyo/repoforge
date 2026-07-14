# Snapshot-Consistent Workspace Assessment Design

## Context

Issue #14 requires one read-only application transaction that composes existing workspace, policy, base, pull-request, CI, and verification evidence against a single exact identity. Missing providers must remain explicit, while any identity mutation during collection invalidates the whole result.

## Decision

Add an internal `WorkspaceAssessmentReader`; no MCP tool is added. The reader captures an `AssessmentSnapshot`, invokes existing typed readers, and revalidates the snapshot after every provider boundary and once before return.

The immutable identity contains:

- workspace ID;
- exact HEAD SHA;
- workspace fingerprint;
- deterministic configuration generation (SHA-256 of the active config file bytes);
- repository policy hash;
- creation timestamp;
- deterministic snapshot ID over the preceding fields.

## Domain model

`AssessmentEvidence` wraps every component with:

- `status`: `current`, `partial`, `unavailable`, or `not_applicable`;
- `coverage`: `complete`, `partial`, or `none`;
- bounded typed value;
- stable error code;
- safe fallback description.

`WorkspaceAssessment` contains the required changed paths, diff summary, change budget, path policy, base freshness, PR state, CI summary, failure-evidence references, verification-receipt freshness, evidence coverage, and uncertainties.

Domain validation requires every successful component identity to equal the assessment snapshot. Ordering is deterministic and paths are already filtered by existing policy-aware Git readers.

## Collection algorithm

1. Resolve workspace/repository/path and acquire the workspace lock.
2. Capture exact snapshot identity.
3. Read workspace status, then revalidate identity.
4. Read bounded diff summary, then revalidate identity.
5. Compute path-policy and change-budget evidence from policy-safe changed paths, then revalidate identity.
6. Read base freshness; provider failure becomes explicit partial/unavailable evidence; revalidate identity.
7. Read PR state; no PR/auth failure becomes explicit unavailable/not-applicable evidence; revalidate identity.
8. Read CI checks and derive exact failed Check Run selectors; revalidate identity.
9. Derive receipt freshness from workspace status.
10. Revalidate identity immediately before returning.

Any HEAD, fingerprint, config generation, or policy-hash change raises `STALE_ASSESSMENT_SNAPSHOT`; no current assessment is returned.

## Safety and bounds

- Read-only orchestration only; no fetch outside existing base-status behavior, no write, no receipt creation.
- Diff bodies are not returned by the assessment shell; only bounded stat/truncation metadata.
- Paths come only from existing policy-filtered readers.
- Provider exceptions are converted to stable codes and safe fallback text, never raw external payloads.
- Failure evidence is represented by at most twenty exact `check-run:<id>` references.
- Audit records snapshot ID, workspace ID, coverage, and stable error codes only.

## Non-goals

No semantic symbol analysis, architecture rules, CodeGraph, final risk score, execution plan, caching, commit eligibility, or public MCP surface.