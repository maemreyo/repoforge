# RepoForge Forge v2 tool reference

RepoForge exposes **exactly 28** static MCP tools under connector identity `forge_v2`. The Pydantic registry in `repoforge.contracts.registry` is authoritative for discovery, runtime input validation, runtime output validation, the generated schema bundle, and release-contract drift checks.

The retired connector identity `forge_v1` exposes only `migration_required`. It reports the expected `forge_v2` surface hash, records the stale caller, requests shutdown of the retired process, and instructs the operator to reconnect so the client rediscovers the new manifest. There is no client-selected v1/v2 negotiation and no public alias window after the cutover.

## Result and error contract

Successful calls return a short human-readable text block plus the complete typed object in MCP `structuredContent`. The structured object is the source of truth. Full JSON duplication into text is disabled by default; operators may temporarily restore it with the deployment-only `server.legacy_text_result_duplication` compatibility setting.

Every advertised tool output is the closed union of its tool-specific success model and the shared
`ToolFailure` model. Success models include:

- `status`: exactly `ok`;
- `summary`: a bounded human-readable result;
- `error`: exactly `null`.

Failures use `status = "failed"`, a bounded summary, and one typed, redacted `ToolError` envelope
with a stable error code, explanation, retryability, correlation ID, safe next action, and bounded
details. Runtime success and failure payloads are validated against their respective branch before
they are returned. Public results, errors, audit entries, and traces must not expose absolute host
paths, credentials, or arbitrary process output.

## Recommended agent flow

1. Call `repo_list` to discover reviewed repositories and capabilities.
2. Call `repo_task_context` with an issue number and/or existing workspace ID.
3. Create one isolated worktree with `workspace_create` when mutation is required.
4. Inspect with `workspace_status`, `workspace_read`, `workspace_search`, `workspace_tree`, and `workspace_diff`.
5. Apply all filesystem changes through `workspace_mutate`.
6. Use `workspace_verify` for planning, targeted diagnostics, quick iteration, and the final full gate.
7. Commit only an exact verified fingerprint with `workspace_commit`.
8. Publish with `workspace_push`, then use `workspace_pr` and `workspace_pr_evidence` for draft-PR lifecycle and CI evidence.
9. Use `operation` to inspect or request cancellation of durable work.

RepoForge never merges, force-pushes, writes protected branches, exposes arbitrary shell execution, or treats client capabilities as repository authority.

## Exact 28-tool roster

### Repository context and reads

| Tool | Purpose |
| --- | --- |
| `repo_list` | List bounded reviewed repositories, default refs, capabilities, and which verification profiles a model may start (`model_invocable_profiles`) versus which the reviewed configuration reserves for the operator (`operator_only_profiles`). |
| `repo_task_context` | Assemble bounded repository, status, ticket, workspace, and recent-commit sections in one call. The `repository` section carries `model_invocable_profiles` and `operator_only_profiles`. |
| `repo_read` | Read up to 20 UTF-8 files from one immutable snapshot with independent ranges, one global byte budget, and resumable cursors. |
| `repo_search` | Search literal text, reviewed regular expressions, or file names in one immutable snapshot. |
| `repo_tree` | List a bounded snapshot tree, optionally below one subtree. |
| `repo_history` | Read one commit, list history, or compare refs through the `mode` field. |
| `repo_pr_read` | Read bounded pull-request overview, files, checks, or reviews with explicit freshness. |

`repo_history.mode` is one of `commit`, `log`, or `compare`. Compare mode requires base and head refs. All repository reads are snapshot-bound and policy-filtered.

`repo_list`'s optional `requested_repo` hint resolves deterministically to `selection.outcome`: `exact_match` or `single_enrolled` proceed without asking; `input_required` returns bounded `candidates` plus a `selection_prompt` that is present and identical whether or not the client negotiated Elicitation; `no_match` means nothing enrolled matches. Never guess a `repo_id` when the outcome is `input_required`.

### GitHub-native issues and repository policy

