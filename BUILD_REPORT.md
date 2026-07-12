# Build report

Version: 2.0.1
Prepared: 2026-07-12
Product name: RepoForge
Python package: `repoforge-mcp`

## Release scope

- bounded local repository scanning through `rf scan-repos`;
- multi-repository config generation through repeatable `init --scan-root`;
- deterministic collision handling for duplicate repository names;
- Makefile-aware command/profile detection;
- safer Python/uv profile detection;
- hand-reviewed `config.repoforge.toml` for self-hosted development;
- GitHub Actions quality matrix for Python 3.10 and 3.13;
- corrected Work Frontier profile documentation;
- prior MCP Inspector and runtime-artifact fixes.

## Safety properties of repository scanning

- explicit roots only;
- bounded depth and repository count;
- no symlink traversal;
- dependency, virtual-environment, cache, build, VCS, and hidden-directory exclusions;
- read-only metadata/manifests during scanning;
- no MCP scan tool, worktree creation, branch push, or pull-request write;
- generated commands require operator review before config installation.

## Validation performed for this patch

- Python compile check: passed for source and tests.
- TOML parsing: passed for `pyproject.toml`, `config.example.toml`,
  `config.work-frontier.toml`, and `config.repoforge.toml`.
- Shell syntax: passed for bootstrap, test, Inspector, E2E preflight, and tunnel scripts.
- Targeted discovery/CLI tests: 10 passed, including Makefile detection, bounded scanning,
  exclusions, deterministic IDs, multi-repository rendering, and CLI generation.
- The complete locked suite is configured in `.github/workflows/ci.yml` and must pass before release.

## Environment limitation

The packaging environment could not resolve external Python dependencies, so Ruff, strict Mypy,
the complete MCP protocol suite, and distribution rebuild were not rerun here. The operator's prior
RepoForge L0-L5 validation passed for 2.0.0; the new 2.0.1 scan/config patch still requires the new CI
run and a local `./scripts/test-all.sh` before tagging.
