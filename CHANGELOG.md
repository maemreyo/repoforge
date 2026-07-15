# Changelog

## Unreleased

- Wired `repo_list`, `workspace_list`, `repo_issue_graph`, `repo_issue_next`, and `workspace_pr_watch` registration through `ApplicationContext.audited` with bounded, secret-free details (counts and identifiers only), so `rf audit`/`rf audit stats` now see every public read and write tool.
- Added bounded 30-day daily buckets alongside lifetime totals in `operation-metrics.json`, migrated compatibly from schema version 1, and extended `rf audit stats` with `--since`/`--until` window filters so a fix's before/after effect on a tool's failure rate or latency can be measured locally; default `rf audit stats` output is unchanged.
- Added optional bounded `issue_ids` workspace metadata to `workspace_create`, surfaced in `workspace_list` and `workspace_status` alongside dirty/clean state, plus a documented one-issue-per-workspace default with a stacked-issue exception.
- Added local `rf audit` and `rf audit stats` commands to read the existing private audit log and operation-metrics snapshot, with `--last`, `--action`, `--failed`, and `--slow` filters, so a failed or slow consumer call can be found and timed without new instrumentation.
- Added AI-ready issue forms, a deterministic machine-readable ticket graph, offline validation, Ready-ticket selection, and bounded read-only GitHub drift checks.
- Added snapshot-bound explainable workspace risk and policy-driven ordered verification recommendations while retaining the final exact-tree gate.
- Added reusable typed durable-state envelopes and private atomic JSON storage, adopted by OperationTask without changing its serialized record contract.
- Replaced the obsolete manual source hash manifest with a documented executable source and release integrity policy.
- Added deterministic size-balanced pytest sharding with combined branch coverage so the complete production gate remains exact-tree bound and connector-friendly.
- Added local-first setup and serve, standard-install interactive onboarding dependencies, deterministic config/state path discovery, written-file summaries, and executable docs/script drift coverage.
- Added dual-format atomic patch application for unified diffs and OpenAI apply_patch envelopes, deterministic hunk repair, actionable structured failures, and whitespace-check/apply parity without logging patch bodies.

## 2.0.0 — 2026-07-12

- Renamed product to RepoForge and CLI to `repoforge` / `rf`.
- Added repository discovery, config generation, doctor fixes, smoke testing and tunnel command output.
- Added Work Frontier configuration at the requested local path.
- Expanded MCP surface from 21 to 27 focused tools.
- Added repository context, batch reads, path restore, default verification, change budgets, PR edit and CI checks.
- Added PR labels/reviewers/no-maintainer-edit options.
- Added uv lockfile and full development/testing documentation.
- Added complete protocol/integration/security test suite with an 80% coverage gate.
