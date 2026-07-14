# Snapshot-Consistent Workspace Assessment Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans or subagent-driven-development. Follow test-first development and exact-tree verification.

**Goal:** Add a read-only internal assessment transaction whose evidence is bound to one exact workspace/config/policy snapshot.

**Architecture:** Domain models own identity and component consistency. An application reader composes existing workspace readers and checks identity after every provider boundary. Provider failures remain typed component evidence; identity drift fails the whole assessment.

### Task 1: Domain contract

- Create `src/repoforge/domain/assessment.py`.
- Add stable assessment error codes.
- Test deterministic snapshot IDs, component bounds, ordering, and identity consistency.

### Task 2: Application orchestration

- Create `src/repoforge/application/workspace/assessment.py`.
- Capture config generation and policy hash.
- Compose status, diff, base, PR, and checks readers.
- Revalidate after every boundary and before return.
- Convert provider failures to bounded typed evidence.

### Task 3: Internal service integration

- Wire `WorkspaceAssessmentReader` into `CodingService` without adding MCP or release-contract changes.
- Document the assessment test layer and roadmap capability.

### Task 4: Tests

- Add domain consistency tests.
- Add real temporary workspace integration.
- Add mutation injection at multiple boundaries.
- Add no-PR, missing-CI, unavailable-remote, and partial-provider matrices.
- Assert deterministic ordering, policy-safe paths, bounded references, no verification receipt creation, and audit safety.

### Task 5: Verification and publication

- Run focused assessment/workspace/GitHub tests.
- Run the untouched production verification profile and RepoForge full exact-tree gate.
- Review diff and change budget.
- Commit, push without force, and open one draft PR with `Closes #14`.