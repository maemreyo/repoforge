# Autonomy Policy Model

- Version: v4
- Status: **DRAFT — architecture review round 4.** Policy decisions are settled where marked "settled"
  in §13. Platform tool exposure, publication-override wiring, containment, credential handling,
  compatibility migration, backend enforcement proof, and the outside-checkout host-effect matrix remain
  gated by the implementing issues cited in §13, including #375, #381, #384, #395, #400, #405, #406, and
  #407 — this document gives those issues stable terms; it does not claim their implementation evidence.
- Key: `autonomy-policy-model`
- Implements: [#370](https://github.com/maemreyo/repoforge/issues/370)
- Read by: every issue under EPIC [#369](https://github.com/maemreyo/repoforge/issues/369) that defines
  a new execution mode, lease, or bypass surface. Cite terms from here rather than inventing new ones.

## Changelog

- **v4** — Review round 4 found: environment-variable wording overstated what's guaranteed present
  (§2, §4 — `SubprocessCommandExecutor.environment()` forwards allowlist vars only when present in
  `os.environ`, and always synthesizes `PATH` regardless); the mode matrix conflated **execution
  backend** (containment) with **authorization posture** (§4 now models them as two axes); the
  tool/path-policy row wrongly claimed "Same" for `trusted_host` when #405 requires effects outside
  the checkout to be independently classified (§4); the direct-remote-shell row wrongly showed `N/A`
  for `relaxed`/`sandboxed_turbo` when a normal (non-force) `git push` already reaches the remote under
  `relaxed` today (§4); `--force-with-lease` was asserted as settled target policy rather than labeled
  current-implementation-only (§6); lease execution authority and publication-override authority were
  conflated (§6, §7); `actor_or_session_binding` was named too concretely given #383's connector-retry
  survival requirement (§7); several genuinely open sub-decisions were implicit rather than listed
  (§13); and six section cross-references were wrong after §3 was inserted in v3 (fixed throughout —
  verified by grepping every `§N` against actual header numbers).
- **v3** — separated tool/path policy from process containment in the mode matrix (grounded in
  `NativeReviewedAdapter`), added the threat-model/attacker-assumptions content and the
  workspace-ownership table #370's scope/AC require, split publication into three outcomes, corrected
  the force-push non-bypassable claim, expanded the lease record, restored audit-event enumeration,
  marked `workspace_exec` tool-count mechanics as open rather than settled, narrowed the confirmation-
  ceremony claim to RepoForge-side only, fixed a timeline error.
- **v2** — reversed the `workspace_exec` recommendation, rewrote iteration/publication into four tiers,
  split lease authorization into static config vs. ephemeral instance with concrete TTLs.
- **v1** — initial draft.

## 1. Scope and non-goals

This document defines the policy model for governed, relaxed, sandboxed-turbo, trusted-host, and
operator-authorized host-bypass execution: what each mode is allowed to skip, what it can never skip,
who can authorize it, and how that authorization is bounded in time and scope.

It does **not** design the wire schema for any tool (#376, #382, #399), the container/sandbox runtime
(#384), the full workspace-lifecycle domain model (#371 owns that; §3 locks the policy principles it
must satisfy), the full host-effect matrix (#405 owns that; §4 only requires it not be flattened into
"same as workspace"), or the lease storage format in full detail (#383). Those issues implement
decisions made here; §13 lists what remains open.

## 2. Threat model: assets, actors, trust boundaries, attacker assumptions

**Assets at risk:**
- Repository content (source, history, GitHub issues/PRs).
- Host credentials reachable from the execution environment: SSH agent socket, `git` credential
  helper, `gh` host auth, and any variable in `DEFAULT_ALLOWED_ENVIRONMENT`
  (`config.py:80-89`: `HOME`, `PATH`, `LANG`, `LC_ALL`, `SSH_AUTH_SOCK`, `GH_HOST`, `GIT_SSH_COMMAND`,
  `COREPACK_HOME`, `PNPM_HOME`). **Precisely stated:** `SubprocessCommandExecutor.environment()`
  (`adapters/subprocess/command_executor.py:70-79`) forwards each allowlisted variable only if it is
  actually set in `os.environ`; `PATH` is the one exception — it is always synthesized from
  `config.path_prefixes` plus the inherited `PATH`, unconditionally. Do not assume every variable is
  present in every process; assume it *can* be, and that credential-bearing ones (`SSH_AUTH_SOCK`,
  `GH_HOST`, `GIT_SSH_COMMAND`, `HOME`-derived `~/.ssh`/`~/.gitconfig`) reach the process whenever the
  operator's own shell has them set.
- Host filesystem, process table, and network egress beyond the repository checkout.
- RepoForge control-plane state: config store, operation ledger, audit sink.
- GitHub remote state: branches, protected refs, releases.

**Actors:**
- The **operator** — the human who owns the checkout and the host, and the only actor who can grant a
  bypass.
- The **model** (ChatGPT Web or any MCP client) — capable but non-authoritative. It can request
  execution; it cannot self-grant scope it wasn't given.
- **RepoForge** — the enforcement boundary between the two.
- **Host OS** — where trusted-host execution actually runs; not itself a trust boundary.
- **Third-party remote content** — forks, PRs, issue bodies, webhook payloads, dependency package
  contents. Adversarial by default.

**Trust boundary today:** the MCP tool contract is the only boundary between the model and the host.
Everything on the host side of a tool call runs on the sole execution backend that ships today, which
provides no process isolation (§4).

**Attacker assumptions / threat classes:**

1. **Prompt injection** carried in repository content, issue/PR bodies, or dependency package content.
2. **Opaque execution surfaces** — scripts, package-manager lifecycle hooks, and CLI wrappers the
   git-argv content guard cannot see through (§8).
3. **Confused-deputy escalation** — a command declared as one effect class performing a larger one.
4. **Credential enumeration or exfiltration** via the ambient environment described above, reachable
   from any process on the native-uncontained backend regardless of authorization posture (§4).
5. **Path escape, symlink substitution, or checkout relocation.**
6. **Child-process or daemon persistence** past lease expiry or revocation — see §7's TTL/revocation
   model, which requires revocation to reach already-running long-lived work, not only new launches.
7. **Concurrent state changes** from the operator's editor, index, or HEAD — #374/#388's domain; this
   document only requires no mode assumes exclusive access by default.
8. **Remote destructive effects and protected-ref rewriting**, directly or via an unparsed wrapper.
9. **Resource exhaustion** — CPU, memory, disk, process-tree, or output volume.
10. **Control-plane state mutation or audit suppression.**

## 3. Workspace ownership policy

| Workspace kind | Lifecycle ownership | Cleanup | Shared state |
|---|---|---|---|
| `managed_worktree` | RepoForge created it and owns its lifecycle | May be removed per policy (idle/retention) | Governed/exact-state by default |
| `adopted_worktree` | RepoForge owns the worktree it manages, but not the branch inside it | Never deletes a branch outside what it created | Mode-aware: still exact-state bound, but the branch's history isn't RepoForge's to rewrite |
| `attached_shared` | The operator | Never deletes, resets, or stashes the checkout or branch on its own initiative | Dirty tree, external index changes, and concurrent editor edits are expected, observed state (#388) |

Ownership of the worktree/checkout is not ownership of the branch inside it — the EPIC principle
"authorization is not ownership" made concrete at the workspace level. #371 implements the full domain
model against this table.

## 4. Execution backend vs. authorization posture (two axes, not one)

**v3 conflated these.** It said every mode running on the native adapter — including `strict` — sits
on the "trusted_host end" of the containment axis. That is imprecise: `strict` and `relaxed` are
**governed authorization postures** that happen to run on an **uncontained execution backend** today,
because that's the only backend that exists. Calling them "trusted_host" collapses a distinction that
audit, authorization, and this policy matrix all depend on. Two axes instead:

**Execution backend** (what actually contains the process):
- `native_uncontained` — the only backend shipping today. `NativeReviewedAdapter`
  (`adapters/execution/native.py:67, 87-88`): `network = NetworkAccess.HOST_INHERITED`,
  `filesystem = FilesystemAccess.HOST_ACCOUNT_ACCESS`, `degraded = True`,
  `degradation_reasons = ("network_not_isolated", "filesystem_not_isolated")`
  (`native.py:44`/`execution_environment.py:471-472`).
- `sandboxed` — requires the `EnforcementRequirement.ENFORCEMENT_REQUIRED` seam
  (`native.py:76`) to have a real `DEV_CONTAINER`/`HERMETIC_CONTAINER` implementation. Does not exist
  yet.

**Authorization posture** (what governs whether/how broadly commands run, and how much ceremony
applies):
- `governed_strict` — typed diagnostics/profiles only. Runs on `native_uncontained` today.
- `governed_relaxed` — allowlisted argv via `workspace_exec`. Runs on `native_uncontained` today.
- `trusted_host` lease — operator-issued, broader scope, less ceremony. Runs on `native_uncontained`
  (it is specifically the posture for when there is no containment to rely on instead).
- `sandboxed_turbo` — broad-shell authorization *paired with* the `sandboxed` backend specifically. It
  is a product mode defined as that pairing, not a posture that can run on `native_uncontained` — if the
  backend isn't actually sandboxed, this posture cannot be claimed (§13).

**Corrected framing of "bypass ceremony, not containment":** a `trusted_host` lease removes
RepoForge-side allowlist and admission ceremony within its granted scope. It does not suppress
platform-owned ChatGPT confirmations (EPIC out-of-scope: "disabling platform-level ChatGPT
confirmations that RepoForge does not control"). It also does not remove the non-bypassable list (§6)
or the circuit breakers underneath generic shell execution (#385, #406, #407) — the absence of sandbox
containment is not the absence of every other guarantee.

### Mode matrix (backend and posture kept distinct; tool/path policy and remote-effect rows corrected)

| Dimension | `governed_strict` | `governed_relaxed` | `sandboxed_turbo` | `trusted_host` lease |
|---|---|---|---|---|
| Execution backend | `native_uncontained` | `native_uncontained` | `sandboxed` (required) | `native_uncontained` |
| Shell execution (tool policy) | None (typed diagnostics/profiles only) | Allowlisted argv via `workspace_exec` | Broad, inside container | Broad, on host |
| Filesystem — tool/path policy | Tool calls validated to workspace paths | Same | Same | Workspace by default; effects **outside the checkout** (HOME/dotfiles, system paths, other repos) require lease-granted scope and are classified by #405 as allowed, denied, lease-gated, or observed-only — never flattened to "same as the other columns" |
| Filesystem — process containment | None (`HOST_ACCOUNT_ACCESS`) | None (`HOST_ACCOUNT_ACCESS`) | Container-isolated, no host escape | None — there is no OS containment. Lease/policy checks gate admission and #385/#405 hooks enforce where technically possible; effects that cannot be safely constrained are denied or explicitly observed-only, never represented as contained |
| Network — process containment | None (`HOST_INHERITED`) | None (`HOST_INHERITED`) | Container network policy — default-deny vs. profile-based is open, §13 | Host network, operator-declared scope, not enforced by containment |
| Credential exposure | `DEFAULT_ALLOWED_ENVIRONMENT`, when set | Same | Whether ambient credentials are denied outright or brokered per profile is open, §13 | Operator-declared, brokered per #381 |
| Git-argv content guard | N/A | Applied to `git` argv only | Applied to `git` argv only | Applied to `git` argv only — never disabled, never a general circuit breaker (§8) |
| Protected-ref write | Blocked | Blocked | Blocked | Blocked by default; only a separate higher-order override authority can lift it (§6) |
| Local commit verification | Repository-publication-policy dependent (§5) | Same | Same | Same |
| Direct remote-shell effect (e.g. `git push`, no force flags) | N/A (no shell) | **Reachable today when `git` is enrolled in `adhoc_runners` and usable credentials are present**: `_assert_git_command_allowed` (`domain/adhoc.py:151-183`) blocks force/mirror/delete/history-rewrite forms, but not an ordinary push; `HOST_INHERITED` supplies network reachability | Reachable only when the sandbox network and credential profiles grant the required remote effect — not exclusive to `trusted_host` | Reachable only when the lease grants the remote effect and an allowed credential profile is available; execution authority still does not create a governed publication receipt |
| Governed push/PR/merge (§5, outcome 1) | Full | Full | Full | Full — a lease widens *execution* reach, it does not by itself grant *publication-override* authority (§6, §7) |

## 5. Publication: three outcomes; local commit decoupled from posture

**Local commit is a repository-publication-policy question, not a posture property:**

```
Current behavior:  all built-in strict/standard/relaxed presets require verification before commit.
Target behavior:   local-commit verification is repository-policy-dependent, independent of
                    execution backend or authorization posture.
```

**Publication has three distinguishable outcomes** (#395 requires "operator-approved skip/override
semantics with durable evidence where policy permits" — a distinct middle case, not just
verified-vs-waiver):

1. **Verified governed publication** — typed publication path, exact-state bound, configured final
   verification ran and passed.
2. **Governed publication override** — still through the typed publication path, exact-state bound, but
   with a durable operator-approved waiver evidencing that verification was skipped or overridden by
   policy. Categorically different from outcome 3: it stays inside the typed path.
3. **Direct remote-shell effect** — `workspace_exec` (under any posture that can reach the remote, per
   §4's corrected row — not only `trusted_host`) performs a push or remote call outside the typed
   publication path entirely. No governed publication receipt; only observed command evidence plus
   whatever waiver evidence justified going around the typed path itself.

Iteration requires no publication verification under any posture; `workspace_exec` evidence is never
conflated with any of the three outcomes above.

## 6. Non-bypassable controls (execution vs. publication-override authority separated)

No posture or lease lifts any of the following as a **default**:

1. Protected-ref write (`validate_adopted_branch`, `domain/policy.py:44`) — with one narrow exception:
   #375 requires a separate **higher-order operator override authority**, distinct in kind from an
   ordinary `trusted_host` lease. An ordinary lease cannot silently acquire this.
2. **Force-push — current implementation vs. target policy, kept separate:**
   - *Current implementation:* bare `--force`, `--mirror`, and branch deletion are blocked by
     `_assert_git_command_allowed`; exact `--force-with-lease=<ref>:<sha>` is permitted by that same
     guard today (`domain/adhoc.py:175-191`).
   - *Target autonomy policy — not yet decided:* whether that exact-form allowance should remain a
     normal guarded push, become an operator-only history-rewrite authority, or be blocked from generic
     shell entirely and reserved for a typed tool. #375 ("remote force/delete blocked by default in
     every autonomy mode") and #385/#407 (non-force guarantee on typed publication) bear on this but
     don't resolve it by themselves. This is listed as open in §13, not asserted as settled.
   - Generic shell or an opaque wrapper around git: the argv guard cannot evaluate it at all (§8); the
     hard safeguard for that case is the enforcement adapter (#385/#406/#407), not argv parsing.
   - Silent or unscoped force push is prohibited in every posture without exception.
3. Secret or credential export as a tool response payload.
4. Direct mutation of RepoForge control-plane state outside its own typed write path.
5. Repository deletion.
6. **Governed publication override authority (§5, outcome 2) is not granted by an execution lease.** A
   `trusted_host` lease authorizes *where and how broadly commands run* (§4); it does not by itself
   authorize *skipping verification inside the typed publication path*. Those are two different
   authorities: a lease can enable outcome 3 (by granting the execution reach to push directly), but
   reaching outcome 2 requires the separate durable operator-waiver mechanism #395 owns. A lease does
   not implicitly upgrade into publication-override authority.

## 7. Lease authorization: static configuration, ephemeral instance, principal binding

**Static capability configuration** — decided per repository, changes rarely, is a capability expansion:
whether `trusted_host` is permitted at all, maximum TTL, allowed host effects, credential profiles,
additional protected resources. Flow: `rf config edit`/`set` → `rf repo refresh <id> --accept` →
`rf config approve` → `rf runtime reload`.

**Ephemeral lease instance** — a runtime authorization issued within the static bounds; does not create
a new config generation per grant. Flow: `rf trust grant` / `rf trust list` / `rf trust revoke`.

**Lease record** (#383's scope: "scoped by repo, checkout, branch/ref, effects, environment,
credentials, and TTL"):

```
lease_id
repository_identity
checkout_identity
workspace_kind            (managed_worktree | adopted_worktree | attached_shared, §3)
branch_or_ref
allowed_effects
host_effect_scope         (filesystem paths, process, network, package managers — #405's classification)
execution_environment_id
credential_profile_ids
granted_by
principal_binding
config_generation
policy_digest
issued_at
expires_at
revoked_at
```

**`principal_binding`, corrected from v3's `actor_or_session_binding`:** #383's actual requirements are
that a lease "cannot be replayed against another checkout, repository, environment, or broader effect"
and that "authorization evidence survives connector retries without exposing bearer secrets." Binding
literally to a chat/session identifier risks failing a legitimate connector retry, which the AC
explicitly requires to survive. The binding should instead be to a **stable consumer principal or
capability context** the platform provides — durable enough to survive a retry, specific enough that
the lease cannot be replayed against a different principal or a broader scope than granted. The exact
schema is #383's to design; this document only fixes the requirement it must satisfy.

**TTL and renewal (settled):**

```
Default TTL:            30 minutes
Maximum single grant:    4 hours
Sliding renewal:         none
Model-initiated renewal: never permitted
Operator renewal:        explicit action, creates a new lease instance (not a silent extension)
Expiry behavior:         blocks new process launch under this lease
Revocation behavior:     blocks new process launch immediately; additionally terminates the
                        process tree of already-running work when repository policy requires it
                        (#406: enforced "before launch and during long-running work where required")
```

**Audit semantics** — every lease-bearing and bypass-authorized action is audited via the existing
`AuditSink`/`runtime_logs_read` path, recording at minimum: grant/renewal/revocation/expiry events with
lease/config-generation identity; a command digest (not raw command text, which may carry secrets);
declared vs. observed effect; execution-environment and credential-profile identity used;
protected-resource denials and effect mismatches; operation/job/process-tree identity (so a long-running
process traces back to its authorizing lease); which publication outcome (§5) an action produced;
redaction applied and durability tier.

## 8. Non-bypassable git-argv scope (unchanged)

`classify_adhoc_command` inspects only `argv[0] == "git"` (`domain/adhoc.py:224`); other runners'
content is not inspected per the function's own docstring. It does not see through `bash script.sh`, a
Python wrapper, nested shells, package-manager lifecycle hooks, opaque CLI wrappers, or a child process
that itself calls `git`/the GitHub API. The actual hard safeguards for the general case are the
enforcement-adapter boundaries #385/#406/#407 exist to prove.

## 9. `workspace_exec` — semantic decision settled, tool-count mechanics open

**Settled:** `workspace_exec` ships as a first-class public tool, superseding
`workspace_verify(mode="adhoc")` for the run-a-command intent — EPIC #369/#370/#376 (2026-08-01) is a
deliberate, informed supersession of the 2026-07-22 decision (commits `ed9568d`, `5b2fad8`), ten days
earlier, not six weeks.

**Genuinely open, platform-gated:** removing `mode="adhoc"` does not reduce tool count, since
`workspace_verify` keeps existing for its other modes. Adding `workspace_exec` moves the static surface
from 28 to 29 unless #404 proves dynamic tool exposure works, another tool is retired, or the operator
raises the ceiling. Not this document's to settle in advance (§13).

## 10. Config threading requirement

Every new mode, static-capability field, or policy value must be traced through all four layers:

1. `config.py` — loader validation.
2. `application/config_admin/service.py` — canonical validation and its field tuple.
3. `application/configuration/document.py` — `render_resolved`.
4. `domain/config_generation.py` — `_REPO_RECOGNIZED` and delta-kind classification.

Applies to the static-capability side of §7, not to individual ephemeral lease instances.

## 11. Compatibility and migration

Existing repository configuration (`policy`, `execution_mode`, `adhoc_runners`) must remain backward-
compatible and strict by default during rollout, but this document does **not** declare that no
configuration migration is needed: #400 explicitly owns migrating strict/relaxed repositories without
silently widening permissions. The MCP surface separately needs a deprecation window for
`workspace_verify(mode="adhoc")`; a tool-count replacement record settling §9's open question;
`SERVER_INSTRUCTIONS` updated so "run a command" resolves to `workspace_exec` unambiguously; and client
capability negotiation validated through #404 before the old surface is removed.

## 12. Domain terms

- **Execution backend** (`native_uncontained` / `sandboxed`) vs **authorization posture**
  (`governed_strict` / `governed_relaxed` / `trusted_host` / `sandboxed_turbo`) — two axes, §4.
  `sandboxed_turbo` is specifically the pairing of broad-shell posture with the `sandboxed` backend.
- **Verified governed publication / governed publication override / direct remote-shell effect** — the
  three publication outcomes, §5. A lease grants execution reach, not publication-override authority
  (§6).
- **Tool/path policy** vs **process containment** — §4.
- **`workspace_exec`** — first-class command-execution tool (§9).
- **Workspace kinds** — `managed_worktree` / `adopted_worktree` / `attached_shared` (§3).
- **Static capability configuration** vs **ephemeral lease instance**, bound by `principal_binding`
  (§7).
- **Bypass ceremony, not containment** — §4.
- **Provenance class** — [#445](https://github.com/maemreyo/repoforge/issues/445).
- **Exact-state binding** — head SHA + workspace fingerprint or [#440](https://github.com/maemreyo/repoforge/issues/440)'s token.
- **One documented path per intent** — owned by `SERVER_INSTRUCTIONS`/#399; terminology owned here.

## 13. Decisions and open questions

**Settled:**
1. `workspace_exec` ships as a first-class tool, superseding `workspace_verify(mode="adhoc")` (§9).
2. `sandboxed_turbo` requires a real `sandboxed` backend; it cannot be claimed on `native_uncontained`
   under any name (§4).
3. `trusted_host` lease TTL: 30 min default, 4h max, no sliding/model renewal, operator renewal creates
   a new instance, revocation reaches already-running long-lived work when policy requires it (§7).
4. Local-commit verification is a repository-publication-policy axis, independent of backend or
   posture (§5).
5. Publication has three distinguishable outcomes, not two (§5).
6. Workspace-checkout ownership is distinct from branch ownership, across all three workspace kinds
   (§3).
7. Execution backend and authorization posture are separate axes; `strict`/`relaxed` are governed
   postures on an uncontained backend, not instances of `trusted_host` (§4).
8. A `trusted_host` lease grants execution reach, not publication-override authority; the two are
   separate authorities (§6).

**Genuinely open:**
1. **Tool-count mechanics for `workspace_exec`** (§9) — gated on #404.
2. **Publication-override wiring** (§5, outcome 2; §6 point 6) — #395's mechanism to design.
3. **`--force-with-lease` target policy** (§6) — whether the exact-form allowance stays a normal guarded
   push, becomes an operator-only history-rewrite authority, or is blocked from generic shell entirely.
   Gated on #375/#385/#407.
4. **Sandboxed network default posture** — default-deny vs. profile-based (§4 mode matrix). Gated on
   #384/#405.
5. **Ambient credentials under `sandboxed_turbo`** — denied outright vs. brokered per profile (§4).
   Gated on #381/#384.
6. **Outside-checkout host-effect schema representation** — how HOME/dotfiles/system-path/daemon
   effects are declared and checked against a lease's `host_effect_scope` (§4, §7). #405's to own.
7. **Backend enforcement proof** — that §6's non-bypassable list and §2's threat classes hold under
   adversarial testing is #385/#406/#407's job, not something a policy document settles by assertion.

Issues implementing §3, §4, §7, §8 (#371, #383, #384, #385, #405) should cite this document rather than
re-deriving mode semantics; the open questions above are theirs, or #375/#395/#399/#404's, to close.
