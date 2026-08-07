# Enroll a repository

This guide is written for AI agents and operators who need to add one local Git repository to
RepoForge. It is the deterministic CLI path: discover, propose, decide, approve, activate,
verify. Use `rf onboard` instead when you are discovering several repositories or want the
interactive review UI (see [INTERACTIVE_ONBOARDING.md](INTERACTIVE_ONBOARDING.md)).

Every command below is safe to run as-is except the final activation step, which changes the
active configuration generation and requires an explicit approval token.

## Prerequisites

- The repository already exists on this machine as a normal (non-bare) Git clone.
- `rf` is installed (`which rf`).
- GitHub CLI is authenticated for the account that can read the repository's remote:
  `gh auth status`. Git fetch/push uses the repository's own remote and SSH configuration.
- The repository is *not* already enrolled: `rf repo list` does not contain its `repo_id`.

## 1. Discover

```sh
rf repo discover /absolute/path/to/repository
```

RepoForge derives a natural `repo_id` from the directory name and reports eligibility,
duplicates, and exclusions. If the path is a nested worktree, the primary clone is resolved
automatically.

The `repo_id` is the short allowlisted identifier the MCP tools will accept. You can override
it later with `--repo-id` if you need a different name.

## 2. Propose

```sh
rf repo propose /absolute/path/to/repository \
  --repo-id <id> \
  --template standard \
  --non-interactive
```

The proposal returns JSON with:

- `proposal_id` — the exact approval token for the activation step (`approve:<proposal_id>`);
- `findings` — warnings that need review (multiple lockfiles, large repository, existing
  policy, symlinks, binary files, and so on);
- `required_decisions` — the decision prompts RepoForge needs answered before enrollment;
- `policy` — the resolved policy preview: mode, remote, default base, denied paths, profiles,
  publish flag, and change budgets.

Pass every decision you already know with `--decision CODE=CHOICE` (repeatable). The proposal
re-runs deterministically: the same facts and decisions produce the same `proposal_id`, so
re-proposing with the exact same flags is how you iterate toward a complete, high-confidence
proposal. When `required_decisions` is empty and `confidence` is `high`, the proposal is ready
to approve.

## 3. Choose a template

| Template | Meaning | Budgets (files / diff lines / bytes) |
| --- | --- | --- |
| `read_only` | Inspection only: no verification profiles, publishing disabled | 150 / 12 000 / 25 MiB |
| `standard` | Default. Writable; publishing to the selected remote when GitHub auth is verified; auto-detected verification profiles | 150 / 12 000 / 25 MiB |
| `strict` | Like `standard` but with tight change budgets | 50 / 4 000 / 10 MiB |

Budget and read-only behavior can be tuned with `--policy-override` (repeatable):

| Override | Example |
| --- | --- |
| `read_only=false` | Force writable enrollment even when an automatic read-only trigger fired |
| `max_changed_files=200` | Raise or lower the per-task file budget |
| `denied_paths_remove=.github/workflows/**` | Lift a default denied path (only the removable set is allowed) |
| `allowed_paths=src/**,tests/**` | Restrict writable paths |

The default denied paths always include `.git`, `.env*`, key/secret/credential files, and
`.github/workflows/**`. Protected branches (`main`, `master`, `develop`, `production`) can
never be edited; writable work happens on `ai/<slug>-<suffix>` branches in isolated worktrees.

## 4. Answer the required decisions

Each required decision is `security_relevant` — the choice becomes part of the immutable
policy. Common ones and safe defaults:

