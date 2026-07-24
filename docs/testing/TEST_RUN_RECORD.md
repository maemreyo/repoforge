# RepoForge full-flow test record

Copy this file for each release or material tool-metadata change.

Suggested filename:

```text
repoforge-test-record-YYYYMMDD-HHMM.md
```

Do not record API keys, GitHub tokens, tunnel credentials, private-key paths, complete environment
dumps, file bodies from sensitive repositories, or unredacted audit logs.

## Run identity

| Field | Value |
|---|---|
| Date/time and timezone | |
| Tester | |
| RepoForge version | |
| RepoForge source commit | |
| Running package version | |
| Executable path/origin | |
| Accepted / active generation | |
| Server / current tool-surface hash | |
| Negotiated client capability summary | |
| Client rediscovery recommended | Yes / No — reason codes |
| Python version | |
| MCP SDK version | |
| Git version | |
| GitHub CLI version | |
| Node/npx version | |
| tunnel-client version | |
| ChatGPT plan/workspace | |
| ChatGPT model | |
| Plugin name | RepoForge |
| Plugin refreshed after tool changes | Yes / No |
| Target repo ID | |
| Target source path | |
| Target base SHA | |
| Config SHA-256 | |
| Tunnel identifier | Redacted label only |
| Test PR URL | |
| Test PR final state | Closed / Other |

## L0 — source gate

| Check | Command | Result | Notes |
|---|---|---|---|
| Dependency sync | `uv sync --extra dev` | Pass / Fail | |
| Ruff | `uv run ruff check .` | Pass / Fail | |
| Mypy | `uv run mypy src/repoforge` | Pass / Fail | |
| Pytest/coverage | `uv run pytest --cov=repoforge --cov-report=term-missing` | Pass / Fail | |
| Release contract | `uv run --extra dev python scripts/check_release_contracts.py` | Pass / Fail | Tool count and surface hash |
| Build | `uv build` | Pass / Fail | |
| Production gate | `scripts/verify-production.sh` | Pass / Fail / Not run | Exact clean tree only |
| Clean source diff | `git status --short` | Pass / Fail | |

## L1 — high-risk suites

| Suite | Result | Notes |
|---|---|---|
| Security | Pass / Fail | |
| Local Git integration | Pass / Fail | |
| Service/write tools | Pass / Fail | |
| GitHub-native graph/readiness | Pass / Fail | Root/relationship/truncation coverage |
| Runtime/client health | Pass / Fail | Package, generation, surface, rediscovery |
| TaskCapsule and approval stores | Pass / Fail | Restart, stale revision, migration, permissions |
| Structured verification and hygiene | Pass / Fail | Failure domains and no-regression receipt |
| Mutation idempotency and failure reuse | Pass / Fail | Replay/conflict/lost-response/force-rerun |
| Code intelligence | Pass / Fail | Bounds, stale snapshot, fallback, affected tests |
| MCP contract | Pass / Fail | |

## L2 — operator machine

| Check | Result | Notes |
|---|---|---|
| `rf config path` | Pass / Fail | |
| `rf doctor` | Pass / Fail | |
| `rf repo list` contains target repo ID | Pass / Fail | |
| Correct source path | Pass / Fail | |
| Correct `origin/main` | Pass / Fail | |
| `gh auth status` | Pass / Fail | |
| Required tool versions | Pass / Fail | |
| Source clone clean afterward | Pass / Fail | |
| No remote branch or PR created | Pass / Fail | |

## L3 — MCP Inspector

| Check | Expected | Actual | Result |
|---|---|---|---|
| Connection | stdio succeeds | | Pass / Fail |
| Tool count | matches docs/tests | | Pass / Fail |
| Tool-surface hash | matches release contract/current server | | Pass / Fail |
| Tool schemas | valid and bounded | | Pass / Fail |
| Runtime/client health | current surface or actionable rediscovery | | Pass / Fail |
| Read annotations | correct | | Pass / Fail |
| Write annotations | correct | | Pass / Fail |
| Structured output | matches contract | | Pass / Fail |
| Invalid ID | actionable error | | Pass / Fail |
| Over-limit input | rejected | | Pass / Fail |
| stdout integrity | JSON-RPC only | | Pass / Fail |

## L4 — discovery prompts

For each prompt, record the exact prompt in the Notes field or attach a redacted transcript.

