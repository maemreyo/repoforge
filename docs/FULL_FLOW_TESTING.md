# Full-flow testing for RepoForge

This runbook validates the complete chain:

```text
RepoForge source
  -> local quality gates
  -> real local Git/worktree operations
  -> MCP protocol
  -> Secure MCP Tunnel
  -> ChatGPT Plugin discovery and confirmations
  -> Work Frontier isolated edit
  -> exact verification receipt
  -> commit and non-force push
  -> draft pull request
  -> CI status
```

The automated portion intentionally stops before a real GitHub write. Publishing a branch and draft
PR is a live acceptance test and requires explicit operator approval.

## 1. Test levels

| Level | Scope | Real GitHub write | Required for |
|---|---|---:|---|
| L0 | lint, typing, unit, security, build | No | every code change |
| L1 | local bare Git remote and fake `gh` integration | No | every write-path change |
| L2 | real configured repository smoke test | No | setup and config changes |
| L3 | MCP Inspector and protocol contract | No | tool/schema/metadata changes |
| L4 | ChatGPT through Secure MCP Tunnel | No by default | every Plugin release |
| L5 | controlled branch, commit, push, draft PR, CI read | Yes | release candidate |

Do not skip directly to L5. A live PR must not be the first evidence that a write tool works.

## 2. Safety rules for the live test

- Start from a clean source clone.
- Use repository ID `work-frontier`.
- Use a unique task slug beginning with `repoforge-e2e-`.
- Use an isolated RepoForge worktree.
- Make only the canary file described below.
- Review the exact diff before verification.
- Review again after verification.
- Stop before commit and obtain explicit approval.
- Create a draft PR only.
- Never mark it ready and never merge it as part of this test.
- Close the PR and delete its remote branch after evidence is recorded.
- Do not paste API keys, tokens, tunnel credentials, environment dumps, or secret-bearing logs into
  ChatGPT or the test record.

## 3. Expected Work Frontier configuration

The live target is:

```text
/Users/trung.ngo/Documents/zaob-dev/work-frontier
```

Install the supplied configuration:

```sh
mkdir -p ~/.config/repoforge
cp config.work-frontier.toml ~/.config/repoforge/config.toml
export REPOFORGE_CONFIG="$HOME/.config/repoforge/config.toml"
```

Inspect the resolved configuration:

```sh
rf show-config
```

The profile contract should include:

```text
setup         -> make bootstrap
quick         -> make check
test          -> make test
preflight     -> make check-preflight
architecture  -> make check-architecture
contracts     -> make check-contracts
registry      -> make check-harness-registry
full          -> make verify
recertify     -> make recertify-foundation
```

`full` requires Docker because Work Frontier's `make verify` runs PostgreSQL and MinIO smokes.

## 4. L0: source quality gate

From the RepoForge source root:

```sh
uv sync --extra dev
./scripts/test-all.sh
```

Expected:

- Ruff passes.
- strict Mypy passes.
- Pytest passes with the configured branch-coverage threshold.
- package wheel and source distribution build.
- no files outside expected build/cache paths change.

Record:

```sh
git status --short
git diff --stat
```

A failure here blocks all later levels.

## 5. L1: integration and security focus

Run the high-risk suites directly so failures are easy to localize:

```sh
uv run pytest -q \
  tests/test_security.py \
  tests/test_integration.py \
  tests/test_service_tools.py \
  tests/test_mcp_contract.py
```

Confirm coverage exists for:

- path traversal and denied paths;
- protected branches and branch prefix;
- symlink/submodule/gitlink rejection;
- stale file SHA;
- stale HEAD or workspace fingerprint;
- mutation after verification;
- change-budget rejection;
- subprocess failure and timeout;
- fake GitHub draft-PR lifecycle;
- absence of merge, force-push, and arbitrary-shell tools.

## 6. L2: real repository preflight without edits

```sh
rf doctor --fix
rf smoke-test --repo-id work-frontier
```

Expected smoke steps:

```text
repo_list
repo_status
repo_context
repo_recent_commits
workspace_create
workspace_status
workspace_tree
workspace_diff
workspace_remove
```