| Decision | Question | Recommended |
| --- | --- | --- |
| `default_base` | Which branch do worktrees branch from? | The repository default branch (usually `main`) |
| `package_manager` | Multiple lockfiles found — which toolchain is authoritative? | The one CI uses (check `.github/workflows`), not necessarily the first detected |
| `dependency_install` | Networked dependency setup — include, exclude, or block? | `exclude` unless a task genuinely needs `npm ci`/`yarn install` |
| `publish_remote` | Only asked with multiple remotes: which may RepoForge push to? | The canonical `origin`, or `read_only` |
| `publishing_access` | Only asked when GitHub auth is unverified: local-only, read-only, or block? | `local_only` until `gh auth status` is healthy |
| `autofix` | `fix` scripts mutate content — include, exclude, or block? | `exclude` |
| `risky_commands` | Deploy/release/db/destructive scripts detected — exclude or block? | `exclude` (they are never auto-inferred as profiles) |
| `monorepo_scope` | Multiple manifests — root-wide or scoped verification? | `root` for a single app, `scoped` for real monorepos (needs `allowed_paths`) |
| `submodules` | Submodules are never writable — block or read-only parent? | `block` |
| `lfs` | Git LFS content — bounded read-only or block? | `read_only` |
| `repository_budget` | Large repo — keep defaults, scoped paths, or read-only? | `keep_defaults` unless scans truncate |
| `existing_policy` | RepoForge policy metadata already exists — preserve, replace, or block? | `replace` after reviewing the proposal |
| `existing_worktrees` | Extra worktrees exist — create a new isolated one, read-only, or block? | `use_new_isolated` |

Iterate until the proposal shows no `required_decisions`:

```sh
rf repo propose /absolute/path/to/repository \
  --repo-id <id> \
  --template standard \
  --decision default_base=main \
  --decision package_manager=npm \
  --decision dependency_install=exclude \
  --non-interactive
```

## 5. Enroll and activate

```sh
rf repo add /absolute/path/to/repository \
  --repo-id <id> \
  --template standard \
  --decision default_base=main \
  --decision package_manager=npm \
  --decision dependency_install=exclude \
  --approve approve:<proposal_id> \
  --activate always \
  --wait
```

The command validates the approval token against the proposal, writes the source config entry,
resolves a new immutable generation, activates it, and returns an `activation_receipt_id`.
`--activate always` is required for a fully non-interactive activation; `--wait` waits for the
runtime to report the activated state.

If the approval token is wrong or stale, re-run the propose step with the exact same flags to
get the current `proposal_id` — never guess the token.

## 6. Verify

```sh
rf repo list                      # the new repo_id appears; generation bumped; runtime healthy
rf show-config                    # resolved policy: read_only, publish_enabled, budgets, profiles
rf runtime status                 # active_generation matches; restart_required false
rf doctor                         # ok: true; 0 errors
rf config history                 # previous generation retained; rollback available
```

When the runtime restarts during activation (the activation may be classified as
`restart_fallback`), the MCP tool surface itself is unchanged — adding a repository never adds
or removes tools. ChatGPT-side connectors only need a reconnect/rediscovery when the client
caches tool results or dropped the connection during the restart.

## Optional: enable managed CodeGraph semantic intelligence

CodeGraph is not enabled by repository discovery, proposal, `rf setup`, or `rf repo add`. A newly
resolved repository explicitly carries `code_intelligence_provider_id = ""`, which preserves the
exact Tree-sitter → syntax baseline composition. Enabling CodeGraph is a separate reviewed
configuration-generation change: enroll one pinned provider manifest, then set the selected
repository's `code_intelligence_provider_id` to that provider ID.

A reviewed enrollment has this shape in the immutable resolved configuration:

```toml
[[providers]]
provider_id = "codegraph"
kind = "analyzer"
version = "1.5.0"
executable = "/opt/repoforge/providers/codegraph"
executable_digest = "<lowercase-sha256>"
supported_languages = ["python", "javascript", "typescript"]
supported_capabilities = ["semantic_graph"]
network_policy = "none"

[providers.filesystem]
capability = "managed_state_write"
allowed_paths = []

[providers.codegraph]
init_timeout_seconds = 60
sync_timeout_seconds = 120
query_timeout_seconds = 30
max_changed_paths = 200
max_relationships = 200
max_affected_paths = 200
max_depth = 4
projection_max_files = 20000
projection_max_bytes = 268435456
canary_timeout_seconds = 120

[repositories.<id>]
code_intelligence_provider_id = "codegraph"
```

