# Runtime health/observability incident remediation (Signatures A, B, C)

Status: Implementation tracked by [issue #448](https://github.com/maemreyo/repoforge/issues/448); this file is its design source of truth.

## Objective

Make RepoForge's runtime supervision truthful and non-self-destabilizing under three related failure classes discovered live on 2026-08-04 while activating PR #446's candidate (`3e1cd5a`):

- **Signature A** — `runtime-single-instance` lock handoff contention causing an unbounded launchd respawn loop.
- **Signature B** — remote Secure MCP Tunnel control-plane 5xx responses causing the local supervisor to kill and restart the tunnel-client child.
- **Signature C** — the control socket's `HEALTH` path running a fresh, uncached, blocking nested probe on a single-threaded accept loop, which can queue callers behind each other and produce timeouts and `degraded` phase flips that are artifacts of the observability mechanism itself, not of the service being observed.

The end state: an operator or agent reading runtime status can trust what it says, restarts are bounded and never unbounded-cascade, and a candidate that ends up serving successfully is never left with a permanently `failed` activation receipt with no path to a truthful terminal state.

## Non-goals

- Not fixing the external `tunnel-client` binary (`/usr/local/bin/tunnel-client`, independently versioned, source not in this repo) — Signature B's remote-5xx *behavior* (shutdown instead of reconnect) is upstream-owned. This issue's job is to stop RepoForge's *own* policy from amplifying it into cascading local restarts, not to fix the binary.
- Not addressing the 39 orphaned `execution_worker` processes from the old `b68aa18d` release — tracked separately as a follow-up on #424, confirmed unrelated (they hold no relevant locks).
- Not manually editing the terminal-`failed` activation receipt (`act-20260804-001`) to force it to `success` — any fix must reach a truthful state via a designed recovery/re-activation path, never a hand edit.
- Not increasing the `2.0s` HEALTH timeout to 5s/10s as a fix — that hides the symptom; the architectural fix is to stop running an expensive nested probe on the request path at all (see Architecture Decision).

## Proven vs. unproven — do not conflate

**Proven (code + direct log/process/unified-log evidence, not inference):**
- Signature A: `single_instance_wait_seconds` (45s) is a real `flock(LOCK_EX|LOCK_NB)` poll loop (`adapters/locking/fcntl.py:37-58`); `~/Library/LaunchAgents/dev.repoforge.supervisor.plist`'s `KeepAlive: {SuccessfulExit: False, Crashed: True}` (`adapters/activation/launchd.py:55`) unconditionally respawns the job on any non-zero exit.
- Signature B: `tunnel_cli.py:_observe_log_line` (~line 126) marks `control_plane_response` unhealthy on ≥2 consecutive 502/503/504/"failed to post"/"context canceled" lines from the external binary's own log. `supervisor.py`'s watchdog (2s cadence, `health_failure_threshold=3` default) calls `tunnel.terminate()` (SIGTERM→SIGKILL) on the child once threshold is hit (`supervisor.py:875-922`).
- **The A↔B connecting link is now code-proven** (not just hypothesized): if the in-process tunnel-child respawn also fails and `restart_count > max_restarts` (default 3), the supervisor writes `RuntimePhase.FAILED` and `return 2` (`supervisor.py:932-950`) — the entire worker process deliberately exits, which is exactly what launchd's `KeepAlive` then relaunches. Reconstructed one full instance of this via macOS unified log: pid `79232` started `15:37:46.115Z`, confirmed exited `15:40:54.671Z` (independently corroborated by `cfprefsd`'s XPC-invalidation log), replaced by pid `81580` at `15:40:54.745Z`.
- Signature C: `adapters/runtime/unix_control.py:UnixRuntimeControlServer.start()` (line 171-252) runs a single dedicated thread with a synchronous `accept()`→`exchange()`→`accept()` loop, no thread pool. Every external `HEALTH` request triggers a fresh, uncached call to `_observe_health()` (`supervisor.py:426`), which does its own nested, blocking MCP round-trip (`timeout_seconds=2.0`, `supervisor.py:379` — a bare literal duplicated at `main.py:1438`, no shared constant, no comment, no historical latency data anywhere to justify the value). A second caller queues in the kernel backlog with no bound on wait time before its own client-side timeout fires. Directly observed: `repository_self_check` timeout at `16:04:49Z` (self-cleared by `16:05:27Z`, zero restarts — never reached the 3-consecutive threshold since only the *reported phase* is single-failure-triggered, not the restart).
- **New observability defects, proven via a live `config_inspect` call during this investigation**: a health snapshot **1144.6 seconds (~19 minutes) old** was still reported as `phase: healthy` with no staleness indication — `runtime_health.observed_age_seconds` exists as a field but nothing downstream refuses to call an old snapshot "healthy". Separately, that same call reported `restarts_total: 0`/`last_restart_at: null` — i.e. **restart counters reset to zero on every new process incarnation** (matches: `restarts_total`/`last_restart_at` live only in the in-memory-backed durable record tied to the current process's own lifetime, not a cross-incarnation-durable store) — meaning the exact gate technique used during this investigation (comparing `restarts_total` deltas) is **only valid when combined with process identity and launchd `runs`**, and is not by itself a safe signal across a supervisor replacement.

**Unproven / correlated only — explicitly not to be read as causal:**
- Whether the `16:04:49Z` `repository_self_check` timeout (Signature C) contributed to the three-restart cluster observed ~112–172s later at `16:06:41Z`–`16:07:48Z` (Signature B-shaped: each new tunnel-client died within tens of milliseconds of starting). Timing is suggestive, not proven; the gate script that plausibly contributed load to the *first* timeout had already exited before the later cluster and cannot explain it.
- Whether any *specific* historical `RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT` occurrence was actually produced by the A↔B chain above (vs. some other cause) — the chain itself is code-proven as *possible*, not proven for any one past occurrence.
- Whether timed-out probes leave orphaned server-side work running (plausible from the code shape — no visible cancellation of the nested `mcp_control.request()` once the outer caller's client-side timeout fires — but not directly observed/measured).
- Historical frequency of Signature C before tonight — not instrumented; no discrete event log for `repository_self_check` timeouts exists, only the current overwritten boolean.

## Architecture decision

Do not treat these as three independent point-fixes. The unifying decision:

1. **`HEALTH` becomes a snapshot read, never a live probe trigger.** The watchdog is the sole producer of health observations (already true for its own loop, `supervisor.py:875`); `_control_handler`'s `HEALTH` branch (`supervisor.py:423-438`) must stop calling `_observe_health()` itself and instead return the most recent snapshot the watchdog already computed, annotated with `observed_at`/`age_seconds`/a freshness state. This removes Signature C's core mechanism (a request-triggered nested probe) entirely, rather than tuning its timeout.
2. **The control socket gets bounded concurrency**, independent of (1) — a slow *anything* (not just HEALTH) must not block other callers indefinitely. Bounded worker pool or per-connection task with a cap, explicit overload response when saturated, deadline+cancellation per request.
3. **Active probing (if ever needed on-demand) is single-flight** — at most one real probe in flight; concurrent callers await the same result or get the current snapshot, never each triggering their own.
4. **Health becomes a state machine with freshness and hysteresis**, not one boolean: `healthy | transient_failure | degraded | unavailable | stale | failed`, driven by failure *class* and *sustained* evidence, not a single failed observation.
5. **Restart/incarnation observability becomes durable and epoch-aware** — a supervisor incarnation ID and tunnel-child incarnation ID, a monotonic restart sequence that survives process replacement, last-restart-reason, last-successful-probe-at, snapshot age, and a unique per-cycle identifier (the tunnel-client's correlation ID is confirmed reused across restarts and must not be relied on as a lifecycle identifier).
6. **Signature A's handoff gets explicit serialization** against launchd-style relaunch, so a fresh instance can't race a dying one for the lock in a way that produces a 45s stall.
7. **Activation reaches a truthful terminal state** when a previously-failed candidate later serves successfully — a designed recovery/re-activation path, not a reconciliation shortcut and never a manual receipt edit.

## Acceptance criteria

- [ ] External `HEALTH` requests return an immutable, timestamped snapshot; they never trigger a fresh nested MCP round-trip.
- [ ] The watchdog is the only producer of active health probes; on-demand refresh (if it exists at all) is single-flight.
- [ ] The Unix control server has bounded concurrency, an explicit overload response, and per-request deadlines with real cancellation — no unbounded thread/task creation, and no single slow request can block all other callers.
- [ ] A health snapshot older than a defined threshold reports `stale`/`unknown`, never `healthy`.
- [ ] A single transient probe failure does not, by itself, flip the externally reported phase to `degraded`; sustained/classified failure evidence is required.
- [ ] Restart/incarnation history (`restarts_total`-equivalent, `last_restart_at`-equivalent, restart reason) is durable across process replacement, keyed to a monotonic incarnation identifier, not reset to zero on every new process.
- [ ] Every supervisor and tunnel-child lifecycle instance has a unique cycle identifier that is never reused across restarts (the tunnel-client's own correlation ID is confirmed reused and unsuitable for this).
- [ ] Repeated Signature-B-style child failures are bounded with backoff **and jitter**, and cannot, by themselves, exhaust `max_restarts` and force a full supervisor exit without that exit itself being a clearly diagnosable, intentional terminal state (not indistinguishable from a crash).
- [ ] A candidate that ends up serving successfully after a `failed` activation receipt has a designed path to a truthful terminal state — never a permanently stuck `failed` receipt with a healthy candidate, and never a manually edited receipt.
- [ ] `RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT`/lock-handoff evidence surfaces in the structured log an operator actually checks first, not only in raw stderr.

## Deterministic test matrix

Each row must be a real, automated, deterministic test (not a manual runbook step) added under the relevant slice below:

1. Two concurrent external `HEALTH` callers — assert both get bounded, fast responses; assert only one (or zero, if pure snapshot) underlying probe actually ran.
2. A slow/blocked nested MCP request — assert it cannot block unrelated control-socket callers (PING/STATUS) from responding promptly.
3. A probe timeout occurring while real service traffic continues succeeding — assert phase does not flip to `degraded` from that alone.
4. Prolonged remote relay 502/503/504 (simulated) — assert bounded restart attempts with backoff+jitter, not an unbounded loop.
5. Restart budget exhaustion (`max_restarts` exceeded) — assert a clearly diagnosable terminal state is reached and reported, and that this triggers the *intended* recovery path rather than an opaque crash-looking exit.
6. A launchd-style parent relaunch racing an in-progress handoff — assert no `RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT`-class stall, or if lock contention is unavoidable, assert a bounded, clearly diagnosed wait.
7. An activation running concurrently with a simulated remote outage — assert the activation's own success/failure determination is not corrupted by transient remote flakiness.
8. A stale health snapshot after process replacement — assert it is reported `stale`/`unknown`, not `healthy`, and that restart counters/incarnation identifiers reflect the replacement rather than resetting silently.
9. A terminal `failed` activation receipt while the candidate is actually serving — assert a designed path reaches a truthful terminal state (never a manual receipt edit, never silently ignored).

## Live verification procedure (post-fix, before closing this issue)

1. Deploy the fix to a real runtime (not just unit/integration tests) and run a read-only, two-layer stability gate similar to the one used during this investigation, but now validated against the corrected semantics (process/handoff layer unchanged in spirit; service layer must read the new snapshot+freshness fields rather than triggering probes).
2. Confirm, live: two rapid `HEALTH`-equivalent calls in quick succession do not produce a queuing-induced timeout.
3. Confirm, live: after a deliberate tunnel-child kill (test-only, explicitly authorized), the health snapshot correctly ages, transitions to `stale`, and restart/incarnation counters increment durably and are visible cross-process.
4. Only after this passes: resume #404's functional compatibility testing (currently BLOCKED on this issue), per the existing runbook.

## Rollback and release-safety requirements

- Ship behind the existing config-generation/release-activation mechanism (`rf upgrade`), never as a hot patch to a live process.
- Any change to the control-socket protocol (new snapshot fields, freshness states) must be additive/backward-compatible for in-flight callers reading the old shape, consistent with this repo's existing durable-schema-evolution pattern (optional fields, defaulted on read).
- A rollback to the previous release must not itself require a manual receipt edit or produce a new terminal-`failed`-but-serving inconsistency — the fix for that specific inconsistency (item 6 in Architecture Decision) must itself be rollback-safe.
- No change in this issue's scope should be mixed into PR #446 — #446 stays frozen as the #404 compatibility-evidence candidate; this remediation lands as its own change, on its own branch/PR.

## Explicitly out of scope (unchanged from original)

39 unsupervised `execution_worker` processes (generation 17, old `b68aa18d` release) — tracked separately as a follow-up on #424, confirmed via `lsof` to hold none of the locks relevant here.

## Related

- Discovered while working EPIC #369 → #404 (ChatGPT Web / Secure MCP Tunnel compatibility), PR #446 — kept frozen as evidence, not touched by this remediation.
- #404's functional testing remains **BLOCKED** on this issue, not merely paused pending a timed gate.
- Full evidence trail, exact timelines, and file:line citations for Signatures A, B, and C are preserved in this issue's comment history — this body is the living spec; the comments are the investigation record.

## Implementation plan for #448 (main-agent session — planning only, not executed here)

This is a plan for a **dedicated implementation session on its own workspace/branch**, separate from PR #446 (which stays frozen as #404 evidence). No production code has been touched to produce this plan. Each slice below names exact files, the invariant it must establish, the tests to add, and how to verify it live.

---

### Slice 1 — Bounded concurrency in the control server

**Files:** `src/repoforge/adapters/runtime/unix_control.py` (`UnixRuntimeControlServer.start()`, lines 171–252).

**Invariant:** no single in-flight request handler can block the accept loop from servicing other callers; total concurrent handlers is capped; a caller arriving at capacity gets an explicit, typed overload response (not a silent queue-and-timeout).

**Approach:** replace the single dedicated `serve()` thread's inline `exchange(connection)` call with a bounded worker pool (e.g. `concurrent.futures.ThreadPoolExecutor` sized to a small constant, or a semaphore-gated thread-per-connection model) so `accept()` keeps cycling while handlers run. Each handler execution gets a deadline; on cap exhaustion, return `ControlResponse(ok=False, error_code="CONTROL_SOCKET_SATURATED", ...)` immediately rather than accepting and holding the connection.

**Tests:** extend `tests/test_phase4_runtime_control.py` with: two concurrent `HEALTH`-class requests both returning promptly (bounded, not serialized behind each other); a deliberately slow handler not blocking a concurrent `PING`; capacity exhaustion producing the typed overload response, not a hang.

**Migration/compat:** `ControlResponse`/`ControlRequest` shapes are additive-only (new `error_code` value is a new enum-ish string, not a breaking change to existing readers).

---

### Slice 2 — Snapshot-only `HEALTH`, single-flight active probing

**Files:** `src/repoforge/application/runtime/supervisor.py` (`_control_handler`, lines 393–449; `_observe_health`, lines 333–391; the watchdog loop, ~lines 840–930).

**Invariant:** external `HEALTH` requests never trigger a fresh nested MCP round-trip; the watchdog is the sole producer of health observations; if on-demand refresh is retained at all, concurrent demand collapses to one real probe (single-flight), and every caller either reads the latest completed snapshot or awaits that one shared result — never each independently launching a probe.

**Approach:** `_control_handler`'s `HEALTH` branch stops calling `self._observe_health(...)` (line 426) and instead reads the most recent snapshot the watchdog already wrote into `self._store`/an in-memory cache, annotated with `observed_at`, `age_seconds`, and a freshness classification (see Slice 3). If on-demand refresh is wanted, gate it behind a single-flight primitive (e.g. one in-flight `Future` shared across concurrent callers, guarded by a lock) rather than each caller independently invoking `_observe_health()`.

**Tests:** new test asserting a `HEALTH` request under this change does **not** cause `_mcp_control.request()` to be invoked (mock/count calls); a concurrency test with N simultaneous `HEALTH` callers asserting at most one underlying probe executes if refresh-on-demand is kept.

**Migration/compat:** response payload gains `observed_at`/`age_seconds`/freshness fields — additive; existing consumers reading only `status`/`ok` are unaffected.

---

### Slice 3 — Health state machine: freshness + hysteresis

**Files:** `src/repoforge/domain/runtime.py` (`RuntimePhase`, `HealthCheck` — locate/extend near existing enum); `src/repoforge/application/runtime/supervisor.py` (the `RuntimePhase.DEGRADED` write path, lines 904–918).

**Invariant:** a snapshot older than a defined age threshold is reported `stale`/`unknown`, never `healthy`. A single transient probe failure produces `transient_failure`, not `degraded`; only sustained/classified failure evidence (the existing `consecutive_health_failures` concept, or a refined successor) escalates to `degraded`/`unavailable`. States: `healthy | transient_failure | degraded | unavailable | stale | failed`.

**Approach:** extend `RuntimePhase` (or introduce a parallel freshness/severity axis alongside it, since `RuntimePhase` may be relied on elsewhere for restart-decision logic — check all call sites of `RuntimePhase` before deciding enum-extend vs. new orthogonal field) with the new states; add an `age_seconds`-driven staleness check wherever a snapshot is read externally; change the single-failure `DEGRADED` write (line 910) to require the same sustained-evidence logic the restart trigger already uses (`consecutive_health_failures`), or an explicit `transient_failure` intermediate state for the first failure.

**Tests:** unit tests for each state transition (fresh healthy → stale after threshold; single failure → transient_failure not degraded; N consecutive failures → degraded; recovery path back to healthy). Golden/contract test update if `RuntimePhase` is part of any public contract surface (check `contracts/v2.py`/schema goldens for `phase`-shaped fields before assuming this is internal-only).

**Migration/compat:** if `RuntimePhase` appears in any MCP-facing contract, this is a **contract change** — regenerate `docs/contracts/tool-schemas-v2.json` and bump the tool-surface hash deliberately, following this repo's existing pattern for contract evolution (do not let it land as an accidental silent hash change).

---

### Slice 4 — Durable, incarnation-aware restart observability

**Files:** `src/repoforge/domain/runtime.py` (`RuntimeRecord` or equivalent durable dataclass); the durable-record store adapter (`src/repoforge/adapters/persistence/json_runtime_activation_store.py` or the specific store backing `managed-runtime-v3.json` — confirm exact class before implementing); `src/repoforge/interfaces/cli/main.py` (`_runtime_status`, line 1417+, and `_activation_result`).

**Invariant:** `restarts_total`/`last_restart_at`-equivalent history is durable across process replacement, keyed to a monotonic supervisor-incarnation identifier — not reset to zero on every new process (confirmed live tonight: a fresh incarnation reported `restarts_total: 0`/`last_restart_at: null` despite a real prior restart history). Every supervisor and tunnel-child lifecycle instance gets a unique cycle identifier that is never reused (the tunnel-client's own correlation ID is confirmed reused across restarts and is unsuitable).

**Approach:** introduce a durable, file-backed (or otherwise cross-process-durable) incarnation counter/log separate from the in-memory-scoped current record, following this repo's existing backward-compatible schema-evolution pattern (new fields at the end with defaults, `_OPTIONAL_*_FIELDS`-style decoding) so in-flight records from before this change still load. Generate a fresh, non-reused UUID/ULID per supervisor incarnation and per tunnel-child spawn, and thread it through the same structured-event logging path tunnel_cli.py already uses, replacing reliance on the external binary's own (reused) correlation ID for lifecycle attribution.

**Tests:** a durable-record round-trip test (this repo already has `tests/test_durable_record_round_trip.py`-style coverage for other records — add the new incarnation/restart-history fields there, marking any legitimately-inapplicable combinations the same way the existing `_ADHOC_MUTUALLY_EXCLUSIVE_FORMS` exemption pattern does); a test that a fresh process incarnation still reports historical restart counts rather than resetting to zero.

**Migration/compat:** must not require a manual migration step for an already-running installation — first read after upgrade should self-heal to "0 known prior restarts" rather than erroring, but must not report that as if it were verified zero history (surface an explicit "history predates this build" marker rather than a bare `0`).

---

### Slice 5 — Supervisor restart/handoff and launchd amplification protections

**Files:** `src/repoforge/application/runtime/supervisor.py` (`_single_instance_lock`, lines 468–493; the `restart_count > max_restarts` → `return 2` path, lines 932–950); `src/repoforge/adapters/activation/launchd.py` (line 55, `KeepAlive`); `src/repoforge/adapters/locking/fcntl.py`.

**Invariant:** a fresh instance launched by launchd cannot race a still-terminating prior instance into a 45-second stall; if lock contention is genuinely unavoidable during a fast respawn, the wait is bounded and clearly diagnosed as "prior incarnation still shutting down," not surfaced as an opaque `RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT`. The `return 2`-on-`max_restarts`-exceeded path (which is what launchd's `KeepAlive` amplifies into a full relaunch) must itself be a clearly diagnosable, intentional terminal state, not indistinguishable from an ordinary crash.

**Approach:** investigate whether the prior incarnation's shutdown sequence (`TunnelCliClient._finalize_child`'s 30s log-pump join, plus the 15s termination graces at `supervisor.py:990,997,1017,1024`) can be bounded tighter or made to release the single-instance lock earlier (e.g., release the lock as soon as the decision to exit is made, before the slower child-teardown sequence, if correctness allows); ensure the `max_restarts`-exceeded exit path writes an unambiguous, distinctly-coded terminal record (already partially done via `error_code="RESTART_LIMIT"`, `supervisor.py:945` — verify this is actually surfaced clearly enough for an operator/agent to distinguish from a genuine crash without reading source).

**Tests:** extend `tests/test_supervisor_handoff_lock.py` with a test modeling a fast respawn racing a still-shutting-down prior instance; assert bounded, diagnosed wait rather than the raw 45s timeout with no context.

**Migration/compat:** no plist/launchd-level behavior change without re-verifying `live-activation` CI, since this touches the actual OS-level service definition.

---

### Slice 6 — Truthful activation recovery for terminal-failed-but-serving state

**Files:** `src/repoforge/application/activation/upgrade.py` (`UpgradeService.reconcile()`, and the `_activate`/`_upgrade_locked` flow, ~lines 173–302, 645–799).

**Invariant:** when a candidate that received a terminal `failed` activation receipt is later found to be genuinely, durably serving (per Slice 3's freshness-aware health, not a stale snapshot), there is a designed path to a truthful terminal state — never a manual receipt edit, never a silent `reconcile()` no-op left standing indefinitely.

**Approach:** design (in this planning pass, not implemented yet) a new, explicit reconciliation branch or a distinct "promote a serving candidate" operation that: (a) independently re-verifies current health via the Slice-3 state machine (not a stale/cached read), (b) if genuinely healthy and stable for a defined minimum duration, writes a new, honestly-labeled receipt (e.g. `outcome: "recovered"` or similar — never silently rewriting the original `failed` receipt itself, preserving history), (c) makes this explicit and operator/agent-visible rather than automatic-and-silent.

**Tests:** a test simulating exactly tonight's shape (terminal `failed` receipt, candidate later serving durably) and asserting the new path reaches a truthful, distinctly-labeled terminal state; a test asserting a *stale* health read does **not** trigger this promotion (guards against Slice 3/4 gaps re-introducing a false-positive here).

---

### Slice 7 — Deterministic A/B/C regression tests and post-fix live gate

**Files:** new test module(s) alongside the existing `tests/test_supervisor_handoff_lock.py`, `tests/test_phase4_runtime_control.py`, `tests/test_supervisor_preflight_fail_closed.py`; a new or extended live-verification script alongside `scripts/live-activation-sandbox.sh` (confirm exact script name before implementing).

**Content:** implement the full deterministic test matrix from the issue body (concurrent `HEALTH` callers, slow nested probe, probe-timeout-without-service-impact, prolonged simulated remote 5xx, restart-budget exhaustion, launchd-style relaunch race, activation-during-remote-outage, stale-snapshot-after-replacement, terminal-failed-but-serving). Add a post-fix live gate to CI or as a documented manual step (decide during implementation) mirroring the two-layer read-only gate used during this investigation, but reading the corrected snapshot/freshness fields.

**Verification commands** (to be exact once implemented — placeholders now): `scripts/select_affected_tests.py --run` scoped to the changed modules; a live `rf upgrade --activate --watch` rehearsal against a disposable profile before touching the real one; `gh pr checks` green on the dedicated remediation branch before merging.

---

### Sequencing recommendation

Slices 2 and 3 are the core fix for Signature C and should land together (snapshot-only HEALTH is meaningless without freshness/hysteresis to interpret it). Slice 1 (bounded concurrency) can land independently and first, as a safety net regardless of Slice 2/3 timing. Slice 4 (durable incarnation observability) should land before or with Slice 6, since Slice 6's "genuinely healthy and stable" check depends on trustworthy restart history. Slice 5 (handoff/launchd) and Slice 7 (tests + live gate) close out the issue. Do not attempt all seven in one PR — this matches the pattern already used across #370–#443 in this branch's own history (staged, reviewed, sequential commits).
