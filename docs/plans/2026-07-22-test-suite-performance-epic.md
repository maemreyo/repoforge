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

### B. In-process implementation of the hot read methods — INVESTIGATED, NOT WORTH IT

Spiked 2026-07-22 with `pygit2` 1.19.3 / libgit2 1.9.4 (installs cleanly). On
inspecting the method contracts, the plan collapses:

- **`fingerprint` (the #1 cost, 138 of the binary git calls) is immovable.** Its
  value is `sha256(head_sha + raw bytes of "git diff --binary HEAD" + untracked
  contents)` — a hash of git CLI's **exact diff output**. libgit2's diff bytes
  are not identical to git CLI's, so a pygit2 fingerprint yields a *different
  hash*. And `workspace_fingerprint` is a **persisted, cross-cutting identity**:
  serialized in plan bindings (execution_plan.py:431), compared for currency
  (:379), and stamped into receipts/task-capsules/operation-tasks. Changing its
  algorithm invalidates every stored plan and iteration-cache key — a breaking
  migration, not an optimization. The contract test would (correctly) reject it.
- **`status_porcelain_v2`** returns git's exact porcelain-v2 `-z` text format;
  reconstructing it byte-for-byte from pygit2 status flags is error-prone.
- That leaves `head_sha` (trivially byte-exact via pygit2, but small — many of
  its calls are *inside* fingerprint, which stays on CLI) and
  `changed_paths`/`untracked_paths` (output is a path list, so byte-equivalence
  is achievable, but replicating git's diff + `--exclude-standard` gitignore +
  the symlink/submodule mode security checks is high-effort and high-risk).

With the dominant method off the table, the residual win (head_sha +
changed_paths) does not justify adding a C-extension dependency and reimplementing
git semantics. **B is not worth pursuing.**

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

Both technical levers were spiked on 2026-07-22 and **both are blocked**:

- **C rejected** — memoizing identity disables the `_assert_current` drift guard.
- **B not worth it** — the dominant method (`fingerprint`) is an immovable
  persisted identity that hashes git CLI's exact diff bytes; the residual win
  doesn't justify a C-extension dependency and reimplementing git semantics.
- **A** (full ~60-method fake) and **D** (layered re-architecture) remain large,
  high-risk, and out of scope.

**Conclusion: stop.** The shipped optimizations (forge_env template, timeout-layer
fix, coverage-based `test-affected`) are the practical optimum under the `-n 3`
machine cap. The inner loop is already <1 min for the common (domain/leaf) case;
the full suite (~5:21) is an acceptable pre-push gate. Further speedup would
require changing persisted-identity semantics or the test architecture itself —
neither justified by the marginal wall-clock saved.

Reopen only if a constraint changes: the machine tolerates more parallelism, the
fingerprint identity is redesigned for other reasons, or CI wall-clock becomes a
real bottleneck.

## Guardrails that make any of this safe

- The authoritative gate (`production-gate.yml`) always runs the **full** suite,
  so a selection/optimization regression can never merge silently.
- Any git reimplementation ships with a contract test asserting equivalence to
  the real `GitCliRepository` before a single test migrates onto it.
