# Testing strategy

RepoForge has write access to local code and GitHub branches, so every tool needs automated tests.
The suite intentionally tests behavior at several layers instead of only checking that functions import.

## Current gates

```bash
./scripts/test-all.sh
```

The gate runs:

- Ruff;
- strict Mypy;
- Pytest with branch coverage, minimum 80%;
- package build.

## Test layers

1. **Unit/config/security:** validation, denied paths, path escape, patch parsing, symlink/submodule rejection.
2. **Repository discovery/CLI:** JavaScript, Python, Rust, Go and generic detection; init, doctor,
   smoke-test, audit, tunnel command and error handling.
3. **Local Git integration:** bare remote + clone + real worktree + edit + verification receipt + commit + push.
4. **Fake GitHub CLI integration:** deterministic issue/PR reads, draft PR create/edit/status/checks, labels and reviewers without touching a real account.
5. **In-memory MCP protocol:** actual MCP client lists tools, checks schemas/annotations, invokes all 31 tools and validates tool errors.
6. **Committed snapshot integration:** real Git objects prove exact branch/commit identity, dirty-clone isolation, deterministic ordering, truncation, and ref/path/object rejection.
7. **Negative regression:** stale SHA, stale fingerprint, post-verification edits, batch limit, denied workflow path and change-budget enforcement.

## Live checks still required on the recipient machine

Automated tests deliberately do not use the recipient's GitHub credentials or OpenAI tunnel. Before
first real coding task, run:

```bash
rf doctor --fix
rf smoke-test --repo-id work-frontier
./scripts/inspect-mcp.sh
```

Then run the prompts in `docs/testing/PLUGIN_TEST_CASES.md` against the actual ChatGPT Plugin. Confirm that
write actions request approval and that the final PR remains draft.
