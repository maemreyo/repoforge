# Managed CodeGraph Provider Design

## Status

Architecture direction approved for issue #38. This written specification is awaiting final review before the implementation plan and production changes are started.

## Context and source reconciliation

Issue #38 asks for an optional managed graph provider and semantic canaries under parent #35. Its declared blockers, #36 and #37, are closed; RepoForge reports both references as stale, so the work is dependency-ready even though the issue title and body still say blocked.

The design reconciles four authorities:

- the exact current implementation at `c820574`, especially `CodeIntelligenceResult`, `FallbackCodeIntelligenceProvider`, provider manifests, process containment, egress, and workspace lifecycle;
- roadmap section 15, which requires a RepoForge-owned structured sidecar, no raw public project path, no shared daemon or watcher, no adoption of user CodeGraph state, Git-invisible managed indexes, integrity locking, canary-gated upgrades, and verification widening on uncertainty;
- the issue report supplied by the repository owner;
- the pinned upstream CodeGraph 1.5.0 CLI contract: one-shot `init`, `sync`, `status --json`, `query --json`, `callers --json`, `callees --json`, `impact --json`, and `affected --json`.

The GitHub issue body instructs implementers to read all specification comments. RepoForge_V2 currently reports its comments capability as incomplete and truncated without a continuation cursor, and repository policy does not allow the `gh` runner. Therefore this design treats the owner-provided report, roadmap, current code, dependency tickets, and upstream primary documentation as the available specification record. Implementation must stop and reconcile before proceeding if a later comment read reveals a conflicting requirement.

## Problem

The built-in syntax and tree-sitter providers provide bounded static symbols, imports, references, and affected-test candidates. They do not provide a deep call graph, callers and callees, cross-file impact radius, or semantic upgrade canaries.

Simply placing CodeGraph above the existing fallback ladder is incorrect. `FallbackCodeIntelligenceProvider` returns any non-unavailable, non-truncated primary result without merging the fallback. A partial CodeGraph result would therefore discard useful baseline facts. Writing CodeGraph state into the worktree is also incorrect: `denied_paths` controls RepoForge reads and writes, not Git visibility, and CodeGraph initialization creates files under its project directory.

## Goals

1. Add optional semantic graph evidence without changing disabled baseline behavior.
2. Preserve all useful baseline evidence when CodeGraph is partial, unavailable, stale, malformed, or bounded.
3. Keep every CodeGraph source projection and index outside the worktree and under RepoForge lifecycle control.
4. Reuse existing provider integrity, subprocess containment, egress, snapshot, and cleanup machinery.
5. Normalize graph evidence into a provider-neutral, bounded, deterministic contract.
6. Gate first enrollment and every binary or schema upgrade with semantic canaries.
7. Make graph uncertainty widen verification; it can never authorize an action or reduce a required gate.

## Non-goals

- Exposing CodeGraph MCP tools or provider instructions to an agent.
- Running `serve --mcp`, a shared daemon, native watcher, installer, telemetry command, upgrade command, or self-download.
- Reading or adopting a user's `.codegraph` directory, CodeGraph config, agent configuration, credentials, or home-directory state.
- Replacing the tree-sitter and syntax baseline.
- Returning raw source snippets in phase 1.
- Supporting arbitrary unpinned binaries from `PATH`.
- Making graph evidence an authorization or policy input.

## Architecture

The provider composition is augmentation, not fallback:

    disabled: Fallback(TreeSitter, Syntax)

    enabled: CodeGraphAugmentedProvider(
                 base=Fallback(TreeSitter, Syntax),
                 graph=ManagedCodeGraphProvider)

`CodeGraphAugmentedProvider.analyze()` always evaluates the base provider first. It then attempts semantic enrichment for the same immutable `CodeIntelligenceSnapshot`. Baseline symbols, imports, references, affected tests, status, coverage, and confidence are never replaced by a weaker graph result.