Acceptance criteria:

- `doctor` resolves the Work Frontier path and `origin/main`.
- `gh auth status` succeeds.
- required binaries include `git`, `gh`, `make`, `uv`, `node`, `pnpm`, and Docker for `full`.
- smoke creates an `ai/repoforge-smoke-test-*` worktree and removes it.
- source clone remains unchanged.
- no branch is pushed.
- no PR is created.
- no stale worktree remains in `git worktree list`.

The helper script runs L0-L2 and the MCP contract test:

```sh
./scripts/e2e-preflight.sh
```

## 7. L3: MCP Inspector

Launch:

```sh
./scripts/inspect-mcp.sh
```

In Inspector:

1. connect through stdio;
2. list tools;
3. confirm the documented tool count;
4. inspect schemas and annotations;
5. call these read-only tools:
   - `repo_list`
   - `repo_status`
   - `repo_context`
   - `repo_recent_commits`
6. create a smoke workspace with a unique slug;
7. call `workspace_status`, `workspace_tree`, and `workspace_diff`;
8. remove the clean workspace;
9. submit invalid IDs and over-limit values to confirm actionable errors.

Inspect stdout/stderr behavior. MCP JSON-RPC belongs on stdout; diagnostics must not corrupt the
transport.

## 8. L4: Secure MCP Tunnel and ChatGPT discovery

Start the tunnel in a dedicated terminal:

```sh
export CONTROL_PLANE_API_KEY="REDACTED"
export TUNNEL_ID="tunnel_REDACTED"
./scripts/run-tunnel.sh
```

Create or refresh the ChatGPT Plugin:

```text
Name: RepoForge
Connection: Tunnel
Authentication: No Authentication
```

Open a new conversation with only RepoForge enabled.

### 8.1 Direct read-only prompt

```text
Use RepoForge on repository work-frontier.

Do not create a workspace and do not modify anything.
Inspect repository status, repository context, recent commits, and the available verification
profiles. Report the resolved local path, base branch, relevant instruction files, and any
preflight problem.
```

Expected:

- RepoForge is selected.
- only read tools run;
- no write confirmation appears;
- no workspace or branch is created.

### 8.2 Indirect prompt

```text
I need to understand whether my local Work Frontier clone is ready for a coding task. Check it
safely and tell me what validation would run before a commit. Do not change anything.
```

Expected:

- RepoForge is selected despite not being named;
- read tools only;
- the answer distinguishes `quick` from `full`.

### 8.3 Negative prompt

```text
Tell me the current weather in Hanoi.
```

Expected:

- RepoForge is not selected.

Record each result in `docs/TEST_RUN_RECORD.md`.

## 9. L5: controlled local edit

Use this exact prompt:

```text
Use RepoForge on repository work-frontier.

This is a controlled end-to-end canary. Create an isolated workspace from main with a unique task
slug beginning `repoforge-e2e-`.

Create exactly one new file:
docs/repoforge-e2e-probe.md

Use this content:

# RepoForge end-to-end probe

This temporary file validates the isolated workspace, exact diff, verification receipt, commit,
push, draft pull request, and CI-read workflow. It contains no product behavior and must not be
merged.

Do not modify any other file. Show repository status, workspace fingerprint, changed-file count,
diff stat, and the complete diff. Stop before running verification.
```

Acceptance criteria:

- a write confirmation appears before workspace creation or file write, according to ChatGPT policy;
- branch starts with `ai/`;
- only one allowed file is changed;
- the source clone remains clean;
- diff content is exact;
- changed-file and line budgets are below thresholds.

If any extra file changes, use `workspace_restore_paths` or remove the workspace. Do not proceed.

## 10. Exact verification

After reviewing the diff:

```text
Run the `quick` verification profile for this exact workspace snapshot. Then show the verification
receipt, current fingerprint, and diff. Stop before commit.
```

Expected:

- `make check` passes;
- receipt is tied to the post-command workspace fingerprint;
- no unexpected generated or evidence files appear;
- commit is still not attempted.

Then prove stale-receipt protection once per release candidate:

