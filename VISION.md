# RepoForge Vision

## The control plane for agentic software engineering

RepoForge exists to make software engineering with humans and AI agents safe, explainable, reproducible, and evidence-driven.

It is not merely a bridge between an AI model and a Git repository. It is the control plane that governs how engineering intent becomes a real software change.

---

## Vision

> A future where humans and AI agents can build software at high speed while every change remains safe, explainable, verifiable, reproducible, and under human control.

RepoForge aims to become the trusted execution and evidence layer between:

- human intent;
- AI agents and developer tools;
- source repositories;
- execution environments;
- analyzers and code-intelligence providers;
- CI/CD systems;
- Git hosting platforms;
- and the people responsible for approving and operating software delivery.

In this future, a developer should be able to say:

> Pick the next safe task, understand the repository, make the change, verify it, and prepare it for review.

RepoForge should then coordinate the complete workflow:

```text
Understand intent
    ↓
Recover durable task context
    ↓
Inspect repository instructions and current source state
    ↓
Create or resume an isolated workspace
    ↓
Assess impact, architecture, risk, and required verification
    ↓
Execute an approved, immutable plan
    ↓
Produce exact-tree evidence
    ↓
Commit, push, and prepare a draft pull request
    ↓
Return the exact resulting state for human review
```

The result is not autonomous coding without limits.

The result is **controlled engineering automation with explicit authority, current evidence, and human accountability**.

---

## Mission

> RepoForge turns engineering intent into evidence-backed software changes by orchestrating tasks, constraining capabilities, isolating execution, assessing impact, and verifying the exact source state before publication.

RepoForge fulfills this mission by providing:

1. **Accurate repository context**  
   Repository instructions, source state, issues, architecture rules, verification profiles, and prior decisions are collected into a consistent working context.

2. **Task-oriented orchestration**  
   Work is managed through durable tasks, workspaces, plans, operations, evidence, and receipts rather than disconnected tool calls or chat history.

3. **Constrained execution**  
   Repositories, branches, paths, commands, environments, network access, and external writes remain explicitly controlled.

4. **Exact-state verification**  
   Evidence and verification are bound to the precise source tree, workspace fingerprint, configuration generation, and execution environment that produced them.

5. **Explainable recommendations**  
   RepoForge explains what should happen next, why, which evidence supports the recommendation, what remains uncertain, and which fallback is safest.

6. **Human authority**  
   AI agents may automate approved and recoverable work, while capability expansion, policy changes, sensitive actions, and irreversible decisions remain under human control.

---

## Who RepoForge Is For

ChatGPT is an initial client, not the final boundary of the platform.

RepoForge should support any authorized human, agent, or system that needs to understand, modify, verify, review, or publish software safely.

### Builders

Builders directly create or modify software:

- software developers;
- ChatGPT and other MCP clients;
- coding agents;
- IDE agents and extensions;
- documentation agents;
- migration and modernization agents;
- internal engineering automation.

### Reviewers

Reviewers inspect evidence and decide whether work is acceptable:

- code reviewers;
- technical leads;
- QA engineers;
- security engineers;
- architecture reviewers;
- release managers;
- compliance reviewers.

### Operators

Operators manage the environment in which engineering work runs:

- platform engineers;
- DevOps and SRE teams;
- security teams;
- internal developer-platform teams;
- runtime and infrastructure administrators.

### Automated systems

RepoForge should also integrate with:

- CI/CD systems;
- GitHub, GitLab, Bitbucket, Forgejo, and enterprise Git hosts;
- issue and project-management systems;
- code-intelligence providers;
- policy engines;
- artifact registries;
- observability systems;
- identity providers;
- agent orchestrators;
- optional agent-to-agent protocols.

---

## Product Positioning

RepoForge is:

> **A software engineering control plane for humans and AI agents.**

It is the layer that coordinates and governs:

```text
Intent
  → Task
  → Workspace
  → Evidence
  → Plan
  → Execution
  → Verification
  → Receipt
  → Publication
```

RepoForge is not:

- a foundation model;
- a general-purpose coding assistant;
- an IDE;
- a remote terminal;
- an unrestricted filesystem bridge;
- a generic Git wrapper;
- a full CI platform;
- a merge bot;
- a secret manager;
- a replacement for human ownership.

Those systems may connect to RepoForge, but they do not replace its role as the policy, execution, and evidence boundary.