When graph evidence is current, the result carries an additive semantic graph section. When it is partial or unavailable, the result keeps the baseline facts and carries typed graph status and limitations so risk and verification routing can widen. When the feature is disabled, provider construction and serialized results remain byte-for-byte equivalent to the current baseline.

## Provider-neutral contract

Add the following immutable bounded domain types:

- `CodeRelationshipKind`: normalized values `calls`, `imports`, `extends`, `implements`, `references`, `instantiates`, `overrides`, and `routes_to`. Unknown upstream kinds are rejected into a limitation, not passed through.
- `CodeRelationshipFact`: relationship kind, source path, source qualified symbol, target path when resolved, target qualified symbol, traversal depth, and confidence.
- `AffectedPathCandidate`: repository-relative path, reason, confidence, and optional depth.
- `SemanticGraphEvidence`: provider id and version, status, coverage, confidence, relationships, affected paths, limitations, and truncation.

`CodeIntelligenceResult` gains one optional field, `semantic_graph: SemanticGraphEvidence | None = None`. Existing providers use the default and require no behavior change. Serialization is additive. New fields obey the existing path, text, fact-count, limitation-count, sorting, uniqueness, and immutable-tuple rules.

Graph success does not overwrite the overall baseline status. Graph partial or unavailable status is consumed explicitly by assessment and verification recommendation logic. That logic must record a widening reason and must never use missing graph edges as evidence that a test or gate is unnecessary.

## Configuration model

Provider identity and operational options must not become two competing identity planes.

The existing `[[providers]]` manifest remains authoritative for:

- `provider_id`;
- kind and version;
- absolute executable path;
- mandatory executable SHA-256 digest;
- supported languages and capabilities;
- network and filesystem declaration;
- stdout, stderr, and artifact bounds.

A provider may contain one nested `[providers.codegraph]` operational table. It is valid only for an executable analyzer manifest declaring the `semantic_graph` capability. The parsed application model keeps the manifest and CodeGraph options together as one enrollment. The manifest hash, CodeGraph options digest, adapter schema version, platform identity, and canary corpus digest all participate in promotion identity.

Operational fields and defaults:

- `init_timeout_seconds = 120`, range 1 through 600;
- `sync_timeout_seconds = 60`, range 1 through 300;
- `query_timeout_seconds = 15`, range 1 through 120;
- `max_changed_paths = 256`, range 1 through 2000;
- `max_relationships = 1000`, range 1 through the domain fact bound;
- `max_affected_paths = 1000`, range 1 through the domain path bound;
- `max_depth = 5`, range 1 through 10;
- `projection_max_files = 20000`, bounded by repository resource policy;
- `projection_max_bytes = 250000000`, bounded by repository resource policy;
- `canary_timeout_seconds = 180`, range 1 through 600.

Per-repository opt-in is `code_intelligence_provider_id = ""` by default. A non-empty value must resolve to one enrolled provider with the `semantic_graph` capability. Missing, ambiguous, incompatible, disabled, or invalid enrollment fails graph activation closed while leaving the baseline constructible.

`rf init`, config examples, `rf doctor`, and show-config output must cover the new fields. Show-config reveals only reviewed secret-free configuration and digests, never environment values or provider output.

## Managed projection and index

The provider never points CodeGraph at `workspace_root`. Instead it builds one stable, provider-owned projection per workspace under:

    <state_root>/providers/codegraph/workspaces/<workspace_id>/source

CodeGraph receives that projection as its project path. `CODEGRAPH_DIR` is a fixed safe directory name such as `.index`; because the entire projection is outside the Git worktree, CodeGraph-created database, lock, config, and ignore files are not Git-visible. Snapshot identity lives in the RepoForge completion manifest, not in the directory name, so a compatible prior index can be updated incrementally.

The projection builder uses the same repository path policy as RepoForge workspace reads. It includes only normalized allowed regular files from the exact snapshot, never follows symlinks, and excludes denied paths, `.git`, managed state roots, sockets, device files, and the CodeGraph index directory. Omitted symlinks, oversized files, unsupported files, and budget truncation become explicit graph limitations.

