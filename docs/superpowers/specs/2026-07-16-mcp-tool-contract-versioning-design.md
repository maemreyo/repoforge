# MCP Tool Contract Versioning and Verification Consolidation Design

## Scope

This design implements issues #77 and #141 as one dependency-ordered change. It introduces an explicit MCP tool-contract registry and uses that registry to retire `workspace_verify` from the current tool surface while preserving a bounded legacy alias contract.

## Goals

- Give every public MCP tool surface an integer contract version.
- Permit only additive changes within a version; removals or incompatible schema changes require a new version.
- Record aliases, deprecation notices, introduction versions, removal versions, and annotation/schema fingerprints in reviewed release-contract data.
- Select a contract deterministically from connection-scoped client capabilities, with safe behavior for legacy, missing, malformed, and unknown requests.
- Ensure aliases route through the exact canonical service path and retain the canonical tool's mutability annotations.
- Make `workspace_run_profile` the sole verification execution path, including repository-default verification when `profile_name` is omitted.
- Remove `workspace_verify` from the current contract while retaining it in contract v1 during the compatibility window.
- Preserve the exact-tree verification receipt and commit gate.

## Non-goals

- No arbitrary client-provided tool definitions, schemas, commands, or aliases.
- No authorization decision may depend on a claimed contract version or compatibility flag.
- No change to repository path policy, command allowlists, change budgets, publication policy, or protected branches.
- No automatic merging or force-pushing.

## Contract model

`ToolContractRegistry` is a pure domain model. Contract version 1 contains the legacy alias `workspace_verify`; contract version 2 is current and exposes only `workspace_run_profile`. Alias metadata states:

- alias: `workspace_verify`
- canonical tool: `workspace_run_profile`
- deprecated in: v1
- removed in: v2
- notice: callers must migrate to `workspace_run_profile`

The registry validates that:

1. versions are positive and contiguous;
2. the current version exists;
3. aliases point to a canonical tool present in the alias version;
4. alias and canonical annotations are identical;
5. an alias cannot disappear before its declared removal version;
6. a removal requires at least one earlier reviewed version containing the deprecation notice;
7. tool schemas and annotations are hashed deterministically for drift evidence.

## Client selection

RepoForge reads the existing connection-scoped `ClientCapabilities` model. A bounded compatibility flag of the form `repoforge-tool-contract-vN` requests version `N`.

- no explicit request from a normal client: current version;
- legacy or missing initialization: oldest supported version;
- supported explicit request: requested version;
- malformed or unknown explicit request: current version with an explicit fallback reason;
- multiple conflicting requests: current version with an explicit fallback reason.

This selection controls discovery and invocation compatibility only. It never grants repository, filesystem, command, or publication authority.

## MCP adapter

A small `ContractAwareFastMCP` subclass filters `list_tools` by the selected contract and rejects calls to names not available in that connection's selected contract. Offline release-contract generation passes an explicit version override, so golden snapshots do not depend on a live request context.

`workspace_verify` remains internally registered as a deprecated compatibility alias. It has the same mutability annotations as `workspace_run_profile`, an explicit deprecation description, and delegates directly to the canonical service method. Contract v2 neither advertises nor accepts it; contract v1 advertises and accepts it.

## Verification execution path

`WorkspaceRunProfileCommand.profile_name` becomes optional. When omitted, the runner selects the repository-default verification profile and reports `used_default=true`. Explicit profile names retain existing setup/build/quick/full behavior. The result also returns `repo_id`, so the former alias and canonical call have identical structured results.

The standalone `WorkspaceVerifier` use case is removed. `CodingService.workspace_verify` remains only as a compatibility facade that calls `CodingService.workspace_run_profile` with the same arguments. The MCP alias therefore cannot bypass policy, verification receipt creation, locking, audit behavior, or the exact-tree commit gate.

## Release contracts and documentation

The release contract advances to v2 and records:

- current and supported tool-contract versions;
- contract-selection flag syntax and fallback behavior;
- per-version tool snapshots and hashes;
- alias/deprecation/removal metadata;
- schema and annotation fingerprints.

The prior v1 golden remains reviewed compatibility evidence. The checker validates the current golden and registry invariants instead of silently regenerating contracts. `TOOL_REFERENCE.md`, server instructions, and plugin golden prompts name `workspace_run_profile` as the final verification entry point and describe the legacy alias window.

## Test strategy

- Pure registry tests: additive change, alias routing metadata, annotation drift, removal gate, unknown version, conflicting request, legacy fallback.
- Service tests: omitted profile selects default verification, explicit quick profile remains non-default, legacy facade equals canonical result, exact-tree commit gate remains unchanged.
- MCP tests: current contract omits/rejects alias, v1 exposes alias with deprecation notice, both names return identical results in v1, current tool count and annotations agree.
- Release tests: deterministic versioned snapshots, checked golden, docs and golden prompts agree.
- Final gates: focused tests, `quick`, `test`, then repository `full` verification before commit.
