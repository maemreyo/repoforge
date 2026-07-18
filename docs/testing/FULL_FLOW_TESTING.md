# Full-flow testing for Forge v2

This runbook validates the complete RepoForge v2 chain:

```text
source and frozen dependencies
  -> typed 28-tool contracts and release corpora
  -> local Git/worktree integration
  -> forge_v2 MCP protocol and structuredContent
  -> client rediscovery
  -> journaled workspace mutation
  -> impact-routed and authoritative verification
  -> exact-tree commit and non-force push
  -> draft PR, review comments, and CI evidence
  -> rollback drill
```

The automated gate never writes to a real GitHub repository. Live issue, branch, or pull-request
writes are explicit acceptance tests and require an allowlisted repository plus its configured
external-write policy.

## 1. Test levels

| Level | Scope | External write |
|---|---|---:|
| L0 | format, lint, strict typing, schema/release contracts | No |
| L1 | deterministic v2 corpora and unit/security tests | No |
| L2 | local bare Git remote, worktree, journal and installed-wheel lifecycle | No |
| L3 | MCP protocol through `forge_v2` | No |
| L4 | ChatGPT/Claude connector rediscovery and lean payload behavior | No by default |
| L5 | controlled issue/branch/draft-PR lifecycle | Yes |

Do not use a live GitHub write as the first evidence that a mutation path works.

## 2. Required safety conditions

- Start from a clean source checkout or one exact reviewed workspace.
- Use only an allowlisted `repo_id`; never pass model-authored absolute repository paths.
- Use an `ai/*` branch and an isolated RepoForge worktree.
- Do not change denied paths, secrets, credentials, Git internals, or GitHub Actions workflows.
- Review `workspace_diff` before authoritative verification and again before commit.
- Never force-push, merge, mark a PR ready, or modify Project V2 state.
- Keep issue and PR comments bounded, redacted, evidence-bound, and idempotent.
- Record hashes, IDs, selectors, and outcomes—not source bodies, patches, credentials, or raw
  environment dumps.

## 3. L0: source, contract, and release gates

From the RepoForge source root:

```sh
uv sync --extra dev --frozen
make v2-gates
make check
```

`make v2-gates` executes the frozen generated-change, patch, seeded-bug, and read/truncation corpora.
The blocking expectations are:

- generated workspace mutations exceed 99% success with zero wrong-target applies;
- `apply_patch` exceeds 95% with zero wrong-target applies;
- every seeded regression is routed to a catching test or explicitly falls back to full;
- read/truncation correctness is 100%, and every truncated result carries resume metadata;
- primary Tree-sitter routed-test recall passes its per-language threshold;
- reports are written only to the temporary gate directory during `make check`.

`make check` additionally requires:

- release-contract and tool-schema golden files match generated output;
- Ruff formatting and lint are clean;
- strict Mypy passes;
- deterministic pytest shards pass with branch coverage at or above 80%;
- one wheel and one source distribution build;
- an isolated installed-wheel lifecycle succeeds;
- `forge_v2` exposes exactly 28 tools and the expected surface hash;
- the verification process leaves no unreviewed repository artifact.

A failure at L0 blocks every later level.

## 4. L1: targeted high-risk suites

Run focused suites while iterating so failures are attributable:

```sh
uv run --extra dev pytest tests/test_workspace_mutate.py -q
uv run --extra dev pytest tests/test_mutation_idempotency.py -q
uv run --extra dev pytest tests/test_workspace_refresh.py -q
uv run --extra dev pytest tests/test_v2_retrieval.py -q
uv run --extra dev pytest tests/test_v2_benchmark_harness.py -q
uv run --extra dev pytest tests/test_mcp_contract_v2.py -q
uv run --extra dev pytest tests/test_rollback_drill.py -q
```

Confirm positive and negative coverage for:

- all seven `workspace_mutate` operations in one transaction;
- dry-run/no-op behavior and canonical request binding;
- denied paths, traversal, NUL, symlink/gitlink, stale SHA/HEAD/fingerprint, and change budgets;
- crash/I/O fault recovery and same-key deterministic replay;
- refresh preview tokens, generated-path conflicts, resolutions, and changed-path selectors;
- read cursors without duplication or omission;
- low-confidence verification fallback;
- host-path redaction and bounded payloads;
- closed Pydantic input/output schemas and unified typed errors.

## 5. L2: local Git/worktree lifecycle

The production gate already exercises a temporary source checkout, local bare remote, isolated
worktree, verification, commit, push, and installed wheel. For a manual local smoke test, use a
throwaway repository enrolled under a short `repo_id` and perform this sequence through MCP:

1. `repo_list` to resolve the repository without guessing.
2. `repo_task_context` to read instructions and relevant state.
3. `workspace_create` with a unique task slug.
4. `workspace_status` for local/base/hygiene sections.
5. `workspace_mutate` in dry-run mode, then apply with one idempotency key.
6. `workspace_diff` and `workspace_verify` mode `plan`.
7. A narrow `workspace_verify` diagnostic or auto run.
8. The required final `workspace_verify` profile.
9. `workspace_commit` only on the exact verified fingerprint.
10. `workspace_push` to the local bare remote.
11. `workspace_remove` after the worktree is clean.