| Tool | Purpose |
| --- | --- |
| `repo_issue` | Read/spec/graph/next operations and governed issue mutations behind one accurately annotated composite. |
| `repo_policy` | Preview or apply one exact-state-bound repository policy proposal. |

`repo_issue.mode` supports `read`, `spec`, `graph`, `next`, `comment`, `close`, `reopen`, `link`, and `create`. Mutating modes require the fields declared by the strict schema, including evidence and idempotency where applicable. GitHub-native sub-issues and blocked-by relationships are authoritative; a checked-in ticket graph is not required for live readiness.

`graph`, `next`, `read`, and `spec` results include `capability_coverage`: per-capability completeness (`issue`, `sub_issues`, `comments`, `dependencies`, `project_overlay`) with any affected issue numbers and whether that capability's read was truncated, so a caller can tell exactly which GitHub read is missing instead of one blanket evidence flag.

`graph` and `next` results include `read_stats`: `provider_processes` (`gh` subprocess launches against GitHub; a process count, not an HTTPS request count — the transport is not directly instrumented, and higher-level operations such as `gh project item-list` may perform more than one network request), `captured_stdout_bytes`, and `provider_process_duration_ms`, broken down per capability, plus `source` (`live_full` for a fresh batched read, `cache` for a TTL cache hit that performed no provider calls). Cache hits additionally report `cache_hit_reason` and `cache_age_ms`; live reads report `cache_miss_reason` so telemetry never has to guess why a result was not served from cache. Graph reads are batched through GraphQL aliases instead of one `gh api` process per issue, so a fresh 40-node graph uses a handful of provider processes rather than one per node; top-level totals are exact, while per-capability entries are shared attribution (one batched request is counted for every capability it carried) and must not be summed. When status or priority metadata is missing on an issue, the graph applies a safe default (`Backlog`/`P3`) and reports one aggregated `METADATA_DEFAULTED` diagnostic listing the missing fields; a metadata gap never marks the issue provider unavailable. Cross-repository sub-issue or blocker references are never hydrated onto the same number in the local repository: they degrade the affected capability and report `CROSS_REPOSITORY_RELATION_UNSUPPORTED`. A Project overlay read failure is isolated to `project_overlay` — it is reported as an unavailable capability, never as traversal truncation.

`repo_policy.action` is `preview` or `apply`. Preview returns a state-bound token and normalized changes. Apply accepts the token only; it recomputes and rejects stale or mismatched state. Restrictions may activate immediately. Capability expansions remain pending until an operator approves them in the terminal.

### Workspace lifecycle and inspection

| Tool | Purpose |
| --- | --- |
| `workspace_create` | Create or idempotently reuse one isolated `ai/*` worktree. |
| `workspace_remove` | Remove one local worktree; remote branches and pull requests remain untouched. |
| `workspace_list` | List bounded workspace lifecycle, age, repository, branch, dirty state, and issue IDs. |
| `workspace_refresh` | Preview or apply a base refresh with typed conflict evidence and explicit resolutions. |
| `workspace_status` | Read selected local, base, and hygiene sections plus exact HEAD and fingerprint. |
| `workspace_format_changed` | Run one reviewed formatter over policy-allowed changed files. |
| `workspace_read` | Read up to 20 workspace files with independent ranges, byte budgets, partial-error evidence, and cursors. |
| `workspace_search` | Search reviewed workspace files without exposing a shell. |
| `workspace_tree` | List a bounded policy-filtered workspace tree. |
| `workspace_diff` | Return hunk-free per-file summaries by default, or bounded structured hunks on explicit opt-in. |

`workspace_refresh.action` is `preview` or `apply`. Apply requires exact preview/base/workspace bindings and explicit conflict resolutions where necessary.

#### Status reads during a running command

`workspace_status` never waits out a running verification. It waits at most `server.status_read_lock_timeout_seconds` (default `0.5`) for the workspace lock and then answers regardless, reporting which happened in `read_consistency`:

