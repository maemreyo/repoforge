# Build report

Version: 2.0.0  
Packaged: 2026-07-12  
Product name: RepoForge  
Python package: `repoforge-mcp`

## Implemented DX improvements

- repository auto-detection and config generation;
- actionable doctor with optional safe fixes;
- non-mutating smoke test;
- tunnel command generation;
- Work Frontier config for `/Users/trung.ngo/Documents/zaob-dev/work-frontier`;
- `uv.lock`, Makefile and reproducible test/build scripts;
- repository context and batch reads;
- default verification profile;
- path restore and change budgets;
- configurable PR labels/reviewers/no-maintainer-edit;
- draft PR update and compact CI check buckets;
- richer tool descriptions, annotations and stable structured outputs;
- golden prompt and plugin regression test documentation.

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

- the recipient's actual `/Users/trung.ngo/Documents/zaob-dev/work-frontier` filesystem;
- a live OpenAI Secure MCP Tunnel session;
- a real GitHub push or PR using the recipient's credentials;
- Work Frontier's real `pnpm` verification commands.

Those checks require the recipient's Mac, local checkout, GitHub login and tunnel ID. Run `rf doctor`,
`rf smoke-test`, then the golden cases in `docs/PLUGIN_TEST_CASES.md` before the first real task.
