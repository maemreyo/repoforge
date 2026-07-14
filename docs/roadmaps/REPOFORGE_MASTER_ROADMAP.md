# RepoForge Master Roadmap

**Status:** Proposed  
**Scope:** Product, architecture, agent experience, execution, evidence, safety, and scale  
**Target horizon:** Post–RepoForge 2.0  
**Last updated:** 2026-07-15

---

## 1. Executive summary

RepoForge has evolved beyond a thin MCP wrapper around Git. Its current strengths include:

- allowlisted local repositories;
- isolated Git worktrees;
- optimistic file and workspace locking;
- protected branches and paths;
- bounded changes and command profiles;
- exact-tree verification receipts;
- non-force push and draft-only pull request creation;
- immutable configuration generations;
- atomic runtime hot reload;
- managed tunnel lifecycle;
- deterministic onboarding proposals;
- interactive safe-default review;
- stable MCP and release contracts;
- deterministic issue governance with a validated dependency graph and Ready-ticket selector;
- snapshot-bound explainable risk and ordered verification recommendations;
- reusable private atomic durable-state envelopes adopted by OperationTask;
- local-first setup/serve with standard-install interactive onboarding and deterministic path provenance;
- atomic dual-format patch normalization with bounded deterministic repair and actionable failures;
- bounded committed-snapshot tree, file, batch-read, search, commit, and comparison evidence without checkout;
- exact workspace-base freshness evidence and preview-bound merge-only refresh without rebase or force-push.

The next stage should not be driven by adding many more low-level Git tools. RepoForge should become a:

> **Task-oriented, evidence-driven, reproducible software-engineering control plane for humans and AI agents.**

The roadmap is organized around six coordinated programs:

1. **Agent Control Plane**
2. **ChatGPT-native User Experience**
3. **Evidence and Accuracy**
4. **Fast, Reproducible Execution**
5. **Security and Trust**
6. **Scale and Ecosystem**

The highest-priority outcomes are:

- resumable task state;
- one consistent workspace assessment;
- durable progress and cancellation for long operations;
- visual review and approval inside ChatGPT;
- behavior-level agent evaluation;
- CodeGraph-backed impact intelligence;
- architecture drift prevention;
- explainable risk and adaptive verification.

### 1.1 Delivery tracking and authority

