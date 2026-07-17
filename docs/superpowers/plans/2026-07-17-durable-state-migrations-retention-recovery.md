# Durable State Migrations, Retention, and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete issues #72, #73, and #74 as one layered durable-state lifecycle platform with safe migrations, reference-aware cleanup, integrity diagnostics, and operator-controlled backup/restore.

**Architecture:** Extend the shared #71 envelope/store substrate with pure typed lifecycle contracts in the domain, one constrained JSON lifecycle adapter for raw collection administration, and no new MCP resources or tools. Every mutating operation is preview-bound, validates exact source checksums before apply, writes private recovery evidence before destructive changes, and is idempotent on repeated plan tokens.

**Tech Stack:** Python 3.10+, frozen dataclasses, enums, Protocol/Callable contracts, pathlib, deterministic JSON/SHA-256, atomic replace/fsync, pytest, Ruff, strict Mypy, RepoForge verification profiles.

## Global Constraints

- Work only in workspace `durable-state-migrations-53d7c25dde` on its `ai/*` branch.
- Preserve AGENTS.md and SECURITY.md invariants; never persist source bodies, patches, secrets, raw logs, or arbitrary environment data.
- Add no third-party dependencies and no MCP tool/resource surface.
- All collection names, record IDs, scan counts, encoded sizes, output findings, and operation batches are bounded.
- Unknown or future schema versions fail closed; migrations may not skip versions.
- Reverse migration is available only when every reverse step is explicitly registered and tested.
- Migration apply writes a private backup and journal before the first record mutation and restores exact bytes on failure.
- Cleanup is explicit, dry-run first, reference-aware, bounded, resumable, and idempotent; there is no automatic cleanup loop.
- Backup and restore validate destination identity, manifest checksum, record checksums, quota, schema compatibility, references, and conflicts before apply.
- Use strict RED → GREEN → REFACTOR cycles and commit one independently reviewable slice per issue.

---

### Task 1: Issue #72 — typed migration registry and planning

**Files:**
- Create: `src/repoforge/domain/state_lifecycle.py`
- Test: `tests/test_state_lifecycle.py`
- Modify: `src/repoforge/domain/errors.py`

**Interfaces:**
- Produces `StateRecordKey`, `StateMigrationStep`, `StateMigrationRegistry`, `MigrationDirection`, `StateMigrationPlan`, and deterministic plan digests.
- `StateMigrationRegistry.plan(collection, current_version, target_version, direction)` returns contiguous ordered steps or fails closed.
- Migration transforms accept and return `dict[str, object]`; registry validation forbids duplicate edges, gaps, cycles, non-adjacent versions, and missing reverse transforms.

- [ ] Write failing tests for no-op plans, ordered multi-step plans, duplicate/gapped/cyclic registration, future versions, and explicit reverse-only behavior.
- [ ] Run `pytest-target` for the new test file and confirm failure because lifecycle APIs do not exist.
- [ ] Implement minimal typed registry/planning contracts and stable state lifecycle error codes.
- [ ] Re-run focused tests and refactor only after green.

### Task 2: Issue #72 — JSON migration preview/apply, backup, rollback, and restart recovery

**Files:**
- Create: `src/repoforge/adapters/persistence/json_state_lifecycle.py`
- Modify: `src/repoforge/adapters/persistence/__init__.py`
- Test: `tests/test_state_lifecycle.py`

**Interfaces:**
- Produces `JsonStateLifecycleManager.preview_migration(...)`, `apply_migration(...)`, and `recover_incomplete_migrations()`.
- Preview binds collection, target/direction, exact record checksums, migrated deterministic bytes, and a plan digest.
- Apply validates the digest and current checksums, writes mode-0600 backup records plus a journal, atomically replaces records, and marks the journal committed.

- [ ] Write failing tests for mixed versions, deterministic preview, backup-before-write, stale preview, corruption, partial write failure rollback, restart recovery, no-op, and idempotent repeat apply.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement bounded raw envelope decoding, deterministic migration encoding, private backup/journal writing, exact rollback, and recovery.
- [ ] Re-run focused tests, Ruff formatter, lint, and strict Mypy.
- [ ] Review diff and commit `feat(state): add schema registry and safe migrations`.

### Task 3: Issue #73 — typed retention, references, quotas, and cleanup planning

