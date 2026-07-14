# Build report

Version: 2.0.0  
Packaged: 2026-07-12  
Product name: RepoForge  
Python package: `repoforge-mcp`

## Implemented DX improvements

- repository auto-detection and config generation;
- actionable doctor and deterministic config/state path discovery;
- local-first setup and MCP stdio verification;
- managed tunnel command generation;
- `uv.lock`, Makefile and reproducible test/build scripts;
- repository context and batch reads;
- default verification profile;
- path restore and change budgets;
- configurable PR labels/reviewers/no-maintainer-edit;
- draft PR update and compact CI check buckets;
- richer tool descriptions, annotations and stable structured outputs;
- golden prompt and plugin regression test documentation.

## Current hardening additions

- deterministic 80-node ticket graph validation, Ready-ticket selection, AI-ready issue forms, and optional bounded read-only GitHub drift checks;
- snapshot-bound explainable workspace risk with policy-driven ordered verification recommendations;
- shared typed durable-state envelopes and private atomic JSON storage adopted by OperationTask without changing its serialized record contract;
- executable source and release integrity policy replacing the obsolete manually maintained source hash inventory;
- local-first setup/serve, standard-install Rich/InquirerPy onboarding, path provenance, and written-file summaries;
- deterministic dual-format patch normalization with structured repair evidence and no patch-body audit storage.

## Validation in build environment

- Ruff: passed.
- Mypy strict: passed for 12 source modules.
- Pytest: 24 tests passed.
- Branch coverage: 80.74% (minimum gate: 80%).
- MCP contract: exactly 27 tools discovered with descriptions, schemas and annotations.
- MCP protocol lifecycle: all 27 tools invoked through an in-memory MCP client.
- Git integration: real local bare remote, clone, worktree, edit, verify, commit, non-force push and cleanup.
- GitHub integration: deterministic fake `gh` issue/PR/create/edit/status/check lifecycle.
- Negative/security paths: stale locks, denied workflow paths, verification invalidation and change budgets.
- Shell scripts: `bash -n` passed.
- Python distribution: wheel and source archive built successfully.
- Plugin icon: PNG, 256 × 256.

## Not executed during packaging

- a live OpenAI Secure MCP Tunnel session;
- a real external repository beyond the isolated test fixtures;
- a real GitHub push or PR during package-only verification.

Those checks require an operator-owned checkout, GitHub login, and—only for managed runtime use—a tunnel ID and API key. Run `rf config path`, `rf doctor`, `rf repo list`, then the golden cases in `docs/testing/PLUGIN_TEST_CASES.md` before the first real task.