- `locked` — the read held the workspace lock and is exclusive of RepoForge writers. This is the normal case and the previous behavior.
- `concurrent_write` — a command was running in that workspace and kept the lock. Every returned fact was true when sampled, but the facts are not guaranteed to describe one instant, and `workspace_fingerprint` must not be used as the exact-state binding for `workspace_mutate` or `workspace_commit`. Read status again after the operation reaches a terminal state to get a `locked` read.

#### Summary-first diff review

Start with the cheap default summary:

```json
{"workspace_id":"demo-workspace"}
```

Inspect the returned paths and change counts, then request hunks only for a selected file:

```json
{
  "workspace_id":"demo-workspace",
  "include_hunks":true,
  "path_glob":"src/repoforge/application/workspace/retrieval.py",
  "max_files":1
}
```

Default `files[*].hunks` is empty. `change_metrics` always describes the whole workspace, while `files` is the filtered and paginated selection. Follow `next_cursor` when present. The 120 KB `byte_budget` maximum is a safety cap, not a target response size; keep hunk requests narrow.

### Mutation and verification

| Tool | Purpose |
| --- | --- |
| `workspace_mutate` | The only public filesystem mutation tool. It executes an atomic journaled batch under exact HEAD and fingerprint preconditions. |
| `workspace_verify` | Plan and run impact-routed diagnostics, reviewed profiles, or approved ad-hoc verification. |

`workspace_mutate.operations` supports:

- `replace_text` with exact occurrence and SHA preconditions;
- `write` with expected SHA;
- `create` with reviewed mode;
- `delete` with expected SHA;
- `move` with expected source SHA;
- `apply_patch` for normalized reviewed patch formats;
- `restore` for selected uncommitted paths.

A batch is all-or-nothing. The transaction journal is private, bounded, recoverable after interruption, and never becomes Git-visible state. `dry_run` returns typed diagnostics without changing files.

Every response also includes advisory `syntax_diagnostics` for the planner's final changed, non-deleted virtual files. Pinned Tree-sitter grammars cover Python, JavaScript, JSX, TypeScript, and TSX. `state = "ok"` means all analyzed files parsed and `parse_ok = true`; `state = "error"` returns bounded `{path, line, message, severity}` items and makes the response summary prominently include `parse_ok=false`; `state = "unknown"` uses `parse_ok = null` when a grammar is unavailable, UTF-8 is invalid, parsing raises, or the observed 100 ms/file budget is exceeded. Diagnostics never block or roll back an otherwise valid mutation. The section is capped at 100 diagnostics with an explicit `truncated` marker, and source bodies and absolute host paths are never returned. Keyed receipt schema v2 replays the same evidence; historical v1 receipts remain readable and return explicit `legacy_receipt = true` unknown evidence rather than an implicit pass.

Because `workspace_mutate` can delete or restore content, its tool-wide MCP annotation is
`destructiveHint = true`, including when a particular invocation is a dry run.

`workspace_verify.mode` is `plan`, `auto`, `diagnostic`, `profile`, or `adhoc`:

- `plan` returns the assessment, selected route, uncertainty, and recommended steps without execution;
- `auto` uses provider evidence and falls back to the full profile when confidence is insufficient;
- `diagnostic` runs one enrolled typed diagnostic;
- `profile` runs a reviewed repository profile;
- `adhoc` accepts only allowlisted runners under relaxed policy.

`mode = "adhoc"` returns an `adhoc_evidence` section, absent for every other mode. It carries the runner's own policy facts: the declared `mutability`, the inferred `command_class`, `content_inspected`, `fingerprint_changed`, `read_only_violation`, the bounded `changed_paths`, and `network_policy`.

`content_inspected` is the field to read first, because RepoForge content-inspects `git` argv only. A `git` run is checked before any process starts: force, mirror, and delete pushes, history rewrites, `reflog expire`, `update-ref -d`, `clean --force`, and every `--exec` form are refused with `ADHOC_COMMAND_FORBIDDEN`, and a mutating form additionally requires `mutability = "workspace"` with both `expected_head_sha` and `expected_fingerprint`. Every other runner is opaque: `command_class` is `null`, `content_inspected` is `false`, and those guards never ran.

