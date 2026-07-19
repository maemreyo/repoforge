# Round-4 Contract and Process Hardening Design

## Objective

Close the actionable Round-4 findings on PR #225 without expanding the locked 28-tool Forge v2 surface. The change must make errors and recovery actions truthful at discovery, make timeout cleanup safer across supported Unix hosts, publish practical verification-input bounds, and replace non-isolated regression coverage.

This design does not implement the separately deferred Epic #180 work for production-backed benchmark corpora, graph-independent issue drift, the real rollback drill, or live GitHub issue reconciliation.

## Public contract

### Tool output schemas

Every tool advertises one output schema whose accepted values are:

1. that tool's existing successful output model; or
2. one shared `ToolFailure` model containing `status = "failed"`, `summary`, and the existing typed `ToolError`.

The generated JSON Schema must expose this as a real union. Success models retain their current fields and validation. The MCP server validates successful structured content against the success model and failed structured content against `ToolFailure`; both therefore conform to the advertised union.

The runtime representation must avoid recursive inheritance between success models and the failure union. The registry owns composition of the advertised schema/model boundary, while application success models remain independently usable.

### Recovery actions

Recovery evidence becomes a discriminated structure:

```json
{
  "kind": "workspace_mutate",
  "precondition": "...",
  "arguments": {
    "workspace_id": "...",
    "operations": [{"op": "restore", "paths": ["src/a.py"]}],
    "expected_head_sha": "...",
    "expected_workspace_fingerprint": "..."
  }
}
```

`arguments` is the exact public input payload for `kind`; clients do not rename fields or synthesize nested objects. Domain construction validates the typed action invariants, and contract-level regression coverage calls `V2_TOOL_SPECS[kind].validate_input(arguments)` directly for every emitted failure class.

Fresh-plan behavior remains mandatory: stale, timed-out, and cancelled execution plans must not recommend re-executing the implicated plan ID.

## Process cleanup

Timeout cleanup uses a bounded process-identity snapshot, not bare PIDs. A process identity contains at least PID and an OS-observed start token. Linux opens an atomic pidfd, then re-reads identity before signalling through that handle; this closes the check-to-signal PID-reuse race. When an atomic process handle is unavailable, RepoForge skips the direct descendant signal and records the incomplete cleanup instead of falling back to a racy `kill(pid)` call.

Linux uses bounded `/proc` inspection so it does not depend on an external `ps` binary. macOS uses one bounded `ps` query and parses PID, PPID, and start identity. Unsupported or failed inspection remains fail-safe for the caller: the original process group and direct process are still terminated within existing timeouts, while descendant-inspection failure is explicit in internal diagnostics rather than causing an unbounded wait. macOS currently has no standard-library atomic process handle, so its bounded `ps` evidence is diagnostic and direct out-of-group descendant signalling fails closed.

The snapshot walks descendants before group termination and repeats a bounded discovery/sweep after termination to catch descendants that remain attributable. A daemon already reparented before the first snapshot cannot safely be attributed without cgroups/subreaper ownership; this limitation is documented rather than hidden. RepoForge must never signal a PID after identity reuse.

No new dependency or privileged OS feature is introduced.

## Verification input bounds

`selector` and `selector2` accept either:

- one non-empty string of at most 4,096 characters; or
- at most 100 such strings.

`argv` remains at most 100 items with the same per-item bound. Type aliases place array constraints on the array branch so generated discovery schemas contain `maxItems: 100`, not an inapplicable outer `maxLength`.

## Tests

Implementation follows red-green-refactor at these seams:

1. Advertised output schema accepts both one representative success and the shared failure for all 28 tools; tool-specific success models still reject missing success fields.
2. Every produced recovery action's `arguments` validates directly, with no test-only translator.
3. Stale-plan classifications never expose the failed plan ID as an execute recommendation.
4. Process cleanup tests record exact child identity through a temporary fixture; no global `pgrep` matching is allowed.
5. PID-reuse simulation proves a changed start token is never signalled.
6. Schema tests assert `maxItems: 100` and per-item `maxLength: 4096` for both selector branches and argv.
7. MCP protocol tests exercise structured success and structured error through a real client session.

After focused tests, run Ruff, configured Mypy, the complete pytest suite, schema/release-contract checks, v2 gates, and the production verification script.

## Documentation and compatibility

Update `docs/development/TOOL_REFERENCE.md` for the unified error union, exact recovery arguments, annotation changes, and verification bounds. Record the required direct, indirect, and negative metadata prompt checks using the repository test-run template.

The tool roster, names, input operations, connector identities, and successful payload shapes do not change. Generated contract hashes change intentionally and are reviewed with their goldens.

## Non-goals

- Adding cgroups, subreaper ownership, or a process-supervisor dependency.
- Implementing the deferred #182, #187, #194, or #195 work.
- Renaming tools or increasing the 28-tool surface.
- Rewriting unrelated failure-intelligence classification rules.