---

## Core Product Promise

> No software change is considered ready merely because the code looks plausible.

A change is ready only when RepoForge can explain:

- what task was being performed;
- which source state was used;
- what changed;
- what may be affected;
- which policies applied;
- which risks and uncertainties remain;
- which checks ran;
- which environment ran them;
- whether the evidence is still current;
- which actions were authorized;
- and what external state was created.

---

## Strategic Principles

### 1. Safety before autonomy

AI agents may act only within explicitly approved capabilities.

Greater intelligence must not silently grant greater authority.

### 2. Evidence before confidence

Model confidence is advisory.

It cannot replace source inspection, policy validation, test results, verification receipts, or human review.

### 3. Exact state before action

Every important decision must be bound to the exact repository, commit, workspace fingerprint, configuration generation, and evidence snapshot it was based on.

Stale plans and stale evidence must fail closed.

### 4. One snapshot per decision

Impact, architecture, security, risk, and verification recommendations must be computed against one consistent source snapshot.

Conflicting or incomplete evidence must be surfaced explicitly.

### 5. Explainability before convenience

Every recommendation should include:

- the proposed action;
- the reason;
- supporting evidence;
- confidence;
- uncertainty;
- and the safest fallback.

Fast automation that cannot be explained or audited is not a successful outcome.

### 6. Human authority is final

RepoForge should make human decisions easier, faster, and better informed.

It should not remove human ownership of capability expansion, sensitive publication, policy changes, releases, or irreversible operations.

### 7. Local-first, portable by design

The default experience should protect source code and credentials on the operator's machine.

At the same time, the domain model must remain independent of a single machine, client, protocol, Git provider, or execution backend.

### 8. Interfaces do not own policy

MCP, CLI, REST APIs, IDE extensions, dashboards, webhooks, and future A2A adapters are interfaces.

Policy and business invariants belong in the domain and application layers.

### 9. Providers are advisory and replaceable

Code-intelligence engines, analyzers, policy engines, container runtimes, and external services may contribute evidence.

They may not become the source of authorization.

RepoForge must remain functional when an optional provider is unavailable.

### 10. Reproducibility is part of correctness

The execution environment, toolchain, lockfiles, provider versions, network policy, and source state are part of the result.

A verification result without enough environment identity is incomplete evidence.

---

## Long-Term Domain Model

RepoForge should converge around a small set of stable domain entities.

### Task

Represents the durable engineering objective:

- intent;
- acceptance criteria;
- constraints;
- decisions;
- blockers;
- current phase;
- next safe actions.

### Workspace

Represents the exact isolated source state used for a task:

- repository;
- base and HEAD;
- workspace fingerprint;
- policy context;
- active owner or lease;
- change metrics.

### Evidence Snapshot

Represents normalized, versioned evidence:

- source and provider;
- provenance;
- scope;
- coverage;
- confidence;
- limitations;
- source-state bindings;
- staleness.

### Execution Plan

Represents an immutable, approved plan bound to current context:

- ordered stages;
- required capabilities;
- risk mitigations;
- verification scope;
- environment requirements;
- source-state bindings.

### Operation

Represents durable work that may take time:

- state;
- phase;
- progress;
- cancellation;
- retryability;
- result reference;
- error classification.

### Receipt

Represents the outcome of an action:

- subject;
- result;
- environment identity;
- evidence references;
- policy decision;
- exact source-state binding.

### Policy Decision

Represents an explainable authorization result:

- allowed or denied;
- reasons;
- policy version;
- input digest;
- matched rules;
- missing evidence;
- safe alternatives.

### Actor and Identity

Represents the human, agent, service, or workload performing an action:

- identity type;
- scopes;
- roles;
- workload identity;
- authorization context.

---

## Target Architecture

