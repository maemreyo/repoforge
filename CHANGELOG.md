# Changelog

## 2.0.1 — 2026-07-12

- Added bounded local repository scanning with `rf scan-repos` and multi-repository `init --scan-root`.
- Added Makefile-aware detection and safer Python/uv profile generation.
- Added `config.repoforge.toml` for self-hosted RepoForge development.
- Added GitHub Actions CI for Python 3.10 and 3.13, strict typing, tests, coverage, MCP contracts, and builds.
- Corrected MCP Inspector argument separation and ignored continuation runtime artifacts.
- Added full-flow, test-record, repository-discovery, and pull-request documentation.
- Corrected Work Frontier README profiles to match the canonical Make targets.

## 2.0.0 — 2026-07-12

- Renamed product to RepoForge and CLI to `repoforge` / `rf`.
- Added repository discovery, config generation, doctor fixes, smoke testing and tunnel command output.
- Added Work Frontier configuration at the requested local path.
- Expanded MCP surface from 21 to 27 focused tools.
- Added repository context, batch reads, path restore, default verification, change budgets, PR edit and CI checks.
- Added PR labels/reviewers/no-maintainer-edit options.
- Added uv lockfile and full development/testing documentation.
- Added complete protocol/integration/security test suite with an 80% coverage gate.
