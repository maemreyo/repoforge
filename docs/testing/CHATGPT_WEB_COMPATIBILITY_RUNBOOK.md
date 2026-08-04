# ChatGPT Web / Secure MCP Tunnel Compatibility Runbook (#404)

## Why this document exists

Issue #404 gates several already-landed pieces of work: the tool-count mechanics behind
`workspace_exec` (#376), the deprecation of `workspace_verify(mode="adhoc")`, and the
usability claims made for #377/#443 (shell-script execution, argv sequences, output
artifacts, the progress heartbeat). All of that has passing CI — unit tests, contract
tests, `live-activation` — but **CI green does not prove a real ChatGPT Web session can
discover and use the new contract.** This document is the missing half: a step-by-step
manual runbook an operator runs against a real ChatGPT Web session over a real Secure
MCP Tunnel, since no automated agent in this repo's CI (or in a coding-assistant CLI
session) has access to that platform.

Run this after activating a RepoForge runtime built from this branch (or from `main`
once this PR merges) and connecting it to ChatGPT Web through your Secure MCP Tunnel.
Record the actual observed result in the `Result` column of every table below — don't
just check the box. A blank or "assumed" result is not evidence.

## 0. Prerequisites

- [ ] The running RepoForge instance is activated from a build that includes this PR's
      commits (`5dbd3c2`, `c3f0ee3`, and the `main` merge). Confirm from a terminal with:
      ```bash
      rf show-config
      ```
      or ask ChatGPT to call the `config_inspect` MCP tool. Either way, check the reported
      `tool_surface_hash` / release-contract identity matches
      `docs/contracts/release-contract-v2.json` on this branch (29 tools).
- [ ] At least one repository is enrolled with `execution_mode = "relaxed"` and a
      non-empty `adhoc_runners` (e.g. `["python3"]`), so the execution-form tests below
      have something to run.
- [ ] Decide whether you're testing from a **brand-new** ChatGPT Web conversation, an
      **existing** one that predates the runtime restart, or both — record which for
      every section, since "does an existing session see the new contract" is itself one
      of the questions this runbook answers.

## 1. Tool discovery and schema evolution

| # | Step | Expected | Result |
|---|------|----------|--------|
| 1.1 | Start a **new** ChatGPT Web conversation, connect the connector, ask it to list its available tools. | 29 tools total, including `workspace_exec`. | |
| 1.2 | In an **existing** conversation open before the runtime restart, ask it to list tools again (without reconnecting). | Either it already reflects 29 tools (auto-refresh), or it still shows the old count/shape until you explicitly reconnect. Record which. | |
| 1.3 | If 1.2 showed stale tools, reconnect/refresh the connector in that same conversation and re-check. | Tool list updates to 29 without losing conversation context. | |
| 1.4 | Ask it to describe `workspace_exec`'s input schema. | Lists `argv`, `argv_sequence`, `script`, `shell`, `working_directory`, `stdin_text`, `expected_fingerprint`, `expected_head_sha`, `mutability`, `background`. | |
| 1.5 | Ask it to describe `repo_policy`'s `execution` field shape (or call `repo_policy` preview with an empty `execution: {}`). | `adhoc_shell_runners` is listed alongside `execution_mode`, `adhoc_runners`, `adhoc_timeout_seconds`. | |
| 1.6 | Ask it to call `config_inspect` and report the `tool_surface_hash`/contract identity. | Value is stable across repeated calls in the same session; changes only after a real config/contract change. | |
| 1.7 | (If you can trigger it) restart the RepoForge runtime mid-session, then make any tool call. | Either the call fails with a clear, typed error naming a stale contract/generation, or it transparently reconnects — not a silent wrong-schema call. | |

## 2. The three `workspace_exec` execution forms

All three forms are mutually exclusive on one call — asking ChatGPT to combine them
should be refused by the tool itself, not by ChatGPT's own judgment.

### 2.1 `argv` (baseline form, existed since #376)

| # | Step | Expected | Result |
|---|------|----------|--------|
| 2.1.1 | Ask it to run `python3 --version` via `workspace_exec` in an enrolled workspace. | `outcome: "passed"`, `commands[0].returncode == 0`. | |
| 2.1.2 | Ask it to run a command with a runner **not** in `adhoc_runners`. | Refused with `ADHOC_RUNNER_NOT_ALLOWED` and a clear remediation (propose it via `repo_policy`). | |