That distinction matters most when an operator adds a shell (`bash`, `sh`) to `repositories.<id>.adhoc_runners`. Nothing blocks it, and it is the direct way to give an agent pipes, `&&`, and globbing — but `["bash", "-c", "git push --force …"]` is a shell invocation, not a git one, so the git argv guards do not apply to what the shell then runs. Under an opaque runner the remaining protections are the exact-state lock, the fingerprint comparison, and `read_only_violation`.

### Running programs that do not fit in an argv

An argv element carries at most 512 characters and rejects newlines, so a multi-line program belongs in a file the runner reads rather than in a `bash -c` string. Put it in a **gitignored scratch directory** — a convention, not a feature:

```
# .gitignore
.rf-scratch/
```

The workspace fingerprint is `HEAD`, `git diff HEAD`, and `git ls-files --others --exclude-standard`. That last flag is the point: a gitignored file contributes nothing to the fingerprint. A script written to `.rf-scratch/` therefore does not change the fingerprint, does not appear in `changed_paths` or `workspace_diff`, does not require `mutability = "workspace"`, and cannot leak into a commit — while persisting across calls, which nothing else in the ad-hoc surface does. Write it with `workspace_mutate`, which consults `allowed_paths`/`denied_paths` and not `.gitignore`, then run `["bash", ".rf-scratch/run.sh"]`.

A script that should be reviewed belongs in the tree instead, where the diff shows it.

### Standard input

`stdin_text` supplies standard input to a `mode = "adhoc"` command and is rejected for every other mode; without it the command gets no input at all. Unlike an argv element it may contain newlines, which is what makes `["git", "apply", "-"]` usable. It is bounded at 64000 characters; larger input belongs in a file the command reads. Only its length is audited, never its content.

`read_only_violation` is true when a command classified or declared read-only nonetheless changed the workspace fingerprint. It means the run's own account of what it touched is unreliable: re-read `workspace_status` rather than trusting the classification.

Diagnostic failures publish up to 100 complete structured pytest node IDs even when their bounded excerpt truncates. A truncated failed command also returns a content-addressed `failure-output:<sha256>` reference backed by a private 0600 artifact. `rerun = "failed"` is valid only with explicit diagnostic mode and a diagnostic ID; it restores the exact last failure set, forces real execution instead of deterministic failure replay, keeps the same `failure_chain_id`, and refuses with typed stale-workspace evidence when the fingerprint changed. `failure_expectation` distinguishes valid expected TDD RED evidence from unexpected failures in audit and tool output.

Only a successful verification-enabled profile on the exact current fingerprint satisfies the commit gate. A low-confidence or unavailable code-intelligence provider broadens verification; it never narrows a safety gate.

Execution-capable results expose `execution_evidence`. Requested network/filesystem values describe reviewed intent; effective values and the per-control enforcement assessment describe actual backend behavior. The native backend normally reports host-inherited network and host-account filesystem access with advisory enforcement, even when the request is offline or workspace-scoped. Unsupported CPU, memory, disk, subprocess-count, and network-byte controls are never presented as enforced. Treat `execution_evidence` as authoritative over legacy policy labels.

Verification receipts bind environment identity plus requested/effective policy hashes. Immediately before commit, RepoForge recompiles the same profile request and re-inspects the current backend. A PATH, toolchain, adapter, effective-policy, or configuration change makes the receipt stale; run one fresh authoritative profile on the exact tree to recover.

Each `workspace_verify.selector`, `selector2`, and `argv` collection accepts at most 100 items, and
each item is limited to 4096 characters. The limits are present in the advertised JSON Schema as
well as runtime validation. Because `mode = "plan", plan_action = "create"` allocates a new plan,
the composite tool's MCP annotation is `idempotentHint = false` even though other modes may be
idempotent for the same inputs.

