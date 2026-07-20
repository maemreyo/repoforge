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
| `repo_list` | List bounded reviewed repositories, default refs, and capabilities. |
| `repo_task_context` | Assemble bounded repository, status, ticket, workspace, and recent-commit sections in one call. |
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
| `workspace_diff` | Return structured hunks, metrics, exact HEAD, and fingerprint. |

`workspace_refresh.action` is `preview` or `apply`. Apply requires exact preview/base/workspace bindings and explicit conflict resolutions where necessary.

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

### Durable operation and administration

| Tool | Purpose |
| --- | --- |
| `operation` | `get`, `wait`, `list`, `cancel`, or `failure_evidence` one durable-operation surface. `wait` long-polls one exact operation for 1–60 seconds and returns on a progress timestamp change, terminal state, or typed timeout; `since_updated_at` binds the caller's last observed state. Every operation evidence item includes bounded progress unit/message, `suggested_poll_after_s`, and an ETA when step totals and timing evidence permit it. Cancellation is a request and terminal state remains explicit. `failure_evidence` reads one exact private `failure_id` -- content-addressed, bounded, secret-redacted, restart-safe -- with normalized failure class, stable error code, exact pre/post identities, affected scope, and ordered typed recovery actions that never contain arbitrary command text. Each recovery action is exactly `{kind, precondition, arguments}`; `arguments` validates directly as the input of the named public tool, without a caller-side translation layer. |
| `config_inspect` | Read accepted/active configuration generations, repository facts, pending changes, runtime identity, and health. |
| `runtime_logs_read` | Read bounded redacted audit or runtime-log evidence with filters and cursors. |

`workspace_verify.mode = "plan"` additionally supports a plan lifecycle for structured multi-stage work: `plan_action = "create"` compiles reviewed profiles/diagnostics into a deterministic typed DAG and returns an immutable plan for operator review; `"accept"` admits it after revalidating every binding; `"execute"` runs it through either iteration stages or the final full boundary, returning a durable operation reference immediately (poll with `operation`). Every completed stage writes a private, bounded, content-addressed schema-v2 receipt carrying environment identity schema version and requested/effective policy hashes. A read-only iteration stage may reuse a private content-addressed schema-v2 cache entry only when workspace/input, stage definition, target identity, environment/toolchain, requested/effective policy, lockfiles, configuration, policy, and dependency receipts remain compatible; mutating and final-verification stages are always non-cacheable. A compatible legacy schema-v1 entry explains an `environment_identity_schema_changed` miss but can never grant a hit. Only the accepted plan's final verification-enabled stage can populate `last_verification`.

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
```

CLI commands are not additional MCP tools. `rf` exits `0` on success, `2` on stable validation/operation failure, and `3` when an explicit operator decision or approval is required.