### 2.2 `script` + `shell` (#377)

| # | Step | Expected | Result |
|---|------|----------|--------|
| 2.2.1 | Before enabling `adhoc_shell_runners`, ask it to run a script (`script="echo hi"`, `shell="sh"`). | Refused — `adhoc_shell_runners` is empty by default, distinct from `adhoc_runners`. | |
| 2.2.2 | Ask it to propose `adhoc_shell_runners: ["sh"]` via `repo_policy` (preview, then apply). | Preview reports `pending_approval` (capability expansion); after your own approval/reload, the repository's effective config shows the new allowlist. | |
| 2.2.3 | Ask it to run a script using pipes/redirects: e.g. `script="echo one two three | tr ' ' '\n' | grep two"`, `shell="sh"`. | Passes — this is the actual reason the script form exists (argv cannot carry shell syntax). | |
| 2.2.4 | Ask it to run `script="git status"` with `shell="sh"` (git enrolled in `adhoc_runners`, not just the script form). | `adhoc_evidence.content_inspected == false`, `command_class == null` — confirm ChatGPT surfaces this distinction rather than treating it as fully inspected. | |
| 2.2.5 | Ask for a script with a large output (e.g. print several hundred KB). | Response is truncated inline, with `output_artifact_reference`/`status` present in `commands[0]`; ask it to retrieve the artifact via `runtime_logs_read`. | |

### 2.3 `argv_sequence` (#443)

| # | Step | Expected | Result |
|---|------|----------|--------|
| 2.3.1 | Ask it to run a 2-element sequence where both succeed (e.g. `python3 --version` then `python3 -c "print(1)"`). | `outcome: "passed"`, `commands` has 2 entries, each with its own `returncode`/`duration_ms`/output. | |
| 2.3.2 | Ask it to run a sequence where element 1 fails (non-zero exit) and element 2 would otherwise succeed. | Fail-fast: `outcome: "failed"`, only 1 entry in `commands` — element 2 never ran. | |
| 2.3.3 | Ask it to run a sequence where element 2 uses a disallowed runner. | Refused **before element 1 runs at all** — verify no side effect from element 1 occurred (e.g. no file created). | |
| 2.3.4 | Ask it to pass `stdin_text` together with `argv_sequence`. | Refused at the contract with a clear message — never silently ignored. | |
| 2.3.5 | Check `execution_evidence` is present in the sequence's response (not an empty object). | Present, showing effective network/filesystem policy (expect `host_account_access`/`host_inherited`-style native-backend evidence). | |
| 2.3.6 | Ask it to run a long sequence (several elements) on a repository with a low `adhoc_timeout_seconds`. | The sequence enforces one *total* budget across all elements, not the full per-command timeout for each — if you can force this, expect a `SEQUENCE_BUDGET_EXHAUSTED`-style refusal before the last element starts, not a multi-hour hang. | |

### 2.4 Cross-form checks

| # | Step | Expected | Result |
|---|------|----------|--------|
| 2.4.1 | Ask it to declare `mutability="workspace"` without `expected_head_sha`/`expected_fingerprint`. | Refused at the contract — exact-state lock is mandatory for mutating runs. | |
| 2.4.2 | Run a mutating command with the correct lock values (read them from `workspace_status` first), then read `workspace_status` again. | `workspace_fingerprint`/`head_sha` reflect the change; a prior verification receipt (if any) is invalidated. | |
| 2.4.3 | Try any two forms together (e.g. `argv` + `script`). | Refused: "Exactly one of argv, argv_sequence, or script must be provided." | |

## 3. Durable operation behavior

`workspace_exec` always admits through the durable operation queue, even for a
synchronous/foreground call — `background` only controls whether the caller waits.