### Commit, push, draft PR, and CI evidence

| Tool | Purpose |
| --- | --- |
| `workspace_commit` | Commit the exact verified tree under optimistic HEAD/fingerprint checks. |
| `workspace_push` | Push the current workspace branch with state-bound retry evidence. |
| `workspace_pr` | Create/update/comment/watch a draft PR, or request reviewed close/reopen operations. |
| `workspace_pr_evidence` | Read overview, check detail, failure evidence, annotations, and delta tokens. |

`workspace_pr.action` includes `create_draft`, `update`, `comment`, `watch`, `close`, and `reopen`. It never merges. Watch operations use bounded polling and durable operation evidence. `workspace_pr_evidence` requires exact selectors for check-level or failure-level detail and redacts credentials, denied paths, and unbounded logs.

### Selecting the identity a write acts as

The six tools whose calls can act as a repository identity — `workspace_create`, `workspace_commit`, `workspace_push`, `workspace_refresh`, `workspace_pr`, and `repo_issue` — accept two optional selector fields:

| Field | Values | Default |
| --- | --- | --- |
| `auth_profile` | a declared auth profile id, or `auto` | `auto` |
| `actor_class` | `human`, `agent` | `human` |

`auto` succeeds only when exactly one declared profile is deterministically eligible; ambiguity and a missing or disabled candidate fail closed rather than picking by order. An explicit profile passes the same binding, role, capability, transport, author, signer, and publication checks, and can never override an exact repository binding. Because the defaults are the deterministic ones, a caller that omits both fields behaves exactly as before selectors existed.

A selector applies only where a write happens. `workspace_pr action = "watch"` and the read-only `repo_issue` modes (`read`, `spec`, `graph`, `next`) **reject** an explicit selector instead of accepting and ignoring it, which would imply the read ran as a chosen identity. Read-only tools reject it as an undeclared field. Every `workspace_refresh` action keeps the selector, including `preview`, because it fetches the base through the pinned transport.

A selector is an input to choosing an identity, never a result field. It appears in an output schema only inside a typed recovery action's `arguments`, where a suggested retry has to carry the same identity choice as the call that failed. See `docs/development/REPOSITORY_IDENTITY.md` for the surfaces, the operator commands, and the reviewed configuration these ids refer to.

### Durable operation and administration

| Tool | Purpose |
| --- | --- |
| `operation` | `get`, `wait`, `list`, `cancel`, or `failure_evidence` one durable-operation surface. `wait` long-polls one exact operation for 1–300 seconds and returns on terminal state or typed timeout, plus a progress timestamp change when `until="progress"` (the default); `since_updated_at` binds the caller's last observed state. Every operation evidence item includes bounded progress unit/message, `suggested_poll_after_s`, and an ETA when step totals and timing evidence permit it. Cancellation is a request and terminal state remains explicit. `failure_evidence` reads one exact private `failure_id` -- content-addressed, bounded, secret-redacted, restart-safe -- with normalized failure class, stable error code, exact pre/post identities, affected scope, and ordered typed recovery actions that never contain arbitrary command text. Each recovery action is exactly `{kind, precondition, arguments}`; `arguments` validates directly as the input of the named public tool, without a caller-side translation layer. |
| `config_inspect` | Read accepted/active configuration generations, repository facts, pending changes, runtime identity, and health. |
| `runtime_logs_read` | Read bounded redacted audit or runtime-log evidence with filters and cursors. With `source="failure_artifact"` and an `artifact_reference` it returns the complete persisted stdout and stderr of a failing command — the retrieval a failure whose selectors could not be extracted actually needs. |