Acceptance criteria:

- the source checkout remains untouched;
- mutation and receipt commit atomically;
- any post-verification mutation invalidates commit eligibility;
- push is non-force and branch-prefix constrained;
- no stale worktree, lock, transaction journal, or temporary artifact remains.

## 6. L3: MCP Inspector protocol

Launch the reviewed inspector workflow:

```sh
./scripts/inspect-mcp.sh
```

For the active server:

1. Confirm server identity is `forge_v2`.
2. Confirm discovery returns exactly 28 names matching `tool-schemas-v2.json`.
3. Confirm every input and output schema is closed and publishes bounds/enums/patterns.
4. Call representative reads: `repo_list`, `repo_task_context`, `repo_read`, `repo_search`,
   `repo_tree`, and `repo_issue`.
5. Create a smoke workspace and call `workspace_status`, `workspace_read`, `workspace_tree`, and
   `workspace_diff`.
6. Call `workspace_mutate` dry-run with valid and invalid operations.
7. Call `workspace_verify` mode `plan`; confirm it runs no subprocess.
8. Submit undeclared fields and out-of-bound values; confirm one typed, redacted error envelope.
9. Confirm full machine data is in `structuredContent`; text content is a short summary rather than
   duplicated JSON.
10. Confirm protocol JSON-RPC stays on stdout and diagnostics stay on stderr.

For the retired identity, start the grace server and verify `forge_v1` exposes exactly one tool:
`migration_required`. Its response must name `forge_v2`, the expected surface hash, and a reconnect or
rediscovery action.

## 7. L4: client cutover and payload behavior

Before testing a primary client, remove the old connector configuration and create a new connection
whose identity is `forge_v2`. Start a new conversation; do not rely on an existing session’s cached
roster.

Run the direct, indirect, and negative prompts in `PLUGIN_TEST_CASES.md` and record:

- discovered identity and tool count;
- representative input schemas and confirmation classification;
- whether complete structured results are available to the client;
- whether text content remains a short summary;
- whether the client asks for nonexistent Forge v1 tools;
- runtime/client surface hashes and rediscovery guidance;
- connector errors separated from engine execution evidence.

A client that cannot consume `structuredContent` must use the deployment-level legacy text-duplication
compatibility setting; this is not a per-call choice and does not restore the Forge v1 tool surface.

## 8. L5: controlled GitHub acceptance

Use a disposable or explicitly approved repository. Ensure policy enables only the external writes
needed by the test.

### 8.1 Issue field test

Use `repo_issue` to:

1. read/spec an issue and capture current evidence;
2. post one bounded comment with an idempotency key;
3. retry the exact comment key and confirm no duplicate is posted;
4. close only with an explicit evidence reference;
5. reopen or link only when the per-repository policy enables that action;
6. confirm Project V2 remains read-only.

Check the policy ceilings for writes per call and per time window. A bare close, disabled operation,
stale approval, changed idempotency payload, or rate overflow must fail closed.

### 8.2 Branch and draft PR field test

1. Create an isolated workspace for a canary change.
2. Mutate and review the exact diff.
3. Run authoritative verification.
4. Commit the exact verified tree.
5. Push without force.
6. Use `workspace_pr` action `create_draft`.
7. Use `workspace_pr` action `watch` and retrieve progress through `operation`.
8. Use `workspace_pr_evidence` for checks, annotations, and bounded failure packets.
9. When approved, use `workspace_pr` action `comment` to respond with evidence.
10. Stop before ready/merge; RepoForge has no merge tool.

After recording evidence, close the disposable PR and remove its remote branch through normal operator
GitHub controls.

## 9. Rollback drill

Run:

```sh
uv run --extra dev python scripts/rollback_drill.py
```

The drill must prove both `forge_v2 -> forge_v1` and `forge_v1 -> forge_v2` transitions against the
same bounded persistent-state snapshot, with unchanged workspace/audit/config-generation state and no
stuck rediscovery status. The old identity is a rollback/grace artifact, not a supported parallel
production surface.

## 10. Release record

Record at minimum:

- source HEAD and branch;
- `release-contract-v2.json` and tool-schema hashes;
- `forge_v2` tool-surface hash;
- v2 corpus report summary;
- full verification profile and exact fingerprint;
- wheel version and installed-wheel smoke result;
- rollback drill result;
- client identity/tool count and structuredContent result;
- live issue/PR IDs only when L5 was explicitly approved;
- unresolved uncertainty or skipped live checks.

Do not declare cutover complete merely because source tests pass. Completion requires the exact final
tree, the release gates, the connector identity, consumer rediscovery evidence, and the governed issue
reconciliation required by the release ticket.