```text
Clients
  ├── ChatGPT and MCP clients
  ├── Coding agents
  ├── CLI
  ├── IDE extensions
  ├── MCP Apps and dashboards
  ├── REST or JSON APIs
  ├── CI/CD integrations
  └── Optional A2A adapters
            │
            ▼
Interface Adapters
            │
            ▼
Application Use Cases
  ├── Task management
  ├── Workspace assessment
  ├── Plan and execution management
  ├── Verification orchestration
  ├── Evidence management
  ├── Policy and authorization
  └── Publication
            │
            ▼
Domain Core
  ├── Task
  ├── Workspace
  ├── Evidence
  ├── Plan
  ├── Operation
  ├── Receipt
  ├── Policy Decision
  └── Actor and Identity
            │
            ▼
Ports
  ├── Repository and Git hosting
  ├── Code intelligence
  ├── Execution environments
  ├── Verification
  ├── Policy engines
  ├── Storage
  ├── Identity
  └── Events and messaging
            │
            ▼
Adapters and Infrastructure
  ├── Git and GitHub
  ├── GitLab and other forges
  ├── Native and isolated runtimes
  ├── CI providers
  ├── Code analyzers
  ├── Artifact stores
  ├── Local durable state
  └── Optional remote services
```

The domain core must not depend on a particular client, protocol, provider, or deployment model.

---

## Future Capabilities

RepoForge should evolve incrementally toward the following capabilities.

### Agent control plane

- durable Task Capsules;
- task resume and handoff;
- workspace ownership and leases;
- structured next-safe-action recommendations;
- immutable execution plans;
- durable operations with progress and cancellation.

### Unified assessment

- exact workspace status;
- policy validation;
- base freshness;
- change impact;
- affected tests;
- architecture drift;
- security evidence;
- explainable risk;
- verification recommendations.

### ChatGPT-native and visual workflows

- task dashboards;
- workspace assessment review;
- impact visualization;
- approval interfaces;
- progress and cancellation;
- runtime operations;
- capability-aware elicitation;
- structured fallbacks for clients without advanced protocol support.

### Code intelligence

- syntax-aware repository inspection;
- symbols, definitions, and references;
- callers and callees;
- dependency and flow analysis;
- affected-test recommendations;
- provider coverage and confidence;
- optional graph-based providers.

### Reproducible execution

- environment identity;
- native reviewed execution;
- optional dev-container and hermetic adapters;
- verification DAGs;
- explainable caching;
- structured failure intelligence;
- exact-tree final verification.

### Security and trust

- normalized analyzer findings;
- dependency and vulnerability evidence;
- SBOM and license policy;
- secret-safe egress;
- explicit network and capability policy;
- workload identity;
- signed verification attestations;
- independent receipt verification.

### Scale and collaboration

- behavioral agent evaluations;
- workflow record and replay;
- OpenTelemetry-compatible traces;
- multi-repository task bundles;
- multi-agent coordination;
- team and remote execution profiles;
- optional A2A integration.

---

## Non-Goals

RepoForge should not become:

- an unrestricted shell for AI agents;
- a service that bypasses repository protections;
- a system that automatically trusts repository instructions or skills;
- a tool that reduces required verification based only on model confidence;
- a platform that merges or releases software without explicit policy and human authority;
- a mandatory container or graph-indexing platform;
- a provider-specific orchestration layer;
- a general-purpose CI replacement.

These boundaries protect the clarity and trustworthiness of the product.

---

## Success Criteria

RepoForge succeeds when it improves both engineering speed and engineering confidence.

### Agent effectiveness

- fewer tool calls per correctly completed task;
- higher first-tool selection accuracy;
- faster recovery after interruption;
- fewer unsafe retries;
- fewer stale-state failures.

### Accuracy

- consistent assessment snapshots;
- high affected-test recall;
- low false-positive policy blocking;
- explicit uncertainty;
- fewer missed verification requirements.

### Safety

- no unauthorized repository or path access;
- no arbitrary command execution;
- no stale-receipt commits;
- no secret leakage;
- no silent capability expansion;
- immediate revocation of removed repository access.

### Performance

- low warm assessment latency;
- efficient repository indexing;
- explainable verification-cache reuse;
- fast task resume;
- bounded runtime and storage overhead.

### User experience

- clear next actions;
- understandable approvals;
- reliable cancellation and recovery;
- reduced dependence on terminal-only workflows;
- consistent behavior across clients.

### Trust

- every important action is auditable;
- every recommendation is explainable;
- every receipt is bound to current state;
- every external write reports its exact resulting state.

---

## Product Statement

> RepoForge is the control plane that enables humans and AI agents to understand, change, verify, and publish software safely.

It provides the durable context, policy enforcement, isolated execution, current evidence, and exact-state verification required for agentic software engineering to become dependable infrastructure rather than a collection of powerful but disconnected tools.

---

## Tagline

**RepoForge — The control plane for agentic software engineering.**

**Safe execution. Exact evidence. Human control.**