RepoForge verifies the executable digest before every semantic command and requires the exact
pinned version. It launches one-shot commands with no inherited provider environment and with
`CODEGRAPH_NO_DAEMON=1`, `CODEGRAPH_NO_DOWNLOAD=1`, update checks and telemetry disabled. It does
not start a shared daemon, watcher or MCP server, does not download a runtime, and does not add an
MCP tool.

Provider-owned files remain outside every Git worktree:

```text
<state_root>/providers/codegraph/workspaces/<workspace_id>/
<state_root>/providers/codegraph/promotion/<promotion-identity>.json
<state_root>/providers/codegraph/canary-corpus/<promotion-identity>/
```

The promotion identity hashes the executable digest and version, host platform and architecture,
provider-manifest hash, CodeGraph options digest, adapter schema version and embedded canary-corpus
digest. A successful receipt is reusable only for that exact identity. Changing any field forces a
new bounded canary run; corrupt, missing or symlinked receipt state is treated as absent.

`rf show-config` reports repository enrollment, provider/version identity, manifest/options/
executable digests, executable availability and promotion-receipt validity. `rf doctor` reports
separate executable, reviewed-version and promotion-receipt checks. Neither command returns the
resolved executable path, environment variables, raw provider output or provider-private errors.

Graph evidence is additive. When it is unavailable, below threshold, partial, truncated, stale,
ambiguous or canary-unpromoted, RepoForge retains the baseline facts and widens verification.
Automatic targeted verification is refused under semantic uncertainty; CodeGraph can never reduce
a safety gate merely because it returned a candidate set.

Rollback does not require deleting files in a repository. Set
`repositories.<id>.code_intelligence_provider_id = ""` (or remove that field), accept and activate
a new reviewed generation, and the exact baseline provider construction returns. Workspace removal
and bounded startup cleanup delete only provider-owned state under `<state_root>`; RepoForge never
inspects or deletes a user-created `.codegraph` directory in the source clone or worktree.

## Troubleshooting and known constraints

- **A proposed profile differs from CI's package manager.** The profile detector prefers
  `pnpm` → `yarn` → `npm` by which lockfile exists. The `package_manager` decision records
  intent but does not rewrite detected profile commands. If the detected profile does not
  match CI, add an explicit `[repo.policy_patch.profiles.<name>]` section in the source config
  (see the `repoforge` entry in `config.example.toml`) and refresh.
- **A custom branch prefix (for example `dn/`) cannot be configured.** The writable branch
  prefix is currently hardcoded to `ai/`; a `branch_prefix` key in the source config is
  ignored. Tracked as maemreyo/repoforge#360.
- **`gh` cannot resolve the repository** (`Could not resolve to a Repository`). The active
  GitHub account lacks access; switch accounts (`gh auth switch --account <name>`) or wait
  for repository-bound multi-account identity (maemreyo/repoforge#284).
- **The source clone is dirty.** Enrollment proceeds; dirty working trees are a `rf doctor`
  warning, not an error. RepoForge works in isolated worktrees and never touches the source
  checkout. Clean the clone separately if desired.
- **No remote or unverified GitHub auth.** Publishing is disabled automatically and
  `publishing_access` is required; enrollment still succeeds for local-only work.
- **Unsupported ecosystem.** If no safe verification profile can be inferred, enrollment is
  read-only unless `read_only=false` is explicitly overridden.
- **`rf repo refresh <id> --accept`** re-resolves an enrolled repository after source-config
  or policy changes; `rf config set` only addresses the scalar fields
  (`max_changed_files`, `max_diff_lines`, `max_total_changed_bytes`, `read_only`). Deeper
  policy changes go through `rf config edit` + refresh. Never hand-edit the resolved
  generations under the state root.
- **Approval tokens are exact.** The token format is `approve:<proposal_id>`. Different
  decisions produce a different `proposal_id`, so pass the same flags to propose and add.