`workspace_verify.mode = "plan"` additionally supports a plan lifecycle for structured multi-stage work: `plan_action = "create"` compiles reviewed profiles/diagnostics into a deterministic typed DAG and returns an immutable plan for operator review; `"accept"` admits it after revalidating every binding; `"execute"` runs it through either iteration stages or the final full boundary, returning a durable operation reference immediately (poll with `operation`). Every completed stage writes a private, bounded, content-addressed schema-v2 receipt carrying environment identity schema version and requested/effective policy hashes. A read-only iteration stage may reuse a private content-addressed schema-v2 cache entry only when workspace/input, stage definition, target identity, environment/toolchain, requested/effective policy, lockfiles, configuration, policy, and dependency receipts remain compatible; mutating and final-verification stages are always non-cacheable. A compatible legacy schema-v1 entry explains an `environment_identity_schema_changed` miss but can never grant a hit. Only the accepted plan's final verification-enabled stage can populate `last_verification`.

#### Recovering a call whose response was lost

A dropped connection, a 502, or a terminated session between effect and response leaves the caller
without its result -- not without its effect. Every mutating call is a durable operation, so the
answer to "did my write land?" is a read, never a blind retry:

1. `operation` with `action="list"` and `scope="workspace:<workspace_id>"` lists that workspace's
   operations. `kind` is the tool name (`workspace_mutate`, `workspace_commit`, ...), and `state`
   says whether it reached a terminal state.
2. `operation` with `action="get"` and that `operation_id` returns the durable result the lost
   response would have carried, including exactly which paths changed.

Re-sending the mutation instead risks applying it twice; the exact-state preconditions
(`expected_workspace_fingerprint`, `expected_head_sha`) will usually refuse the second attempt, but
a refusal is not evidence about the first. Read the operation. When a response *is* received, its
`outcome` carries the `operation_id` and `receipt_id` for exactly this purpose -- record them.

#### Waiting without polling

`wait` takes `until`, and the choice decides how many round trips a long operation costs:

- `until="terminal"` returns only when the operation finishes. A timeout is the ordinary outcome
  for a long gate, so the response still carries current state, `suggested_poll_after_s`, and an
  ETA when available — call again with the same arguments. Use this whenever the only question is
  the outcome.
- `until="progress"` (default, unchanged) returns on the next durable progress delta. Background
  profile execution emits one at each step start and completion, so an eight-step gate wakes the
  caller sixteen times. Use it only when intermediate steps change what you do next.

`timeout_seconds` accepts 1–300. The ceiling is set by the client, not by RepoForge: a held request
dies with the connector. Progress notifications keep the open request alive while work continues,
and `since_updated_at` makes a dropped wait resumable, so a re-issued `wait` never loses its place.
Spinning on `action="get"` is always the wrong shape — it burns a round trip per sample and learns
nothing `wait` would not have delivered.

A wait response sets `changed_since=true` when durable progress advanced, or returns terminal evidence immediately. A bounded timeout sets `timed_out=true` while still returning the complete slim current operation evidence and pacing hint; it never returns an empty payload. Background profile execution emits one progress update at each step start and completion, not per test, so `updated_at` acts as a liveness heartbeat without unbounded write volume.

Operational and configuration tools never grant authority based on a model or client declaration. Expansion approval tokens remain outside the conversation.

## Connector identity, migration, and rollback

### Moving from Forge v1

1. Install the reviewed wheel containing the v2 contract.
2. Stop the managed `forge_v1` runtime.
3. Start the managed runtime normally; the worker serves `forge_v2` only.
4. Reconnect or recreate the ChatGPT/Claude connector so it rediscovers the manifest.
5. Confirm `config_inspect` reports identity `forge_v2`, tool count 28, and the expected surface hash.

A stale `forge_v1` connection can call only `migration_required`; old tool calls are intentionally unavailable.

### Rollback drill

Run the read-only compatibility drill before release or emergency rollback:

```bash
uv run --extra dev python scripts/rollback_drill.py
```

The drill verifies `forge_v2 → forge_v1 grace → forge_v2`, checks both surface hashes, and proves the selected persistent-state files retain identical digests. It does not mutate production state.