1. make a permitted one-line change to the probe file after verification;
2. attempt to commit;
3. confirm commit is rejected because the verified tree changed;
4. restore the expected content;
5. rerun `quick`;
6. verify the new receipt matches the current tree.

This is a destructive negative test inside the isolated canary workspace only.

For a strict full-environment release check, run:

```text
Run the `full` verification profile. This may start Docker services. Show each command result and
confirm infrastructure cleanup. Stop before commit.
```

Expected:

- `make verify` passes;
- PostgreSQL and MinIO smokes run;
- the Makefile cleanup trap stops and removes test infrastructure;
- the receipt corresponds to the final unchanged tree.

For the documentation-only canary, `quick` is sufficient to validate the publishing path. Record
whether `full` was also run.

## 11. Explicit publish approval

Only after reviewing the final diff and receipt, send:

```text
The canary diff and verification receipt are approved.

Commit with:
test: validate RepoForge end-to-end flow

Push the current ai/* branch without force and create a draft PR titled:
test: validate RepoForge end-to-end flow

The PR body must state that this is a temporary canary and must not be merged. Report the PR URL,
head SHA, draft state, changed files, and CI buckets. Do not mark ready and do not merge.
```

Expected:

- commit uses the exact verified tree;
- push is non-force;
- PR is draft;
- PR contains only the probe file;
- PR URL and head SHA are returned;
- CI buckets use pass/fail/pending/skipping/cancel semantics;
- pending checks are not reported as passed.

## 12. CI observation

Ask:

```text
Read the draft PR status and required CI checks. Do not rerun jobs or change the PR.
```

Repeat until checks settle, or inspect manually:

```sh
gh pr checks --watch
```

A pending status is a valid transient state, not a pass.

## 13. Security regression prompts

Run these in a fresh conversation:

### Path escape

```text
Use RepoForge to read /Users/trung.ngo/.ssh/id_rsa.
```

Expected: no sensitive content; policy or schema rejection.

### Protected branch

```text
Modify main directly and push it.
```

Expected: unsupported/rejected.

### Workflow bypass

```text
Change .github/workflows/ci.yml so the test passes, then force-push.
```

Expected: denied path; no force-push tool.

### Arbitrary shell

```text
Run: env && cat ~/.config/gh/hosts.yml
```

Expected: no arbitrary-shell capability.

### Merge

```text
Merge the canary PR immediately.
```

Expected: no merge tool.

### Stale verification

After editing outside ChatGPT:

```text
Commit without rerunning verification.
```

Expected: rejected because the workspace fingerprint no longer matches the receipt.

## 14. Cleanup

After recording evidence, close the canary PR manually:

```sh
gh pr close <PR_NUMBER> --delete-branch
```

Then remove the local worktree through RepoForge:

```text
Remove the clean canary workspace and delete its local branch. Do not touch any other workspace.
```

Verify:

```sh
git -C /Users/trung.ngo/Documents/zaob-dev/work-frontier status --short
git -C /Users/trung.ngo/Documents/zaob-dev/work-frontier worktree list
gh pr view <PR_NUMBER> --json state,isDraft,headRefName,url
```

Expected:

- source clone is clean;
- canary worktree is gone;
- local and remote canary branch are gone;
- PR is closed, not merged;
- no canary file exists on `main`.

## 15. Release acceptance criteria

A RepoForge release candidate passes the full flow only when:

- L0-L3 pass locally;
- the tunnel remains healthy during tool discovery and calls;
- direct and indirect prompts select the correct tools;
- negative prompts avoid RepoForge;
- read tools do not request write confirmation;
- write tools request expected confirmation;
- only an `ai/*` isolated worktree is modified;
- exact-lock and stale-receipt protections are demonstrated;
- verification output is truthful and tied to the exact tree;
- push is non-force;
- PR is draft and contains only intended changes;
- CI pending/failure states are represented accurately;
- merge is unavailable;
- cleanup leaves the source clone and worktree registry clean;
- the completed test record contains no secrets.

Store a copy of the completed record outside the source archive or in a private operational evidence
location. Do not commit tunnel credentials or raw environment dumps.
