# Phase 8 Program Completion and Release Gates

Phase 8 closes plan-wide gaps that remained after runtime and hot-reload implementation. It does not
add a new execution capability. It makes the existing system reproducible, reviewable, and releasable
from a clean checkout.

## Fast-exit log finalization

A short-lived tunnel child can exit between the first `poll()` and process-identity inspection. The
managed client now rechecks process state before reporting completion. If the child exited, it invokes
the bounded finalizer and does not return `False` while the log-pump thread remains tracked. This
prevents callers from observing zero log files or missing the final oversized-line omission marker.

## Frozen public contract

`docs/contracts/release-contract-v1.json` is generated from the installed implementation and freezes:

- package version;
- human source-config and resolved snapshot versions;
- runtime-control and diagnostics protocol versions;
- MCP server instruction hash and tool-surface hash;
- every MCP tool name, title, description, annotation, input schema, and output schema.

`scripts/check_release_contracts.py` compares generated output with the reviewed fixture and fails with
a unified diff. `--write` exists only for intentional, documented compatibility changes.

## Production gate

`scripts/verify-production.sh` records the exact HEAD, optionally requires a completely clean tracked, staged, and untracked tree,
and runs:

1. frozen dependency synchronization;
2. release-contract drift checks;
3. Ruff format and lint;
4. strict Mypy;
5. full branch-coverage pytest gate;
6. wheel and source-distribution build;
7. isolated wheel installation and CLI/MCP smoke verification;
8. a real temporary bare-remote, worktree, verify, commit, push, and cleanup lifecycle using only the installed wheel.

`.github/workflows/production-gate.yml` repeats the source gate on Python 3.10–3.13 and includes a
macOS Python 3.13 lane for Darwin Unix-socket and process-lifecycle behavior. The local gate redirects bytecode, coverage, Ruff, and Mypy caches into a temporary directory and verifies that a clean run leaves no repository artifacts behind.

## Compatibility

`scripts/run-tunnel.sh` remains an executable compatibility entry point and delegates to `rf start`,
which is the foreground supervisor alias. Minimal-v2 and legacy-v1 config fixtures remain frozen in
the test suite. Contribution guidance requires narrowly scoped Conventional Commits and explicit
review for public contract updates.
