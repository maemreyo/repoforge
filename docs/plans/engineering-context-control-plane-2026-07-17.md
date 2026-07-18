# Engineering Context Control Plane

**Revision 2 — 2026-07-18.** Original decision record from the grilling session of 2026-07-17
(baseline commit `3c77c0e2cadf4ee9c74d8deafab2ae873cea6241`), reconciled with an external
architecture review on 2026-07-18. Review verdict: **CONDITIONAL GO — architecture approved after
document reconciliation; implementation may begin on independent internal tracks, while public
activation remains gated by #194.** This file is the authoritative V1 design; initiative #203
(children #204–#213) tracks execution.

## Problem statement

The primary, measurable pain is **agent rule adherence**: architecture-boundary violations,
non-clean code, oversized files. ChatGPT-class clients do not reliably follow prose instructions;
the system must deliver an engineering "ideology" through mechanisms with teeth, at the moments the
model is actually writing code.

90-day metrics: review-fix cycles per PR; violations-per-review-run trend; token cost of guidance
injection.

Secondary outcomes (on other tracks, consumed here): task resume across chats (TaskCapsule, #18),
context bloat reduction (#150/#193), code intelligence (#189/#38).

## Scope constraint (V1)

**Single-user local operator.** RepoForge V1 runs over a local tunnel for one operator.
`principal` is reserved in the TaskCapsule schema and populated with the local operator;
multi-user/enterprise authentication and tenancy are explicitly out of scope. The schema must not
make later principal binding a one-way migration (per the #180 rollback constraint).

## Locked decisions

1. **Enforcement backbone: checked-at-verify.** Typed rules compile to a review diagnostic in the
   verify plane (#190, wired by #213). Linter semantics, not fail-fast: one run returns ALL
   findings `{rule_id, file, line, fix_hint, state}` so the agent repairs everything in a single
   pass. Rule results are five-state — `PASS | FAIL | UNKNOWN | SKIPPED | ERROR`; a provider being
   unavailable yields `UNKNOWN`, never an implicit pass.
2. **Nothing gates the receipt in V1.** Findings attach to verification evidence and the PR; the
   human review is the final net. `enforcement: hard` is **rejected at config validation with a
   typed `UNSUPPORTED_ENFORCEMENT`** — never accepted and silently ignored. Each rule declares
   **`override_policy: never | task | approval`**: `never` cannot be relaxed by a task; `task`
   accepts a capsule override with scope, reason, actor, expiry; `approval` requires an approval
   receipt (#81). Every conflict appears in compiled diagnostics.
3. **Validators are borrowed, not built.** Built-in structural checks: `file_length`, `diff_size`,
   `import_boundary`, `new_dependency` (`duplicate_helper` deferred to V1.1), tree-sitter-backed
   where the CodeIntelligencePort provides it, degrading to `UNKNOWN` otherwise. Everything deeper
   comes from repo-provided, allowlisted command-profile steps. A rule file can never reference an
   arbitrary command.
4. **Guides are skills — standard `SKILL.md` directory packages.** A skill is its directory:
   `SKILL.md` plus `references/`, `assets/`, `scripts/` indexed as **inert content** with digest
   and provenance. Default roots with deterministic precedence:
   `.agents/skills/` (Codex standard) > `.claude/skills/` (compatibility) > `.agent/skills/`
   (legacy), plus configurable user-level roots (read-only in V1); repo beats user. **Collisions
   produce a diagnostic listing both provenances — never silent shadowing.**
   **Trust boundary:** RepoForge may index and expose script files as inert reference content, but
   skill ingestion never grants execution capability. Execution is available only through
   separately approved repository profiles/diagnostics. Nothing may ever wire `scripts/` into a
   command runner. Skills and AGENTS files are untrusted input: size caps, sanitization, and an
   adversarial corpus (malicious SKILL.md/AGENTS, YAML bombs, symlink escape, injection payloads)
   in the #182 gate corpora.
5. **Skill selection is three-tiered.** `.repoforge/skills.yaml` binding (skill →
   `paths`/`phase`/`delivery`) is primary; lexical auto-match of skill descriptions against task
   intent + focus paths is the fallback (multilingual golden set — Vietnamese prompts × English
   skills — with precision/recall targets in #182); a budgeted catalog with on-demand fetch is the
   net. No embeddings in V1.
6. **Typed rules live in `.repoforge/rules/*.yaml`**, git-versioned, reviewed via PR like code.
   `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` are ingested as advisory prose; a nested `AGENTS.md`
   is advisory scoped to its own subtree with closer-directory-wins; `AGENTS.override.md` is
   supported. No natural-language→typed-rule extraction in V1.
7. **Delivery classes** (per rule/skill, orthogonal to enforcement):
   - `always` — id strip (~300 B) on every mutation-path response; hard cap of 5 records
     repo-wide, config validation rejects the sixth.
   - `on_entry` — full fragment (≤1.5 KB) on first touch of a matching path scope (read-first);
     deduplicated via `guides_delivered` on the TaskCapsule.
   - `refresh` — re-delivered on triggers only: a violation of the rule in this task
     auto-escalates it to `always` for the remainder of the task; decay after 15 mutations;
     phase transitions.
   - `on_demand` — catalog entry only; fetched explicitly.
   Defaults 5 / 15 / auto-escalation are configuration, not constants.
   **Guidance must precede the first mutation on a scope.** Two mechanisms: plan-phase precompile
   from declared `focus_paths`, and a **preflight handshake** for unseen scopes:

   ```text
   mutate(scope, context_pack_hash)
     → GUIDANCE_REQUIRED(pack_hash, fragments)   # mutation not applied
     → retry mutate(scope, pack_hash)            # idempotent, executes
   ```

   If constitution or task revision changed between the two calls, a fresh pack is returned. This
   is a context-freshness handshake, never a human gate; worst case one extra round-trip per new
   scope per task.
8. **Zero-config.** The system runs without `.repoforge/`: built-in checks use conservative
   default thresholds (safe because V1 is non-blocking), skills are auto-matched. `.repoforge/`
   files override or disable defaults. Scaffolding: an optional `rf onboard` step and an
   idempotent `rf rules init`, both writing to the working tree only (the operator commits).
9. **Memory lifecycle is git-native, with an explicit activation model.** A proposal is a draft PR
   touching `.repoforge/**` or repo skills (label `memory-proposal`), produced through the normal
   isolated-worktree flow. **The compiler consumes constitution only from the accepted
   default-branch generation (`constitution_sha`); a workspace's own constitution edits never feed
   the proposing task's pack** — proposals cannot weaken the constitution for the task proposing
   them. Merge makes a proposal **eligible**; activation follows validation and generation
   acceptance; an invalid new constitution keeps last-known-good active with a typed diagnostic.
   CODEOWNERS/required review on constitution paths is recommended (documented, not enforced by
   RepoForge). No `memory_*` tools, no second approval store. Durable state keeps only task
   decisions (inside the TaskCapsule; they die with the task; permanence = promotion to rule/ADR
   via PR) and operator preferences (read-only configuration in V1). Stale detection is a periodic
   diagnostic that proposes cleanup and never auto-deletes.
10. **The context layer adds zero public tools.** The ContextCompiler is an internal engine behind
    `repo_task_context` (#187): new sections `rules | skills | decisions | skill_catalog`, new
    params `task_id`, `focus_paths` — additive under the #181 tolerant-reader contract.
    Just-in-time guidance rides existing read/mutate responses. The only new public surface is the
    task control plane, and it is **effect-homogeneous**: `task_read` / `task_mutate`, honoring
    host tool annotations (`readOnlyHint`, `destructiveHint`) so hosts can route and confirm
    correctly. Mixed-effect `preview|apply` tools in the v2 roster are a recorded annotation
    constraint for the #194 cutover audit. Public activation of everything in this initiative is
    gated by #194; development is not.
11. **Pack binding and budgets.** Every compiled pack carries `code_snapshot_sha` (workspace tree)
    and **`constitution_sha`** (accepted-generation constitution — deliberately separate), plus
    `config_generation`, `task_revision`, `policy_digest`, `compiler_schema_version`, source
    digests, `focus_paths`, and a canonically defined `pack_hash` over all of it. **Model token
    budget and transport byte budget are independent limits** (the current 96 KB cap is bytes).
    Budget drop order: examples → skill catalog → advisory prose → task decisions → typed rules
    last. Dropped sections are declared in `omitted[]`.
12. **No pack framework in V1.** A "policy pack" is a convention: skills + rules + binding lines.
    Vendor by hand with `upstream: <repo>@<sha>` frontmatter for traceability. One mode plus
    per-task capsule overrides (subject to `override_policy`); the manifest/digest/auto-update
    framework waits for the rule of three. Ponytail is the first pack and must not become the
    architecture.
13. **Precedence is the enforceable subset** (compiler-reported, never silently merged):
    server policy → repo typed rules (mediated by `override_policy`) → task decisions/overrides →
    repo skills/advisory → user-level skills → retrieved examples.
    "Current user instruction" exists server-side only as a **TaskInstruction record** on the
    capsule, with dual provenance:

    ```yaml
    asserted_origin: user        # user | agent | issue | system
    recorded_by: model           # model | operator | system
    trust: relayed_unverified    # relayed_unverified | verified
    revision: 7
    ```

    The server never pretends visibility into the live prompt, and audit never upgrades
    model-relayed text into user-authenticated evidence. Conflicts return
    `{winner, suppressed, reason, override_provenance}`.
14. **Paved-path principle.** Every policy refusal must carry a typed `next_action` pointing at the
    sanctioned alternative. Field evidence (2026-07-17): blocked from the canonical golden-schema
    generator (no allowlisted profile step existed), an agent exfiltrated exact bytes through
    truncated diagnostic logs via lossless compression + base85 and hand-assembled the golden — the
    control plane held, but the missing paved road produced an unauditable workaround. Blocked
    agents do not stop; they get creative.

## Shapes (illustrative)

```yaml
# .repoforge/rules/architecture.yaml
- id: application.no-adapter-imports
  enforcement: checked          # checked | advisory ("hard" → UNSUPPORTED_ENFORCEMENT in V1)
  override_policy: never        # never | task | approval
  validator: import_boundary    # built-in id or approved profile step — never a raw command
  paths: ["src/repoforge/application/**/*.py"]
  forbid: ["repoforge.adapters", "subprocess", "fcntl"]
  delivery: always
```

```yaml
# .repoforge/skills.yaml
- skill: domain-conventions
  paths: ["src/**/domain/**"]
  delivery: on_entry
- skill: ponytail-planning
  phase: plan
  delivery: on_entry
```

## Ticket map and build order

| Ticket | Slice | Starts |
| --- | --- | --- |
| #211 | GitHub capability probes + per-capability graph evidence (contract side on #187) | now |
| #208 | TaskCapsule v2: principal, path_scope, TaskInstruction/override records, delivery state, CAS | now |
| #204 | Typed rule engine core (schema, override_policy, validators, batch review library) | now |
| #205 | Skills ingestion + selection + AGENTS advisory + scaffold | now |
| #206 | ContextCompiler behind `repo_task_context` | after #187, #204, #205, #208 |
| #207 | Delivery engine + preflight `GUIDANCE_REQUIRED` handshake | after #206 |
| #209 | Memory proposals + constitution activation model | after #204, #206 |
| #212 | Foreman dispatch: queue, path-scope lease (TTL/fencing), capability-gated `task_next`, public task surface activation | after #208, #211 |
| #213 | Wire batch review diagnostic into `workspace_verify` | after #190, #204 |
| #194 | Gates **public activation/cutover only** — not internal development | — |

Foreman observability (#210) runs on the same track: cursor-based audit/journal reads,
**at-least-once + idempotent consumption** (never claim exactly-once), typed `CURSOR_GAP` when a
cursor predates the prune watermark.

## Non-goals (V1)

No receipt gating; no custom multi-language rule engine; no NL→rule extraction; no vector store as
memory; no auto-learning from merged PRs; no pack framework; no `memory_*` tools; no writable
user-level packs; no embedding-based skill matching; no skill script execution of any kind; no
multi-user auth; no Apps SDK UI.

## Supersession and relations

- #147 → rewrite narrow: optional MCP resources for catalog/reference fetch only — hosts do not
  guarantee resource inclusion, so resources are never an enforcement or injection channel
  (tracked in #195).
- Relates: #133 (consumer efficiency), #18 (TaskCapsule), #81 (approval plane), #38 (intelligence
  providers), #155/#158/#160/#161/#162 (governed intake family — shared approval plane, no
  duplication here).
- Host conformance (golden-prompt regression: tool selection, arguments, confirmation behavior,
  structured output) and the adversarial skills/AGENTS corpus belong to the #182 gate corpora.