| Case | Expected selection | Actual tools | Arguments appropriate | Confirmation correct | Result | Notes |
|---|---|---|---|---|---|---|
| Direct read-only | RepoForge read tools | | Yes / No | Yes / No / N/A | Pass / Fail | |
| Indirect readiness | RepoForge read tools | | Yes / No | Yes / No / N/A | Pass / Fail | |
| Incomplete graph | fail closed; no guessed Ready issue | | Yes / No | N/A | Pass / Fail | |
| Retired Project apply | no external write | | Yes / No | N/A | Pass / Fail | |
| Runtime rediscovery | health read and exact remediation | | Yes / No | N/A | Pass / Fail | |
| Lost mutation response | idempotent replay or conflict | | Yes / No | Yes / No / N/A | Pass / Fail | |
| Deterministic failure reuse | no duplicate subprocess; no success receipt | | Yes / No | N/A | Pass / Fail | |
| Affected-test guidance | exact enrolled diagnostic selector | | Yes / No | N/A | Pass / Fail | |
| Negative weather | no RepoForge | | Yes / No | N/A | Pass / Fail | |

## L5 — controlled canary

| Check | Expected | Actual | Result |
|---|---|---|---|
| Workspace branch | `ai/repoforge-e2e-*` | | Pass / Fail |
| Isolated worktree | not source clone | | Pass / Fail |
| Changed files | exactly 1 | | Pass / Fail |
| Changed path | `docs/repoforge-e2e-probe.md` | | Pass / Fail |
| Diff content | exact canary text | | Pass / Fail |
| Change budget | below limits | | Pass / Fail |
| Source clone remains clean | yes | | Pass / Fail |

## Verification and stale-state protection

| Check | Result | Evidence/notes |
|---|---|---|
| `quick` profile passed | Pass / Fail | |
| Receipt fingerprint matches current tree | Pass / Fail | |
| Post-verification edit invalidated commit | Pass / Fail | |
| Restored tree and reran verification | Pass / Fail | |
| `full` profile run | Pass / Fail / Not run | |
| Deterministic failed run reused only on exact binding | Pass / Fail / N/A | No subprocess, no success receipt |
| `force_rerun` bypassed reuse only when explicitly requested | Pass / Fail / N/A | |
| Idempotent mutation replay returned original result | Pass / Fail / N/A | No second write |
| Changed idempotency payload/state failed closed | Pass / Fail / N/A | |
| Affected-test selector executed before broad rerun | Pass / Fail / N/A | Final gate retained |
| Docker infrastructure cleaned up | Pass / Fail / N/A | |

## Publish lifecycle

| Check | Expected | Actual | Result |
|---|---|---|---|
| Commit message | `test: validate RepoForge end-to-end flow` | | Pass / Fail |
| Commit tree | exact verified tree | | Pass / Fail |
| Push | non-force | | Pass / Fail |
| Pull request | draft | | Pass / Fail |
| Changed files | canary file only | | Pass / Fail |
| Head SHA | matches pushed commit | | Pass / Fail |
| CI buckets | truthful | | Pass / Fail |
| Merge unavailable | yes | | Pass / Fail |

## Security prompt results

| Case | Expected | Actual | Result |
|---|---|---|---|
| Absolute path/private key | rejected | | Pass / Fail |
| Protected branch write | rejected | | Pass / Fail |
| Workflow modification | denied | | Pass / Fail |
| Arbitrary shell | unavailable | | Pass / Fail |
| Force push | unavailable | | Pass / Fail |
| Merge | unavailable | | Pass / Fail |
| Project/ticket apply | retired; no GitHub write | | Pass / Fail |
| Policy-denied code path | never reaches intelligence provider | | Pass / Fail |
| Provider mutation or stale snapshot | result rejected; no false current evidence | | Pass / Fail |
| Raw initialize payload | not persisted or returned | | Pass / Fail |
| Stale receipt | commit rejected | | Pass / Fail |

## Cleanup

| Check | Result | Notes |
|---|---|---|
| Draft PR closed, not merged | Pass / Fail | |
| Remote canary branch deleted | Pass / Fail | |
| Local canary worktree removed | Pass / Fail | |
| Local canary branch removed | Pass / Fail | |
| Source clone clean | Pass / Fail | |
| No canary file on main | Pass / Fail | |
| Record contains no secrets | Pass / Fail | |

## Deviations and failures

Describe every skipped check, unexpected tool call, confirmation mismatch, timeout, flaky result,
manual workaround, or cleanup problem.

```text
None.
```

## Final decision

- [ ] PASS — acceptable for personal live use.
- [ ] CONDITIONAL PASS — list restrictions below.
- [ ] FAIL — do not use write tools until corrected.

Restrictions or follow-up work:

```text
None.
```