Each projection has a RepoForge-owned manifest containing snapshot id, policy hash, ordered path-to-content-digest entries, total files and bytes, adapter schema version, and completion state. The manifest contains no file contents. A projection is usable only when its completion marker and digest match the request snapshot.

Projection updates are deterministic:

1. Compare the requested snapshot with the last complete manifest for the workspace.
2. Atomically mark the projection incomplete before changing source or index state.
3. Materialize changed and added allowed files through temporary sibling files and atomic renames.
4. Remove deleted or newly denied files.
5. Run CodeGraph `init` for a new index or `sync` for a compatible complete index.
6. Validate `status --json` and only then atomically publish the new completion manifest.

A failed update leaves the projection explicitly unusable for every snapshot. The next attempt performs a bounded full rebuild rather than trusting a possibly partial database. No previous index is ever relabeled as current for a different snapshot.

## Subprocess boundary

All commands use the pinned absolute executable from the provider manifest and the existing reviewed subprocess executor, process-tree controller, timeout, cancellation, output bounding, and OS reaper. No shell is used.

The environment is minimal and managed:

- `CODEGRAPH_DIR=.index`;
- `CODEGRAPH_NO_DAEMON=1`;
- `CODEGRAPH_NO_DOWNLOAD=1`;
- `CODEGRAPH_NO_UPDATE_CHECK=1`;
- `CODEGRAPH_TELEMETRY=0`;
- `DO_NOT_TRACK=1`;
- managed `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, and `XDG_DATA_HOME` below the provider state directory;
- deterministic locale, no-color, and non-interactive flags where supported.

RepoForge never invokes `install`, `uninstall`, `serve`, `daemon`, `telemetry`, `upgrade`, or `unlock`. It does not inherit a user CodeGraph configuration. Runtime network policy is `none`; any observed network attempt is a provider failure.

Before promotion, the registry verifies the executable digest and runs `codegraph version`. Parsed version must equal the manifest pin exactly. Version mismatch, digest mismatch, missing executable, self-download request, or unexpected runtime write outside managed roots makes graph evidence unavailable.

Command stdout is read under the manifest bounds and the stricter per-query result limit. JSON is parsed with duplicate-key rejection, schema validation, depth and collection limits, normalized repository-relative paths, and a no-trailing-data rule. Raw provider stdout, stderr, progress text, stack traces, and instructions never enter MCP output.

## Query strategy

Phase 1 uses only structured CLI surfaces:

1. `status <projection> --json` to prove a complete usable index.
2. `affected <changed paths> --depth <n> --json` for affected tests and files.
3. Bounded `query --json` seeded from baseline symbols in requested or changed paths, avoiding repository-wide free-text discovery.
4. Bounded `callers --json`, `callees --json`, and `impact --depth <n> --json` for only the selected symbols.

Paths passed to `affected` are bare normalized projection-relative paths, never absolute paths or `./`-prefixed paths. Symbol selection, fan-out, result count, depth, aggregate stdout bytes, and total wall time are all bounded. Reaching any bound produces `partial` graph evidence with an explicit limitation.

`explore` and `node` are excluded because their primary CLI form is dense text and source context rather than the normalized JSON contract. If a later pinned release provides a stable bounded JSON schema for them, adoption requires a new adapter schema and canary promotion.

## Merge semantics

The augmenter applies these deterministic rules:

1. Run the base provider and retain its result.
2. If graph activation is disabled, return the base object unchanged.
3. If the promotion receipt is absent or invalid, return baseline facts plus unavailable semantic graph evidence and a verification-widening reason.
4. If projection, index, status, or query fails, return baseline facts plus partial or unavailable semantic graph evidence according to whether any validated graph facts exist.
5. Deduplicate normalized relationships and affected paths by their full immutable identity and sort them deterministically.
6. Never lower baseline coverage or confidence because the optional provider failed.
7. Never remove an existing affected-test candidate. Graph candidates are unioned only after path-policy validation and diagnostic selector mapping.
8. Conflicting graph facts are surfaced as limitations; they are not silently chosen over baseline facts.

## Egress and privacy

Phase 1 returns no source snippets. It returns only normalized paths, symbol identities, relationship kinds, measures, reasons, and affected-test mappings. All normalized output still passes the existing central egress scanner before serialization.

Provider logs are bounded, redacted, stored only through existing audit or runtime evidence paths, and never written to MCP stdout. Projection files stay under provider state retention and are never copied to audit logs. No credentials, environment secrets, user home files, ignored repository secrets, or denied paths are projected.

## Concurrency, cancellation, and cleanup

There is no persistent CodeGraph child process and therefore no idle-process timer or provider-owned PID registry.

Per-workspace graph operations are serialized using RepoForge's existing workspace/provider locking boundary. The reviewed executor starts every command in a contained process group. Timeout or cancellation terminates that group and the existing OS reaper handles descendants; RepoForge never scans for or kills unrelated CodeGraph processes.

Workspace disposal removes the provider workspace state after contained commands have stopped. Startup cleanup removes incomplete temporary projections and state for workspaces no longer present, subject to existing retention and bounded cleanup rules. It never touches the user's worktree `.codegraph` directory.

## Semantic canary promotion

A graph provider is usable only with a valid promotion receipt keyed by:

- executable digest and exact version;
- platform and architecture;
- provider manifest hash;
- CodeGraph options digest;
- adapter schema version;
- canary corpus digest.

The canary corpus is a small managed multilingual repository projection containing Python and TypeScript call chains, duplicate symbol names, cross-file imports, inheritance, one unsupported file, affected tests, and a deletion/update scenario.

Promotion fails closed if any gate fails:

- a required call, import, inheritance, or impact edge is missing;
- a forbidden extra edge appears between duplicate names;
- affected-test recall misses a required test or includes a forbidden unrelated test;
- two clean runs produce different canonical normalized JSON;
- upstream JSON has schema drift, duplicate keys, unknown required kinds, invalid paths, or unbounded nesting;
- unsupported-language handling changes from explicit omission/limitation to false current coverage;
- an incremental deletion leaves a stale node or edge;
- timeout, cancellation, or forced failure leaves a child process, lock, or usable incomplete index;
- a denied canary file appears in the projection or any normalized result;
- the source worktree Git status changes.

Receipts contain only identities, bounded metrics, gate outcomes, and timestamps. They contain no source, provider instructions, or raw output. `rf doctor` reports receipt validity and can prewarm promotion. First use may also create a receipt under the canary timeout; until it succeeds, the graph provider remains unavailable and baseline service continues.

Upgrade means any change to binary digest, version, platform, adapter schema, options affecting semantics, or corpus. An old receipt cannot promote a new identity.

## Calibration and verification routing

`calibration-v1.json` gains a `codegraph` entry backed by the canary corpus and affected-test recall measurements. Calibration is advisory evidence, not permission.

Assessment routing consumes semantic graph status as follows:

- current, sufficiently covered graph evidence may add affected tests and recommend narrower diagnostic selectors already permitted by repository configuration;
- partial, truncated, stale, conflicting, or unavailable graph evidence adds a typed widening reason;
- no graph result may remove safety-bundle tests, lower a verification intent, satisfy a release gate, or authorize mutation or publication.

## Module boundaries

Production modules remain focused and below repository size limits:

- `adapters/codegraph/config.py`: typed operational options and enrollment validation;
- `adapters/codegraph/projection.py`: policy-filtered snapshot materialization and manifests;
- `adapters/codegraph/command.py`: reviewed argv, environment, execution, and byte/time budgets;
- `adapters/codegraph/normalize.py`: strict JSON schemas and provider-neutral normalization;
- `adapters/codegraph/provider.py`: managed graph analysis;
- `adapters/codegraph/augment.py`: baseline-preserving merge semantics;
- `adapters/codegraph/canaries.py`: corpus runner, gates, and receipt identity;
- `adapters/codegraph/lifecycle.py`: state disposal and bounded startup cleanup hooks.

Existing process-tree, reaper, provider registry, egress, path policy, workspace lifecycle, and snapshot modules are reused rather than copied.

## Implementation slices

1. Add domain contract defaults and serialization tests with no baseline behavior change.
2. Add provider enrollment and operational config validation, examples, doctor, show-config, and init coverage.
3. Build the policy-filtered projection with denial, symlink, budget, atomicity, and Git-status tests.
4. Build the fake-binary command boundary test-first: version, digest, environment, timeout, cancellation, output, corruption, and descendant cleanup.
5. Add strict normalizers and provider behavior for status, affected, query, callers, callees, and impact fixtures.
6. Add augmentation and bootstrap gating; prove baseline retention for every graph failure class.
7. Add canary corpus, receipts, calibration, upgrade invalidation, and lifecycle cleanup.
8. Update operator documentation and changelog; there is no new MCP tool, but the additive result/config surface must be documented.

Every behavior slice follows RED, GREEN, REFACTOR. Tests use a deterministic fake CodeGraph executable for unit and service coverage. A separately marked integration test may exercise the pinned real binary when the artifact is available; the standard suite must not download it.

## Verification gates

Required evidence before merge:

- focused unit and service tests for each slice;
- affected-selector tests and `make test` during development;
- formatting, lint, strict type checking, configuration drift, tool-schema, and build checks;
- fake-binary fault matrix for timeout, cancellation, malformed JSON, oversize output, partial index, stale lock, version mismatch, and digest mismatch;
- real-binary semantic canaries for the pinned artifact on supported release platforms;
- clean Git status in the source worktree after index, query, cancel, restart, and disposal;
- final `./scripts/test-all.sh` and release-candidate verification because this change affects bootstrap, subprocess lifecycle, configuration, public structured evidence, and release integrity.

## Observability

Emit bounded structured metrics for promotion, projection, init/sync, query groups, normalization, and cleanup: duration, outcome class, input and output counts, bytes, truncation, cache hit, snapshot id, provider identity hash, and widening reason. Never emit source content, raw provider output, absolute workspace paths, credentials, or environment values.

## Rollback

Operational rollback is immediate because repository opt-in defaults empty. Removing the repository provider id returns construction to the exact baseline ladder. Invalid or missing provider state also falls back to baseline behavior.

Code rollback does not require migrating worktree state because all provider data is external and disposable. Managed state may be removed through lifecycle cleanup after contained processes stop. Release deployment remains staged, health-windowed, and receipt-backed; rollback uses the existing RepoForge activation receipt.

## Completion criteria

Issue #38 is complete only when:

1. disabled configuration is byte-for-byte baseline-compatible;
2. enabled graph failures retain all baseline evidence and widen verification explicitly;
3. executable digest and exact version mismatch fail graph activation closed;
4. every CodeGraph process is bounded, cancellable, and reaped through existing containment;
5. denied files and user CodeGraph state are never indexed;
6. no CodeGraph state changes worktree Git status;
7. normalized graph evidence is bounded, deterministic, snapshot-bound, and egress-scanned;
8. semantic canaries detect missing and extra edges, nondeterminism, schema drift, unsupported-language regression, incremental deletion, and lifecycle leaks;
9. upgrade identity invalidates the old promotion receipt;
10. config, doctor, show-config, init, docs, tests, changelog, and release gates agree.

## Primary upstream references

- [CodeGraph CLI](https://colbymchenry.github.io/codegraph/reference/cli/)
- [CodeGraph configuration and index location](https://colbymchenry.github.io/codegraph/getting-started/configuration/)
- [CodeGraph affected tests](https://colbymchenry.github.io/codegraph/guides/affected-tests/)
- [CodeGraph releases](https://github.com/colbymchenry/codegraph/releases)