| # | Step | Expected | Result |
|---|------|----------|--------|
| 3.1 | Run a command that takes longer than a typical connector call timeout (tens of seconds). | The call either completes once the command finishes, or returns a pending `operation` with an `operation_id` and a clear `next_action` telling you to `operation wait`. | |
| 3.2 | Follow up with `operation wait` (`until="terminal"`, `timeout_seconds=60`) on that `operation_id`. | Returns the terminal result once the command actually finishes; re-issuing the same wait while it's still running does not start a second execution. | |
| 3.3 | Start a `background=true` run, then start a **new** ChatGPT Web conversation and ask it to find that operation (`operation list`, `scope="workspace:<id>"`). | The new session can find and read the same operation and its terminal result. | |
| 3.4 | Start a long-running command, then ask ChatGPT to cancel it (`operation cancel` or equivalent). | Operation reaches a terminal `cancelled` state; no zombie process left running (if you can check the host). | |
| 3.5 | If you can restart the RepoForge runtime while a background operation is in flight, do so, then ask about that operation afterward. | Operation reconciles to a definite terminal state (or a clearly-labeled ambiguous one) — never silently lost. | |
| 3.6 | Deliberately re-issue the exact same `workspace_exec` call twice (same `expected_head_sha`/`expected_fingerprint`) after the first one already completed. | `workspace_exec` has no `idempotency_key`, and duplicate-request joining only applies to an operation that is still in-flight — a terminal one is never replayed. So the second call always starts a genuinely new execution: for a mutating command, expect `STALE_STATE` once the tree has actually moved on from the recorded lock; for a read-only command (or one that didn't change state), expect it to simply run again, not return a cached result. | |
| 3.6b | While the first call is still running, issue the exact same call again concurrently. | The second call joins the same in-flight operation instead of starting a duplicate execution. | |

## 4. Secure MCP Tunnel behavior

These are mostly infrastructure/platform observations, not code-testable locally.

| # | Step | Expected | Result |
|---|------|----------|--------|
| 4.1 | Put your laptop to sleep with a ChatGPT Web tab open mid-conversation, wake it, make a tool call. | Tunnel reconnects (perhaps after one retry) rather than requiring a full new conversation. | |
| 4.2 | Change networks (e.g. switch Wi-Fi) mid-session, make a tool call. | Same — reconnect, not silent failure. | |
| 4.3 | Restart the local RepoForge MCP server process directly, then make a tool call from an already-open session. | Client either reconnects transparently or surfaces a clear, actionable error — never a hang or a silently wrong response. | |
| 4.4 | If you hit a `REPOSITORY_SELECTION_STALE`-style error (a previously pinned repo selection expiring) or an `EFFECT_OUTCOME_UNKNOWN`/idempotency-uncertain error during any of the above, record the exact wording ChatGPT surfaced to you and whether it recovered on its own. | Client either recovers automatically or you have a concrete transcript of the failure to file as a follow-up. | |

## 5. Deprecation decision record

Fill this in **after** completing sections 1–4, not before.

- Date tested: **______**
- RepoForge build/commit tested: **______**
- ChatGPT Web surface confirmed at: 28 tools / 29 tools / other (describe): **______**

**Case A — 29 tools discovered and used reliably (sections 1–4 mostly ✅):**
Keep `workspace_exec` as the primary answer to "run a command." Keep
`workspace_verify(mode="adhoc")` working during its deprecation window per
`docs/architecture/autonomy-policy-model.md` §11. Set a concrete date/criteria for
removing `mode="adhoc"` from the public contract (not from `workspace_verify` itself —
other modes stay).

**Case B — tool/schema refresh is unreliable (multiple ❌ in section 1):**
Do not add a silent workaround. Pick one, explicitly:
- Retire a different tool to hold the ceiling at 29 (or lower) instead of growing it.
- Delay `workspace_exec`'s *public* exposure (keep it code-complete, gate it behind a
  flag) until refresh is proven.
- Keep routing the run-a-command intent through `workspace_verify(mode="adhoc")` until
  the platform supports reliable additive schema evolution.
- Add explicit capability negotiation/version handshake so the client states what it
  can see, instead of assuming.

## 6. What this runbook does *not* cover

- It does not test PTY/interactive execution — that's explicitly deferred, no
  scaffolding exists yet (see the #377 commit message).
- It does not test the trusted-host lease / effect-authority model — that's #382/#383/
  #385/#407, not yet implemented.
- It does not exercise adversarial/security scenarios (credential exfiltration via
  script, protected-ref bypass attempts) — that belongs to #385/#406/#407's own hardening
  work, not platform-compatibility testing.
