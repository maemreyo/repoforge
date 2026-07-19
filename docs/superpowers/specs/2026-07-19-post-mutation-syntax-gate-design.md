# Post-Mutation Syntax Gate Design

## Objective

Return bounded, typed Tree-sitter syntax diagnostics in the same `workspace_mutate` response for dry-run and applied mutations. Diagnostics are advisory: mutation readiness and commit semantics remain unchanged.

## Architecture

A focused syntax analyzer consumes the mutation planner's final virtual bytes for changed paths. It uses the repository's pinned Tree-sitter grammars for Python, JavaScript, JSX, TypeScript, and TSX, reports unsupported or undecodable files as `unknown`, caps diagnostics, and never echoes source bodies or absolute paths. The mutation result and idempotency receipt persist the same typed evidence, and the Forge v2 contract projects it without adding a tool.

## Semantics

- `ok`: every non-deleted changed file with a pinned grammar parsed without syntax errors; `parse_ok=true`.
- `error`: at least one syntax error was found; `parse_ok=false`.
- `unknown`: no syntax errors were found but at least one changed file could not be evaluated; `parse_ok=null`.
- Deleted files are ignored.
- Diagnostics are deterministic, capped at 100 entries, and marked `truncated` when evidence is omitted.
- Per-file parsing is budgeted at 100 ms; exceeding the budget yields `unknown` for that path.

## Data Flow

1. The journal planner materializes final virtual state for paths whose content changes.
2. The analyzer parses those bytes before a dry-run response and after the same plan is committed for apply.
3. The result is included in ordinary responses, keyed receipts, and keyed replay.
4. Forge v2 output models expose the evidence with closed enums and payload bounds.

## Error Handling

Analyzer failures never roll back a mutation. Unsupported grammar, invalid UTF-8, parser exception, or budget exhaustion produce typed `unknown` evidence. Existing mutation and idempotency failures retain their current fail-closed behavior.

## Verification

TDD covers valid/malformed Python, unsupported grammar, dry-run, applied break/fix, receipt replay, output contracts, response caps, and latency evidence. Final verification runs the repository's authoritative profile on the exact tree.