Rollback to a last-v1 artifact requires stopping v2, installing the reviewed last-v1 wheel/tag, starting only the grace-compatible runtime, and reconnecting the client. Return to v2 by reinstalling the v2 wheel and rediscovering `forge_v2`. Configuration generations, workspaces, audit data, and durable operations remain schema-compatible in both directions; no migration may be one-way.

## Release contracts and gates

- `docs/contracts/tool-schemas-v2.json` is the byte-stable complete schema bundle for all 28 tools.
- `docs/contracts/release-contract-v2.json` is the compact public release manifest: identities, exact names, per-tool metadata/schema hashes, schema-bundle hash, CLI contract, runtime protocol, and configuration versions.
- `make v2-gates` executes frozen generated-change, patch, seeded-bug, read/resume, and provider-recall corpora without leaving repository artifacts.
- The syntax-gate acceptance test reuses the frozen generated-change corpus and enforces an in-process p95 budget of at most 100 ms per supported-language file.
- `make check` runs release-contract validation, `make v2-gates`, formatting, lint, strict typing, deterministic pytest shards with branch coverage, source/wheel builds, and isolated installed-wheel lifecycle verification.
- Every release is probed before activation: the packaged contract identity (`generated_contract_identity.py`) must match the in-process registry (input/output/schema-bundle digests). A mismatch refuses activation with `RELEASE_CONTRACT_IDENTITY_MISMATCH` before `current` moves; the release stays on disk as a forensic artifact. The supervisor runs the same check as a preflight and fails closed in place (`FAIL_CLOSED` record, control plane answerable, no child spawn, no launchd crash-loop) instead of retrying a child that can never start.

Any intended public drift requires an explicit compatibility review and regenerated golden contract. Additive output fields still require tolerant readers; removed or renamed tools require a new reviewed identity/contract rather than hidden aliases.

## Deliberately unsupported capabilities

Forge v2 does not expose:

- arbitrary shell commands or unrestricted filesystem paths;
- direct source-clone writes or protected-branch writes;
- merge, force-push, workflow dispatch, check rerun, or repository administration;
- secrets, environment dumps, absolute host paths, raw provider queries, or provider instructions;
- caller-controlled policy expansion or implicit approval;
- client-selected legacy contracts;
- direct GitHub Project V2 mutation as a public MCP capability.

## Operator CLI

The `rf` CLI remains the operator surface for setup, reviewed approval, runtime lifecycle, diagnostics, and local recovery. Common commands are:

```bash
rf onboard /path/to/repository --non-interactive --defaults --local
rf runtime status
rf runtime logs --tail 20
rf config pending
rf config approve CHANGE_ID --activate auto
rf diagnostics bundle
rf upgrade reconcile --repair rollback   # repair a fail-closed activation (never default)
```

CLI commands are not additional MCP tools. `rf` exits `0` on success, `2` on stable validation/operation failure, and `3` when an explicit operator decision or approval is required.

`rf doctor` additionally reports `runtime_contract_identity` with field-level detail (active `release_sha`, `manifest_contract_identity`, `packaged_contract_identity`, `computed_registry_identity`, `mismatched_fields`, `artifact_paths`, `safe_next_action`) and exits non-zero when a fail-closed runtime or an inconsistent release is detected.

`rf runtime ls` and `rf doctor` also report `execution_workers` evidence: `stale_execution_worker_count`, `workers_by_release`, `worker_pids`, `owner_supervisor_state`, `locks_held` (from lock-file metadata plus PID identity), `reclamation_safe`, `scan_complete`, `unreadable_record_ids` (registry records that could not be decoded, reported by id so they are never silently dropped), `orphaned_group_without_leader` (dead leader with live process-group members that may still hold locks), and `containment_unproven` (leader gone but no group probe available). `reclamation_safe` is only ever True on complete, readable, provable evidence: a tokenless binding is always unsafe, as is a truncated scan, an unreadable record, an orphaned group, or unprovable containment; when the `execution_workers` check fails, `rf doctor` exits non-zero. The unreadable-record evidence ships as a bounded trio — `unreadable_record_count`, `unreadable_record_ids_sample` (≤8 ids; the JSON payload never ships the full unbounded id list), and `unreadable_record_ids_truncated` — so the doctor payload stays bounded at any registry size. Every release switch, rollback, or upgrade reclaims the departing release's execution workers (PID-reuse-safe, exact entry-point classification, TERM→KILL with bounded wait) before the new supervisor starts; the reclamation evidence rides the activation/rollback receipt (`worker_reclamation`) as a **bounded summary** (`inspected`/`reclaimed`/`already_gone`/`refused_unproven`/`survived_kill`/`evidence_complete`, a ≤8-worker sample, `evidence_digest`, `evidence_reference`) whose full evidence lives in an immutable `worker-reclamation/<id>.json` artifact written before the receipt — so a receipt can never exceed its 4 KiB evidence cap, even at incident scale (92 workers measured ~7.8 KiB of raw evidence).

