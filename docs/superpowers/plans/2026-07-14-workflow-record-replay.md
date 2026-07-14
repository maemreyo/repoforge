# Sanitized Workflow Record and Replay Implementation Plan

> **For agentic workers:** use superpowers:executing-plans or subagent-driven-development. Follow test-first development and exact-tree verification.

**Goal:** Add a deterministic, private, sanitized workflow record and isolated replay format for behavioral evaluation.

**Architecture:** Domain models define the schema and safety invariants. A private checksum-framed JSON adapter owns persistence, retention, and fixture export. An application recorder creates bounded records. Replay runs only through explicitly isolated, no-real-write adapters.

### Task 1: Domain schema and normalization

- Create `src/repoforge/domain/workflow_recording.py`.
- Add stable recording/replay errors.
- Define typed enums, argument summaries, events, metrics, recordings, validation, and safe argument categorization.
- Write failing tests for deterministic ordering, direct/failure workflows, forbidden payloads, event bounds, future schemas, and explicit truncation.

### Task 2: Persistence port and adapter

- Create `src/repoforge/ports/workflow_recording_store.py`.
- Create `src/repoforge/adapters/persistence/json_workflow_recording_store.py`.
- Add exports.
- Test deterministic framed bytes, checksum validation, strict fields, permissions, atomic writes, bounded listing, fixture export, and retention by age/count/bytes.

### Task 3: Recorder service

- Create `src/repoforge/application/workflow/recorder.py` and package exports.
- Build recordings from typed event inputs using existing clock/ID/audit boundaries.
- Enforce event and encoded-size bounds; mark truncation rather than silently dropping evidence.
- Test direct and failure recording flows and audit-safe metadata.

### Task 4: Isolated replay

- Create `src/repoforge/ports/workflow_replay.py`.
- Create `src/repoforge/application/workflow/replay.py`.
- Add a deterministic recorded-category adapter and test fake adapters.
- Reject non-isolated or real-write adapters before invocation.
- Reject truncated recordings as complete eval evidence by default.
- Test deterministic replay and isolation.

### Task 5: Internal composition and documentation

- Wire the store, recorder, and default replay engine into application composition without adding MCP or CLI tools.
- Add internal service methods only if required by tests and future eval harness consumers.
- Update roadmap/testing documentation; release contract must remain unchanged.

### Task 6: Verification and publication

- Run focused workflow-recording and replay tests.
- Run static formatting, Ruff, and strict Mypy.
- Run the untouched production verification profile and RepoForge full exact-tree verification.
- Review the exact diff and safety boundaries.
- Commit, push without force, and open one draft PR with `Closes #25`.