The canonical execution index is [GitHub issue #3](https://github.com/maemreyo/repoforge/issues/3).
Initiatives and implementation tickets record hierarchy, blockers, acceptance criteria, tests, and
expected PR shape. Delivery status comes from current issue state and the validated dependency graph,
not from prose in this document or stale status text in an issue title.

An implementation ticket is selectable only when its canonical status is `Ready`, its parent is
active, its specification is complete, and every blocker is closed. Executable tests, safety policy,
typed contracts, and current product behavior remain higher authority than both roadmap and issue
metadata.

### 1.2 Active onboarding and patch-reliability batch

The current single-worktree batch implements #103, #104, #105, #106, #111, #112, #113, and #114.
It delivers command/document drift prevention, local-first setup and serve, standard-install interactive
onboarding, config/state path provenance, dual-format patch input, deterministic hunk repair,
whitespace-check/apply parity, and structured actionable failures. Parent initiatives #101 and #102
remain open because #107–#110 and #115–#116 are separate follow-on work. The next P0 selectable ticket
after this batch is #115; #116 remains blocked by #115.

---

## 2. Product vision

RepoForge should make the following workflow safe and natural:

```text
Understand task
    ↓
Recover durable task context
    ↓
Create or resume isolated workspace
    ↓
Inspect repository and semantic relationships
    ↓
Edit within policy
    ↓
Assess exact current workspace
    ↓
Explain impact, architecture drift, risk, and required checks
    ↓
Execute an immutable verification plan
    ↓
Produce exact-tree evidence
    ↓
Commit, push, and create or update a draft PR
```

For every task, RepoForge should answer five questions clearly:

1. What task and workspace are active?
2. What is the exact current source state?
3. What can the change affect?
4. What should run next, and why?
5. Is there sufficient current evidence to publish safely?

---

## 3. Strategic principles

### 3.1 RepoForge remains the policy enforcement point

External intelligence providers, analyzers, models, and UI clients are advisory. They do not decide repository authorization, path access, write eligibility, command eligibility, branch policy, verification sufficiency, or publish eligibility.

### 3.2 High-level workflows, granular escape hatches

The default agent experience should use a small number of task-oriented tools. Granular tools remain available for compatibility, debugging, and expert use.

### 3.3 One snapshot per decision

Impact, architecture, risk, security, and verification recommendations must be computed against the same repository or workspace, HEAD SHA, workspace fingerprint, configuration generation, and evidence snapshot.

### 3.4 Explainable automation

Every recommendation should include reason, evidence, confidence, uncertainty, and a safe fallback.

### 3.5 Smart means conservative under uncertainty

When evidence quality is weak, RepoForge broadens verification or asks for review. It does not silently infer safety.

### 3.6 Exact-tree evidence remains authoritative

Adaptive or cached checks improve iteration speed. The final commit gate remains bound to the exact current workspace tree.

### 3.7 Interfaces do not own policy

CLI, MCP, MCP Apps UI, and future A2A adapters only present and invoke application use cases. Policy belongs in domain and application layers.

---

## 4. Current-state assessment

RepoForge is already strong in the following areas:

| Area | Current strength |
| --- | --- |
| Repository access | Explicit allowlist and canonical roots |
| Repository snapshots | Issue #5 complete: immutable branch/commit tree, file, batch-read, and search operations with exact snapshot identity |
| Commit and comparison evidence | Issue #6 implementation: exact branch/tag/SHA commit metadata, first-parent/root file evidence, merge-base and ahead/behind facts, policy-filtered paths, message redaction, binary-patch omission, and bounded optional patches |
| CI failure evidence | Issue #7 complete: exact pushed-SHA Check Run selectors, bounded details, secret-safe failure excerpts, classification, and partial-coverage reporting |
| Durable operations | Issue #9 complete: protocol-independent state machine, private optimistic persistence, recovery/expiry/retention, progress, cancellation request, and bounded status/list interfaces |
| Workspace isolation | Per-task Git worktrees |
| Mutation safety | SHA/fingerprint optimistic locking |
| Change control | Protected paths, branches, file and diff budgets |
| Command execution | Reviewed named profiles rather than arbitrary shell |
| Workspace diagnostics | Issue #11 complete: typed reviewed profiles, constrained tracked selectors, bounded parsers, exact mutation detection, and safe failure classes |
| Verification | Exact-tree verification receipts |
| Publishing | Non-force push and draft PR only |
| Configuration | Immutable accepted generations and semantic deltas |
| Runtime | Atomic generation routing and disposal |
| Tunnel | Managed local lifecycle and health |
| Onboarding | Inspect → propose → decide → approve → activate |
| Operator UX | Plain and optional rich terminal review |
| Contracts | Stable CLI/MCP/release-contract testing |

The main product gaps are:

- task-capsule intent, decisions, and acceptance context are not yet a first-class durable entity;
- durable operations exist, but approved consumers have not yet adopted them for resumable execution;
- the agent must manually reconstruct broader task context after interruption;
- workspace analysis requires multiple tool calls;
- long-running work lacks a unified task/progress contract;
- tool-selection behavior is not evaluated end to end;
- semantic impact, architecture drift, and risk are not yet unified;
- execution is not fully reproducible across machines;
- analyzer outputs lack one normalized evidence model;
- team and multi-agent coordination are not first-class.

## 4.1 Cross-cutting foundation initiatives

The six product programs depend on five cross-cutting foundations tracked in the issue program:

| Initiative | Purpose | Primary consumers |
| --- | --- | --- |
| [#70 Durable State Platform](https://github.com/maemreyo/repoforge/issues/70) | Shared schema versions, atomic persistence, CAS, migrations, retention, quotas, integrity, and recovery | Tasks, operations, plans, evidence, receipts, leases, caches, and indexes |
| [#75 Protocol compatibility and client capability matrix](https://github.com/maemreyo/repoforge/issues/75) | Capability negotiation, compatibility conformance, contract versioning, fallbacks, and diagnostics | MCP core clients, Tasks, Apps, Elicitation, CLI, and future adapters |
| [#80 Human approval and consent plane](https://github.com/maemreyo/repoforge/issues/80) | Exact-state approval requests, action classification, receipts, replay protection, and operator handoff | Onboarding, plan acceptance, capability expansion, disclosure, and external writes |
| [#85 Provider trust and lifecycle](https://github.com/maemreyo/repoforge/issues/85) | Provider identity, digest, capability, health, canaries, upgrade, fallback, and evidence degradation | Code intelligence, analyzers, policy engines, and execution providers |
| [#89 Resource budgets and backpressure](https://github.com/maemreyo/repoforge/issues/89) | CPU, memory, disk, queue, concurrency, output, cache/index, and provider limits | Durable operations, indexing, analyzers, verification, multi-repo, and remote execution |

These initiatives do not create new authority. They make existing authority, state, compatibility,
provider evidence, and machine-resource limits explicit and reusable.

---

# Program 1 — Agent Control Plane

## 5. Task Capsule

### Goal

Create a durable task state independent of chat history and workspace persistence.

### Domain model

```text
TaskCapsule
  task_id
  repo_ids
  workspace_ids
  intent
  acceptance_criteria
  constraints
  source_issue_or_pr
  active_config_generation
  accepted_plan_id
  decisions
  evidence_snapshot_ids
  receipts
  current_phase
  blocked_reason
  open_questions
  next_safe_actions
  created_at
  updated_at
```

### Public operations

```text
task_start
task_resume
task_status
task_cancel
task_complete
```

### Requirements

- A task may exist before a workspace.
- A task may later contain several repository workspaces.
- Task state must not store raw secrets, source bodies, or unbounded logs.
- Resume output must be compact enough for an agent context.
- State transitions must be explicit and auditable.
- Every stored plan and receipt must be bound to current workspace state.

### Value

- reliable resume after chat reload or process restart;
- agent handoff;
- clear task progress;
- reproducible testing;
- multi-repository foundation.

---

## 6. Unified Workspace Assessment

### Goal

Replace repeated manual status, diff, impact, architecture, risk, and verification-selection calls with one consistent assessment transaction.

### Current shell and future public operation

The application layer now provides a snapshot-consistent internal assessment over status, diff,
policy, base, PR, CI, failure-reference, verification-receipt, explainable risk, and ordered
verification-recommendation evidence. It rechecks HEAD, workspace fingerprint, configuration
generation, and policy hash after every provider boundary. Missing evidence raises rather than lowers
risk, every recommendation retains the configured final exact-tree profile, and critical paths require
manual review. Semantic impact, architecture analysis, immutable-plan execution, caching, and public
MCP exposure remain separate follow-on work.

```text
workspace_assess(workspace_id)
```

### Response

```json
{
  "workspace_id": "ws_...",
  "head_sha": "...",
  "workspace_fingerprint": "...",
  "snapshot_id": "...",
  "changed_paths": [],
  "diff_summary": {},
  "policy": {
    "valid": true,
    "budget": {}
  },
  "impact": {
    "affected_symbols": [],
    "affected_paths": [],
    "affected_flows": [],
    "affected_tests": []
  },
  "architecture": {
    "blocking": false,
    "new_violations": [],
    "known_violation_count": 0,
    "resolved_violation_count": 0
  },
  "security": {
    "findings": []
  },
  "risk": {
    "score": 0,
    "level": "low",
    "factors": []
  },
  "receipts": {
    "architecture_current": false,
    "verification_current": false
  },
  "verification_plan": {
    "plan_id": "...",
    "stages": [],
    "final_profile": "full"
  },
  "next_safe_actions": []
}
```

### Invariants

- All evidence uses one workspace snapshot.
- Assessment is read-only.
- Partial failure commits no partial assessment state.
- A source mutation during assessment returns a stale-snapshot error.
- Low evidence coverage broadens the verification plan.
- No affected tests is not interpreted as no verification required.

---

## 7. Immutable Execution Plans

### Goal

Separate analysis from execution and prevent recommendations from changing between approval and use.

### Plan binding

```text
plan_id
task_id
workspace_id
head_sha
workspace_fingerprint
config_generation
evidence_snapshot_id
architecture_policy_hash
risk_assessment_hash
ordered_stages
created_at
```

### Operations

```text
workspace_execute_plan(workspace_id, plan_id, through)
```

`through` may be `iteration` or `full`.

### Rules

- A stale plan cannot execute.
- Execution may stop at the first failed required stage.
- The final stage delegates to the existing authoritative verification use case.
- Plan execution creates structured stage receipts.
- Only final exact-tree verification updates commit eligibility.

---

## 8. Durable operations and progress

Issue #9 delivers the protocol-independent operation state machine: explicit immutable transitions,
monotonic progress, cancellation requests separate from terminal cancellation, startup orphaning,
expiry and retention, and bounded status/list/cancel interfaces through both CLI and MCP. Issue #71
extracts the reusable durable-state substrate beneath it: typed schema versions and revisions, private
bounded JSON, atomic replace and directory fsync, revision compare-and-swap, stable corruption and
future-schema errors, and safe audit metadata. Concrete workers, migrations, quotas, resumable
verification/indexing, PR-check watching, and MCP Tasks adaptation remain future consumer work.

### Goal

Model indexing, onboarding, builds, scans, and full verification as durable operations.

### Core abstraction

```text
OperationTask
  task_id
  kind
  state
  phase
  progress
  result_reference
  error
  retryability
  cancel_supported
  created_at
  updated_at
  expires_at
```

Adopt MCP Tasks through an interface adapter when negotiated by the client. Keep RepoForge's own task store and state machine protocol-independent.

Suitable workloads include CodeGraph indexing, full verification, repository discovery, onboarding batches, security analysis, diagnostics collection, and multi-repository assessment.

---

## 9. Workspace and task leases

### Goal

Prevent concurrent mutation by multiple agents while allowing safe read-only collaboration.

### Model

```text
mutation_owner
lease_token_hash
lease_expires_at
heartbeat_at
read_observers
takeover_history
```

### Rules

- One mutation owner per workspace.
- Multiple read-only observers are allowed.
- Expired-lease takeover requires a fresh workspace assessment.
- Takeover invalidates old plans.
- Lease acquisition and renewal use compare-and-swap.

---

# Program 2 — ChatGPT-native User Experience

## 10. MCP Apps UI

### Goal

Move complex review and progress experiences from JSON and terminal-only interfaces into ChatGPT.

### Primary screens

- **Repository Overview:** health, active generation, policy, profiles, providers, knowledge pack, warnings.
- **Task Dashboard:** objective, acceptance criteria, phase, blockers, next actions, progress and cancellation.
- **Workspace Assessment:** changed files, impact graph, affected tests, architecture violations, risk factors, verification stages.
- **Approval Review:** configuration diff, capability delta, approved commands, network policy, negative-by-default confirmation.
- **Runtime Operations:** supervisor, MCP, tunnel, providers, tasks, disk, locks, and safe remediation.

UI never becomes the source of truth. All inputs are validated server-side and every approval uses the existing approval model.

---

## 11. MCP Elicitation

### Goal

Ask only the decisions required to continue a workflow.

Use it for base branch, package manager, monorepo scope, command profile, source-snippet disclosure, provider enablement, remediation choice, and verification policy.

Clients without Elicitation receive a structured `INPUT_REQUIRED` result with stable decision IDs. Form elicitation must not collect secrets.

---

## 12. Public Tool Surface Simplification

### Recommended default tools

```text
repo_overview
task_start
task_status
workspace_assess
workspace_execute_plan
workspace_publish
task_cancel
```

Existing granular tools remain available but should be deferred where supported, grouped by namespace, omitted from the default agent context when unnecessary, and tested for routing quality.

---

## 13. Structured Next Actions

Every operation should return machine-readable safe actions:

```json
{
  "next_safe_actions": [
    {
      "action": "workspace_assess",
      "reason": "The workspace changed after the previous assessment.",
      "required": true
    }
  ]
}
```

Next actions are recommendations, not execution authority.

---

# Program 3 — Evidence and Accuracy

## 14. Shared Evidence Model

### Goal

Normalize evidence from Git, CodeGraph, analyzers, verification, CI, and configuration.

```text
evidence_id
source
provenance
scope
snapshot_id
head_sha
workspace_fingerprint
config_generation
coverage
confidence
paths
symbols
summary
stale
created_at
```

Evidence cannot grant permissions. Stale evidence cannot satisfy a gate. Conflicting evidence is surfaced. Low coverage creates explicit uncertainty.

---

## 15. CodeGraph Integration

### Architecture

```text
RepoForge application
    ↓
CodeIntelligencePort
    ↓
RepoForge-owned structured sidecar
    ↓
CodeGraph library
```

### Capabilities

- symbol search;
- callers and callees;
- task context;
- impact radius;
- affected tests;
- graph coverage;
- repository and workspace snapshots.

### Safeguards

- no raw `projectPath` in public MCP;
- no shared CodeGraph daemon or native watcher;
- no adoption of the user's `.codegraph/`;
- managed index excluded from Git-visible state;
- provider instructions never reach ChatGPT;
- snippets pass local egress scanning;
- provider release and integrity are locked;
- semantic canaries gate upgrades.

---

## 16. Architecture Drift and Policy Gate

### Initial rule types

```text
forbidden_dependency
allowed_dependency_matrix
cycle_free
forbidden_symbol_access
```

### Frozen baseline

```text
current violations
  ├── known baseline violations
  ├── new violations
  └── resolved violations
```

Only new approved blocking-rule violations prevent commit.

### Enforcement ladder

```text
observe
warn
block
```

Heuristic edges are warning-only by default. Baseline creation and weakening require approval. Architecture receipts bind to exact workspace state.

---

## 17. Explainable Change Risk

Risk factors may include architecture violations, public API changes, critical paths, runtime or security code, schema/migration files, dependency manifests, cross-layer impact, affected-test breadth, graph coverage, and change-budget use.

Output:

```text
score
level
factors
evidence
uncertainties
critical_flows
affected_paths
affected_tests
```

Risk never expands permissions. It only broadens or orders review and verification.

---

## 18. Adaptive Verification Planner

### Example ladder

- **Low:** architecture, affected tests, quick checks, final full profile.
- **Medium:** architecture, affected tests, quick, test, final full profile.
- **High:** architecture, affected tests, quick, test, integration/preflight, final full profile.
- **Critical:** architecture, all relevant profiles, final full profile, manual-review warning.

Empty affected-test output never skips required tests. Low graph confidence broadens the plan. Targeted test paths must be tracked, normalized, approved files. Commands remain typed argv.

---

## 19. Repository Knowledge Pack

### Inputs

- AGENTS.md hierarchy;
- approved Skills;
- coding conventions;
- architecture policy;
- critical paths;
- ownership;
- release process;
- known flaky tests;
- verification profiles;
- migration rules.

### Workflow

```text
discover
    → inspect
    → classify
    → resolve conflicts
    → approve
    → hash and bind to generation
    → monitor drift
```

Skills and repository instructions are privileged inputs and must not be trusted automatically.

---

# Program 4 — Fast, Reproducible Execution

## 20. Execution Environment Port

```text
ExecutionEnvironmentPort
  ├── NativeReviewedAdapter
  ├── DevContainerAdapter
  └── HermeticContainerAdapter
```

Native reviewed execution preserves current behavior. Dev Container use requires review of images, mounts, privileges, lifecycle commands, network, host access, and secrets. Hermetic execution runs in an ephemeral reviewed environment. A Dagger adapter may later be optional.

---

## 21. Verification DAG

```text
environment_prepare
  ├── format_check
  ├── lint
  ├── typecheck
  └── unit_tests
        └── integration_tests
              └── build
                    └── final_gate
```

Each stage declares dependencies, argv, working directory, timeout, network policy, mutability, cache policy, required risk level, artifacts, and failure severity.

Existing profiles can compile into a linear DAG.

---

## 22. Content-addressed Verification Cache

Cache identity includes:

```text
workspace_fingerprint
stage_hash
argv_hash
working_directory
environment_identity
toolchain_versions
lockfile_hashes
network_policy
config_generation
provider_versions
```

Cache accelerates iteration. Environment mismatch invalidates reuse. Final commit eligibility follows repository policy.

---

## 23. Failure Intelligence

Issue #7 delivers the first production slice: workspace PR checks expose opaque Check Run selectors,
and exact pushed-SHA details/failure evidence provides bounded annotations, failed-step context, secret-safe
excerpts, deterministic classification, retryability, truncation, and partial-source coverage. Issue #11
adds repository-reviewed workspace diagnostics with typed selectors, bounded parser output, explicit
failure classes, exact pre/post fingerprints, artifact-path enforcement, and verification invalidation.
Diagnostics remain synchronous and local-only, never accept model-authored argv, and do not replace the
final verification gate. Broader cross-provider diagnosis, automatic verification-plan integration,
durable background diagnostics, and check watching/cancellation remain future work.

### Failure classes

```text
tool_missing
dependency_missing
environment_mismatch
timeout
test_failure
lint_failure
type_failure
build_failure
network_failure
flaky_suspected
policy_failure
stale_workspace
provider_failure
```

Responses include the first relevant error, likely scope, whether files changed, reproducibility, safe recovery options, failed stage, and refreshed fingerprint.

No automatic source fix is allowed unless explicitly approved.

---

# Program 5 — Security and Trust

## 24. Analyzer Plugin Protocol

```text
AnalyzerPort
  doctor()
  analyze(snapshot)
  findings()
  artifacts()
```

Normalized findings include analyzer, rule, severity, confidence, category, path, line, symbol, message, evidence, remediation, snapshot, and blocking policy.

First integrations:

- SARIF ingestion;
- OSV dependency scan;
- secret scanning;
- license policy;
- SBOM generation;
- optional CodeQL and Semgrep adapters.

Analyzer output is evidence, not authorization.

---

## 25. Secret-safe Egress

All model-bound source and findings pass a local egress policy:

```text
allow
redact_lines
withhold_snippet
reject_result
```

Checks include token patterns, private-key markers, credential URLs, high-confidence entropy, binary content, denied configuration shapes, and size limits.

No detected secret is written to audit logs or diagnostics.

---

## 26. Signed Verification Attestations

Design receipts for future in-toto/Sigstore-compatible signing.

Attestation subject may be a commit SHA, tree hash, or artifact digest. Predicate includes repository, generation, fingerprint, architecture policy, plan, command receipts, environment identity, analyzers, and timestamp.

---

## 27. Network and Capability Policy

Every executable stage or provider declares:

```text
network: none | restricted | external
filesystem: read | workspace-write | managed-state-write
process: bounded
credentials: none | named-capability
external_write: false | approved
```

Unexpected access fails closed where enforceable and is reported elsewhere.

---

# Program 6 — Scale and Ecosystem

## 28. OpenTelemetry and Agent Traces

Trace:

```text
MCP request
  → routing
  → application use case
  → policy
  → provider
  → Git/filesystem
  → execution
  → state transition
  → response
```

Safe metadata includes action, duration, status, error code, hashed repository identity, workspace/task ID, generation, cache hit, provider, result counts, and truncation.

Do not emit source, patches, secrets, full paths, or command output.

Important product metrics:

- task completion rate;
- tool calls per completed task;
- time to first correct plan;
- unsafe retry rate;
- stale-plan rate;
- assessment latency;
- provider fallback frequency;
- verification cache hit rate.

---

## 29. Agent Workflow Record and Replay

The internal foundation now defines schema-versioned sanitized recordings containing tool inventory,
selected tool, normalized argument categories and hashes, result category, stable error code, safe
workspace/task/snapshot references, state transitions, next actions, metrics, and explicit truncation.
Private checksum-framed JSON persistence enforces deterministic serialization, `0600` files, bounded
size/event counts, retention, and explicit fixture export.

Replay runs only through adapters declaring isolation and no real writes. The default adapter replays
recorded categories without filesystem, Git, GitHub, network, or subprocess access. Truncated records
remain readable for diagnosis but are not eligible as complete evaluation evidence.

Use this foundation for model-upgrade evaluation, tool-description experiments, routing regression,
incident reproduction, and latency/call-count comparison. Prompts, source, patches, logs, full paths,
credentials, private reasoning traces, and provider payloads remain prohibited.

---

## 30. Behavioral Tool Evals

Test direct and indirect wording, ambiguity, stale workspaces, partial external failure, retries, adversarial prompts, insufficient evidence, unavailable tools, and disabled capabilities.

Measure correct first tool, unnecessary calls, unsafe arguments, ignored errors, completion correctness, and total context/tool cost.

Tool metadata changes should pass behavioral evals before release.

---

## 31. Runtime Operations Center

Unified status should cover supervisor, MCP runtime, tunnel, active generation, provider processes, indexes, tasks, workspaces, locks, disk, health, warnings, and safe remediations.

Proposed future CLI surface:

```text
status
doctor --explain
doctor --fix-safe
doctor --plan-fixes
```

Safe repairs include stale sockets, dead process records, expired tasks, old cache, abandoned managed-index locks, and unsafe private file modes. Capability or policy changes still require approval.

---

## 32. Multi-repository Task Bundles

```text
TaskBundle
  task_id
  workspaces
  dependency_order
  shared decisions
  evidence
  verification plan
  publish saga
```

Publishing uses a saga:

```text
prepare all
verify all
commit locally
push in dependency order
create/update draft PRs
report exact partial state
```

---

## 33. A2A Adapter

A future A2A adapter may expose RepoForge tasks to independent agents, but it remains outside the core.

Core domain remains:

```text
Task
Workspace
Evidence
Plan
Receipt
Policy
```

Interfaces may include CLI, MCP, MCP Apps UI, and A2A.

---

# 34. Recommended Public Workflow

```text
task_start(repo_id, task_description, issue_or_pr)
    ↓
granular inspect/edit operations
    ↓
workspace_assess(task_id)
    ↓
workspace_execute_plan(task_id, plan_id, through="iteration")
    ↓
workspace_assess(task_id)
    ↓
workspace_execute_plan(task_id, plan_id, through="full")
    ↓
workspace_publish(task_id, commit_message, draft_pr)
```

`workspace_publish` validates receipts, commits the exact verified tree, pushes without force, creates or updates a draft PR, and reports exact external state.

---

# 35. Roadmap phases

## Phase 1 — Agent Control Plane Foundation

**Priority:** P0

Deliver Task Capsule, task store, resume/status/cancel, leases, unified assessment, immutable plans, durable operations, and structured next actions.

### Exit criteria

- Tasks resume without reconstructing state from chat.
- Assessment data is snapshot-consistent.
- Stale plans cannot execute.
- Long operations expose progress and cancellation.

## Phase 2 — ChatGPT-native UX

**Priority:** P0

Deliver MCP Apps dashboards, Elicitation, approval UI, task progress, runtime operations UI, and protocol fallbacks.

### Exit criteria

- Full onboarding and assessment can be reviewed in ChatGPT.
- No policy logic exists in the UI.
- Every approval is server-validated.

## Phase 3 — Evidence and Accuracy

**Priority:** P1

Deliver shared evidence, CodeGraph, graph snapshots, Architecture Drift observe mode, risk, adaptive planning, and Repository Knowledge Pack.

### Exit criteria

- One snapshot powers impact, architecture, risk, and planning.
- Provider failure does not disable existing RepoForge.
- Semantic canaries gate provider releases.
- Low confidence broadens verification.

## Phase 4 — Reproducible Execution

**Priority:** P1

Deliver execution environments, verification DAG, cache, and failure intelligence.

### Exit criteria

- Environment identity is part of execution receipts.
- Cache reuse is explainable.
- Full final verification remains authoritative.
- Failures return structured recovery.

## Phase 5 — Security and Trust

**Priority:** P1–P2

Deliver analyzer protocol, SARIF, OSV, secret egress, SBOM, capability policy, and attestation-ready receipts.

### Exit criteria

- Analyzer findings share one schema.
- Secrets do not enter model, audit, or diagnostics.
- External writes remain explicit.
- Evidence can be exported as an attestation.

## Phase 6 — Scale

**Priority:** P2–P3

Deliver OpenTelemetry, record/replay, behavioral evals, multi-repo bundles, multi-agent leases, remote deployment profiles, and optional A2A.

### Exit criteria

- Agent workflow quality is regression-tested.
- Multi-repository tasks report partial failure deterministically.
- Runtime behavior is observable without leaking source.

---

# 36. Dependency graph

```text
Delivery Graph and Derived Readiness
    └── Every issue-driven delivery wave

Durable State Platform
    ├── Task Capsule
    ├── Durable Operations and Plans
    ├── Evidence and Receipts
    └── Leases, Caches, and Indexes

Protocol Compatibility Matrix
    ├── MCP Tasks and Progress
    ├── MCP Apps and Elicitation
    ├── Tool Surface Versioning
    └── Client Diagnostics

Human Approval Plane
    ├── Configuration and Capability Changes
    ├── Plan Acceptance
    ├── Sensitive Disclosure
    └── External Writes

Provider Trust and Lifecycle
    ├── Code Intelligence
    ├── Analyzer Plugins
    ├── Policy Providers
    └── Execution Providers

Resource Budgets and Backpressure
    ├── Durable Operations
    ├── Verification and Analyzers
    ├── Caches and Indexes
    └── Multi-repository and Remote Execution

Task Capsule
    ├── Durable Operations
    ├── Leases
    ├── MCP Apps Task UI
    └── Multi-repo Bundles

Shared Evidence Model
    ├── CodeGraph
    ├── Analyzer Plugins
    ├── Architecture Drift
    └── Risk Assessment

Unified Workspace Assessment
    ├── Shared Evidence
    ├── Architecture Drift
    ├── Risk Assessment
    └── Verification Planner

Immutable Verification Plan
    ├── Verification DAG
    ├── Targeted Tests
    ├── Execution Cache
    └── Plan Execution

Execution Environment Port
    ├── Dev Container
    ├── Hermetic Execution
    └── Environment Identity

Observability
    ├── Task Traces
    ├── Record/Replay
    └── Behavioral Evals
```

---

# 37. Suggested delivery sequence

## Wave 1

1. Task Capsule.
2. Unified assessment shell using existing Git evidence.
3. Immutable plan store.
4. Durable progress abstraction.
5. Behavioral tool eval harness.

## Wave 2

1. MCP Apps UI and Elicitation.
2. CodeGraph structured sidecar.
3. Shared evidence.
4. Architecture observe mode.
5. Risk assessment and advisory plans.

## Wave 3

1. Adaptive iteration execution.
2. Architecture warn/block rollout.
3. Verification DAG.
4. Execution cache.
5. Failure intelligence.

## Wave 4

1. Analyzer protocol.
2. OSV, SARIF, and secret scanning.
3. Dev Container and hermetic adapters.
4. OpenTelemetry and record/replay.
5. Attestation support.

## Wave 5

1. Multi-repository Task Bundles.
2. Advanced multi-agent leases.
3. Team/remote runtime.
4. Optional A2A interface.

---

# 38. Explicit non-goals

Do not:

- expose arbitrary shell;
- merge PRs;
- force-push;
- auto-enable capabilities for existing repositories;
- treat model confidence as authorization;
- let risk reduce final verification below policy;
- automatically trust Skills or repository instructions;
- hard-code all analyzers into the application layer;
- make containers or CodeGraph mandatory for basic use;
- build A2A before a concrete multi-agent requirement;
- turn RepoForge into a general CI platform.

---

# 39. Program-level success metrics

## Agent effectiveness

- fewer tool calls per correctly completed task;
- higher first-tool selection accuracy;
- lower stale-state error rate;
- lower unsafe retry rate;
- faster resume after interruption.

## Accuracy

- assessment snapshot consistency;
- architecture violation precision;
- affected-test recall;
- explicit uncertainty rate;
- reduced missed verification.

## Performance

- warm overview latency;
- warm assessment latency;
- indexing and sync time;
- verification cache hit rate;
- runtime reload time;
- task completion time.

## Safety

- no unauthorized path access;
- no arbitrary command execution;
- no stale receipt commits;
- no source or secret leakage;
- no orphan provider process;
- immediate repository revocation.

## UX

- onboarding completion rate;
- approval comprehension;
- cancellation success;
- recovery success after failure;
- reduced terminal dependency for ChatGPT users.

---

# 40. Top five recommended next initiatives

1. **Issue-driven delivery governance and next-ready automation** — complete #61, then #68/#69 and #63/#64.
2. **Unified Workspace Assessment** — complete base freshness and typed diagnostics, then #14–#16.
3. **Durable State Platform and Task Control Plane** — complete #71, then Task Capsule and the remaining state lifecycle work.
4. **Protocol Compatibility and Human Approval** — complete #29/#76/#77/#79 before client-specific UX, and #81–#84 before Elicitation approvals.
5. **Provider Trust and Resource Control** — complete #86–#88 before semantic provider expansion and #90–#93 before heavy indexing, caches, multi-repository, or remote execution.

CodeGraph, Architecture Drift, MCP Apps, Adaptive Verification, analyzers, and isolated execution should consume these foundations rather than introduce parallel state, approval, provider, compatibility, or resource-control semantics.

---

# 41. Final target

```text
RepoForge today
    Safe local Git and MCP execution platform

RepoForge target
    Task-oriented
    evidence-driven
    reproducible
    explainable
    user-centric
    local software-engineering control plane
    for humans and AI agents
```

The roadmap should be implemented incrementally. Each capability must preserve the current production safety model, remain optional where appropriate, and pass deterministic unit, integration, security, behavioral, clean-install, and end-to-end verification gates before release.