Execution-worker registration is mandatory: `start()` returns a live worker only after its durable lease is on disk. If the lease cannot be written (store failure, unreadable supervisor identity), the spawned worker is TERM'd, awaited, KILL'd, and confirmed dead, the spawn raises `EXECUTION_WORKER_REGISTRATION_FAILED`, and the supervisor enters resident `FAIL_CLOSED` instead of starting a replacement. The registry holds only active leases; when a lease reaches a terminal state (`reclaimed`/`already_gone`) it is archived to the worker history (`runtime-execution-workers-history/`, bounded retention) and removed from the active registry, so the bounded scan covers concurrent workers — never the number of workers that ever existed — and terminal leases left by older releases are archived by the reconciler on its next pass.

Reclamation fails closed: a worker whose identity cannot be proven while it may still run blocks the replacement (`STALE_EXECUTION_WORKER_IDENTITY_UNPROVEN`), a SIGKILL survivor blocks it (`STALE_EXECUTION_WORKER_RECLAMATION_FAILED`), a truncated registry scan blocks it (`EXECUTION_WORKER_REGISTRY_SCAN_INCOMPLETE`), and an unreadable registry record blocks it (`EXECUTION_WORKER_REGISTRY_UNREADABLE_RECORDS`). Every switch, rollback, or upgrade runs a **read-only handoff preflight before any stop or `current` swap**: if the registry cannot support the reclamation, the activation/rollback is refused while the healthy runtime keeps serving (`ACTIVATION_PREFLIGHT_FAILED` / `ROLLBACK_PREFLIGHT_FAILED`) instead of stopping the incumbent first and then discovering the replacement cannot start. The supervisor's own startup reconciliation enters resident `FAIL_CLOSED` on the same uncertainty instead of spawning a contending replacement. Only `running` bindings with a recorded start token are ever auto-reaped; a `running` binding without a start token is invalid for new writes, pre-token records read back as `legacy_unproven`, and tokenless/terminal bindings are never reaped by pattern.

Fail-closed reclamation needs a safe recovery path, so `rf runtime worker-registry` offers one: `inspect <record-id>` reports a record's parseability, content digest, recovered pid, and whether that process still lives; `quarantine <record-id> --reason <text>` moves the record's bytes into a private `runtime-execution-workers-quarantine/` directory (never deletes) and writes a durable repair receipt — refused when the record describes a live process unless `--force` (the explicit operator override); `reconcile` re-reports whether the registry evidence (`scan_complete`, `unreadable_record_ids`, `restart_permitted`) would allow a runtime replacement after the repair.

`rf upgrade reconcile --repair rollback` authorizes only when the observed runtime is provably fail-closed on a deterministic contract failure (phase `fail_closed`, `fail_closed_since` set, allowlisted `CONTRACT_ARTIFACT_MISMATCH`/`RELEASE_CONTRACT_IDENTITY_MISMATCH`, and serving the journal target). Ordinary `rf upgrade rollback` re-smokes its target before the symlink moves; `--force-unverified` is the explicit escape hatch that skips only that pre-swap probe — the receipt still requires observed convergence after restart.
