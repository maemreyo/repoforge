# Test-suite performance epic: breaking the integration-test floor

Status: proposed (design only — no big-bang rewrite)
Date: 2026-07-22

## Why this exists

`make test-fast` (full suite, `pytest -n 3`) runs in ~5:21 and the cost is now
almost entirely intrinsic. This document records what was measured so the epic
starts from evidence, not guesses, and so nobody re-derives it.

### What is already done (shipped, do not redo)

| Change | Effect |
| --- | --- |
| `forge_env` git-repo template (build once, `copytree` per test) | fixture setup 257ms → 48ms/call (5.3×), ~147s → ~27s across the suite |
| pytest `--timeout` 60 → 120 (matches app git budget) | fixed false `Timeout(>60s)` flakiness under xdist |
| Serial lane = genuine shared-state groups only | correctness isolation without over-serializing |
| Coverage-based `test-affected` (`tests/coverage-map.json`) | leaf/domain change → a handful of tests, <1 min inner loop |

### What was measured and rejected (do not retry without new evidence)

- **Higher `-n`**: rejected — the dev machine freezes above `-n 3` (git child
  processes oversubscribe cores).
- **git `core.fsync=none` / `gc.auto=0`**: measured ~2.5% (noise). These tests
  are git-*read*-heavy; fsync only helps writes. Not worth the config.
- **RAM-disk `tmp_path`**: reads/writes already hit the page cache; negligible
  on top of the above.
- **Removing sleeps**: the only sleeps are tight polls (0.05s) and legitimate
  small startup waits; nothing safely removable.
- **Shrinking the serial lane by grep heuristic**: unsafe — `test_phase7_atomic_hot_reload`
  (runtime state) is a false-negative for the shared-state pattern, so a bulk
  move would reintroduce flakiness.
- **Node-level (per-test-function) coverage selection**: no win for core files —
  `commit.py` is executed by 465 distinct test functions; the blast radius is
  genuinely that large, not a granularity artifact.

## Root cause of the remaining cost

Profiling one 12s `execute_plan` test (`cProfile`):

- 11.5s of 11.9s is in `SubprocessCommandExecutor._communicate` — **669 git
  subprocess spawns** in a single test, ~17ms each (pure process-startup; git
  work on the tiny demo repo is instant).
- The spawns are dominated by **~5 read methods**: `fingerprint` (138 binary
  diffs + 69 `head_sha`), `changed_paths` (108), `status_*`. Not the ~60-method
  breadth of the port.
- They are driven by the plan-currency check: `execute_plan._run` calls
  `require_current(plan)` before **every stage**, which runs the **full**
  `WorkspaceAssessment` (status + code-intelligence + PR checks + risk +
  recommendation). `validate_plan_current` legitimately needs
  `risk_assessment_hash` / `recommendation_hash`, and those consume the
  code-intelligence and CI evidence — so the full assessment is not trivially
  removable from the currency path.

**Conclusion:** under `-n 3`, the suite is at a correctness-driven floor. The
test bodies do real, required git work. Speeding them further means changing how
the *core stack* touches git — a production change, not a test tweak.

## Options (ranked by ROI, with honest risk)

### A. Full in-memory `FakeGitRepository` — NOT recommended

Faking all ~60 `GitRepository` methods (worktrees, merges, patches, snapshots,
search) in-memory is more code and bug-surface than the tests it replaces, and a
fake that silently drifts from real git produces green tests against a fiction.
Even guarded by a contract test, the contract suite itself would be enormous.
Rejected.

### B. In-process implementation of the ~5 hot read methods (recommended first increment)

The 669 spawns collapse to a few methods. Replace **only** `fingerprint`,
`head_sha`, `changed_paths`, `status_porcelain*`, `untracked_paths` with an
in-process implementation (via `pygit2`/libgit2, or a batched `git cat-file`/
`git status` reader), keeping every other method on the existing subprocess path.

- Benefits **production too** (fewer spawns on every assessment), so it is a real
  perf fix, not a test hack.
- Guarded by a **contract test**: run identical scenarios against the current
  `GitCliRepository` and the new implementation, assert byte-identical results
  for these methods. If they diverge, the contract test fails — this is the
  safety net that makes the change trustworthy.
- Scope is bounded (5 methods, not 60). New dependency (`pygit2`) is the main
  cost; evaluate against the existing "no heavy deps" stance.

### C. In-operation identity memoization — INVESTIGATED, REJECTED (unsafe)

Spiked 2026-07-22. The premise was that `fingerprint`/`head_sha`, recomputed
4–6× per assessment on the same path, are redundant and cacheable. **They are
not redundant — they are a deliberate drift guard.** `WorkspaceAssessment._assert_current`
(assessment.py:95) re-reads `head_sha` + `fingerprint` and raises
`STALE_ASSESSMENT_SNAPSHOT` if they changed; it is called after **every** evidence
component (`_collect`, plus inline after code-intelligence), specifically to
detect a workspace that mutates mid-assessment while partial evidence is being
collected (code-intelligence `STALE` is even re-mapped to `STALE_ASSESSMENT_SNAPSHOT`
at assessment.py:317). Memoizing the reads makes `_assert_current` compare
cached==cached — a tautology that silently disables concurrent-mutation
detection in a path feeding currency/commit-gate decisions. Reducing the
guard's frequency (once-at-end) is also unsafe: per-component checks validate
each kept component in the partial-result path. **Do not pursue C.**

### D. Layered testing (largest, structural)

Refactor application logic to depend on narrower ports so business logic can be
unit-tested without any workspace/git at all, leaving a thin band of real-git
integration tests. Highest long-term payoff, highest effort, touches
architecture. Only worth it if B/C prove insufficient.

## Recommended path

Revised after the C spike (2026-07-22): **B is safer than C**, reversing the
original ordering. C changes control flow in a safety-critical drift guard; B
leaves all control flow untouched and only swaps the *implementation* of pure
read methods, guaranteed equivalent by a contract test.

1. **B** (in-process hot read methods behind a contract test) is now the only
   recommended technical lever. Reimplement just `fingerprint`, `head_sha`,
   `changed_paths`, `status_porcelain*`, `untracked_paths` in-process (pygit2 or
   batched git), gated by a contract test asserting byte-equivalence to
   `GitCliRepository`. Decide the `pygit2` dependency question first.
2. C is rejected (see above). A and D remain out of scope.
3. If B's cost/dependency is unacceptable, the practical stance is to **stop**:
   the inner loop (`test-affected`) is already <1 min for the common case and
   the full suite (~5:21) is an acceptable pre-push gate.

Each increment must: keep the full suite green, ship its contract/regression
test, and be independently revertable. No half-built fake left in the tree.

## Guardrails that make any of this safe

- The authoritative gate (`production-gate.yml`) always runs the **full** suite,
  so a selection/optimization regression can never merge silently.
- Any git reimplementation ships with a contract test asserting equivalence to
  the real `GitCliRepository` before a single test migrates onto it.