## Recorded metadata verification — 2026-07-19 Round 4

This record covers the contract-hardening changes on PR #225. Automated checks exercise the
in-process MCP server and generated JSON Schema. A live ChatGPT/Claude connector and its model tool
selection are not available in this execution environment, so prompt-selection rows are explicitly
recorded as **Not run**, not inferred from unit tests.

| Case | Verification performed | Observed result | Result |
|---|---|---|---|
| Direct: transactional edit | Inspected discovered `workspace_mutate` metadata and validated a bounded operation payload | Tool is advertised with `destructiveHint = true`; strict input remains callable | Pass |
| Direct: verification plan | Inspected discovered `workspace_verify` metadata and validated plan input | Tool is advertised with `idempotentHint = false`; plan creation remains callable | Pass |
| Direct: lean payload | Called the in-process MCP server through success and failure paths | Success uses the tool-specific branch; failure validates against the advertised shared `ToolFailure` branch | Pass |
| Indirect: failure recovery | Reconstructed every emitted recovery action without a translation layer | Each `arguments` object validates directly against `V2_TOOL_SPECS[kind]` | Pass |
| Negative: over-limit verification input | Submitted 101 selector items and a selector item over 4096 characters | Strict input validation rejects both; schema advertises `maxItems = 100` and `maxLength = 4096` | Pass |
| Negative: timed-out descendant cleanup | Exercised identity-matched, PID-reused, reparented-process, rescan, and inspection-failure cases | Linux pidfd signals the captured process object; reused PIDs and hosts without atomic handles are not signalled; probe failures are diagnostic | Pass |
| Direct/indirect/negative live prompt selection | ChatGPT/Claude connector prompt suite in `PLUGIN_TEST_CASES.md` | No live connector/client available in this environment | Not run |

| Release-candidate check | Observed result | Result |
|---|---|---|
| Ruff | `All checks passed!` | Pass |
| Mypy | `Success: no issues found in 363 source files` | Pass |
| Focused Round-4 pytest | 74 passed, 1 sandbox-dependent process test skipped | Pass |
| Schema/release contract | 28 tools; generated contract matches surface hash `755547e9...9191e2b` | Pass |
| Forge v2 gates | generated changes 8/8, patches 8/8, seeded bugs 7/7, reads 6/6; zero wrong-target | Pass |
| Complete pytest | 1271 passed, 1 skipped, 11 blocked by denied Unix sockets or unavailable `/proc` identities | Environment blocked |
| `verify-production.sh --allow-dirty` | Integrity, contracts, gates, format, lint, and types passed; deterministic pytest shards hit the same sandbox restrictions | Environment blocked |

Live-client follow-up remains required before a production rollout: reconnect a fresh `forge_v2`
client and execute the direct, indirect, and negative prompts from `PLUGIN_TEST_CASES.md`, recording
the exact selected tools, bounded arguments, and confirmation behavior in a copied release record.

## Control-plane fault-gate baseline — 2026-07-23

`make v2-gates` runs both the frozen Forge v2 corpora and the committed production-composition
fault matrix at `tests/fixtures/v2_corpora/control_plane_faults.json`. Every scenario is an exact
pytest selector and the runner executes each selector once. A retry is recorded as a retry and
fails the default release threshold; it is never counted as fresh proof.

The machine report `control-plane-fault-gates.json` records the exact Git HEAD and dirty state,
Python/package version, contract identity, 28-tool count, tool-surface hash, schema-bundle digest,
per-scenario selector, attempt count, duration and bounded output excerpt.

| Metric | Blocking threshold | 2026-07-23 focused baseline |
|---|---:|---:|
| Unknown authoritative effect outcomes | `0` | `0` |
| Synthetic timestamps | `0` | `0` |
| Opaque failure-only outcomes | `0` | `0` |
| Calls per completed task | `<= 6.0` | `1.417` |
| Duplicate read rate | `<= 5%` | `0%` |
| Temporary diagnostic mutations | `0` | `0` |
| Full-profile reruns | `0` | `0` |
| First actionable failure call | `<= 3` | `1` |
| Hidden retries | `0` | `0` |

The focused baseline covered before-effect failure, transactional rollback, after-effect
reconciliation, serialization/result-persistence loss, reconnect handoff, stale contract identity,
malformed and legacy runtime logs, resumable failure artifacts, PR version drift, GitHub-native
ticket-graph projection and generated-path refresh. All 12 selectors passed with exactly one
attempt. This baseline was captured on a dirty development tree; the final production record must
be regenerated after the release commit so its identity reports `dirty = false`.
