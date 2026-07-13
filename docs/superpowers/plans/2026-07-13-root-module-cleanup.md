# RepoForge Root Module Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the six reported regressions, remove obsolete root-level implementations, and wire all active consumers to the layered packages introduced in Phases 1–5.

**Architecture:** Preserve `bootstrap.py` as the only production composition root. Move the active service facade into `application/`, move the runtime worker into `interfaces/runtime/`, delete compatibility facades and duplicate use cases, and enforce the resulting package shape with AST-based tests.

**Tech Stack:** Python 3.10+, argparse, Unix-domain sockets, Ruff, strict Mypy, pytest, pytest-cov, Hatchling/uv.

## Global Constraints

- Do not restore locking to the persistence-only `WorkspaceStore` port.
- Do not execute detected repository scripts during inspection.
- Preserve capability-delta fail-closed behavior for mixed expansion and restriction.
- Unix control sockets must remain owner-only and work on Darwin and Linux path limits.
- Coverage must remain at or above 80% branch coverage.

---

### Task 1: Correct semantic profile capability deltas

**Files:** `src/repoforge/domain/config_generation.py`, `tests/test_domain_config_generation_delta.py`

- [x] Add failing tests for command addition, removal, replacement, and description-only edits.
- [x] Compare profile commands field-by-field instead of replacing a serialized profile set.
- [x] Verify addition is expansion, removal is restriction, replacement is incompatible, and description is metadata-only.

### Task 2: Make Unix control paths portable

**Files:** `src/repoforge/adapters/runtime/unix_control.py`, `tests/test_phase4_runtime_control.py`

- [x] Add a failing long logical socket path test.
- [x] Derive a deterministic hashed bind path in a user-private temporary directory when required.
- [x] Use the same resolver in server and client, retain logical paths for diagnostics, and remove the bound path on close.

### Task 3: Remove root compatibility and duplicate modules

**Files:** `src/repoforge/application/service.py`, `src/repoforge/interfaces/runtime/worker.py`, `src/repoforge/__main__.py`, `pyproject.toml`, imports and architecture tests.

- [x] Move `CodingService` into the application layer.
- [x] Move the supervisor worker into the runtime interface package.
- [x] Point console and module entry points directly at canonical interfaces.
- [x] Delete root facades, superseded onboarding code, and duplicate workspace use cases.
- [x] Replace or delete tests that target removed implementations.
- [x] Add an exact root-module allowlist and removed-import scan.

### Task 4: Verify and package transactionally

- [x] Run Ruff format and lint.
- [x] Run strict Mypy.
- [x] Run the complete available suite and branch coverage gate.
- [ ] Apply the bundle to a simulated `dev@f2c6041` working tree containing the previous locking patch.
- [ ] Verify rollback behavior and package checksums.