**Files:**
- Modify: `src/repoforge/domain/state_lifecycle.py`
- Modify: `src/repoforge/adapters/persistence/json_state_lifecycle.py`
- Test: `tests/test_state_lifecycle.py`

**Interfaces:**
- Produces `StateRetentionPolicy`, `StateProtection`, `CleanupDisposition`, `StateCleanupPlan`, and `StateCleanupReport`.
- `preview_cleanup(...)` sorts records deterministically, protects caller-supplied active/reference/audit records, detects orphan references, and selects age/count/byte candidates within a cursor-bound batch.
- `apply_cleanup(...)` validates exact checksums, moves candidates to a private plan trash directory, records per-item outcomes, and supports safe repeat/resume.

- [ ] Write failing tests for retention windows, protected active/running/referenced records, count and byte quota pressure, orphan findings, bounded cursors, corrupt records, concurrent mutation, partial failure, restart, and idempotency.
- [ ] Run focused tests and confirm failures.
- [ ] Implement pure cleanup selection and constrained adapter apply/resume behavior.
- [ ] Re-run focused tests, format, lint, and typecheck.
- [ ] Review diff and commit `feat(state): add reference-aware retention and quotas`.

### Task 4: Issue #74 — integrity diagnostics and backup manifests

**Files:**
- Modify: `src/repoforge/domain/state_lifecycle.py`
- Modify: `src/repoforge/adapters/persistence/json_state_lifecycle.py`
- Test: `tests/test_state_lifecycle.py`

**Interfaces:**
- Produces `IntegritySeverity`, `StateIntegrityFinding`, `StateIntegrityReport`, `StateBackupManifest`, and `StateBackupPreview`.
- `inspect_integrity(...)` reports bounded deterministic schema, checksum, reference, quota, corruption, and orphan findings without returning payload bodies.
- `preview_backup(...)` builds a checksum-framed manifest bound to a validated destination identity; `apply_backup(...)` writes only after preview validation.

- [ ] Write failing tests for healthy/corrupt/future-schema records, missing references, duplicate IDs, quota overrun, bounded findings, deterministic manifest checksum, invalid destination identity, and backup conflicts.
- [ ] Run focused tests and confirm failures.
- [ ] Implement integrity scans and private checksum-framed backup export.
- [ ] Re-run focused tests and static checks.

### Task 5: Issue #74 — restore preview/apply and compatibility documentation

**Files:**
- Modify: `src/repoforge/domain/state_lifecycle.py`
- Modify: `src/repoforge/adapters/persistence/json_state_lifecycle.py`
- Create: `docs/development/DURABLE_STATE_LIFECYCLE.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_state_lifecycle.py`

**Interfaces:**
- Produces `StateRestoreConflict`, `StateRestorePreview`, `preview_restore(...)`, and `apply_restore(...)`.
- Restore preview validates manifest/frame checksum, every record checksum, destination identity, schema support, references, quota, and exact existing-file conflicts.
- Restore apply backs up replaced destination bytes, writes atomically, is token-bound and idempotent, and rolls back on interruption/failure.

- [ ] Write failing tests for valid restore, checksum mismatch, missing record, destination mismatch, quota rejection, conflict/no-overwrite, overwrite with destination backup, failure rollback, restart, and idempotent repeat.
- [ ] Run focused tests and confirm failures.
- [ ] Implement restore preview/apply and recovery journal behavior.
- [ ] Document migration authoring, dry-run/apply contracts, retention protections, integrity findings, backup/restore operator flow, rollback guarantees, and compatibility limits.
- [ ] Re-run focused tests, format, lint, and typecheck.
- [ ] Review diff and commit `feat(state): add integrity and recovery workflows`.

### Task 6: Final verification and publication

**Files:**
- Review all changed paths only.

- [ ] Run `workspace_diff`; remove unrelated changes and verify change budgets.
- [ ] Run focused `pytest-target` for `tests/test_state_lifecycle.py`.
- [ ] Run `quick` and then the authoritative `full` profile on the exact final tree.
- [ ] Commit any final documentation-only adjustment only after re-running full verification.
- [ ] Push without force.
- [ ] Create one draft PR with `Closes #72`, `Closes #73`, and `Closes #74`, exact verification evidence, rollback/compatibility notes, and no claim of new public tool surface.
- [ ] Inspect PR checks and report the exact state without merging.
