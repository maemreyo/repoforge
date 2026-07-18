# Contributing to RepoForge

## Change shape

Each commit and pull request must address **one independently deployable concern**. Avoid combining
architecture moves, behavior changes, generated artifacts, and unrelated cleanup. Preserve the
dependency direction documented in the production architecture plan and keep `bootstrap.py` as the
composition root.

## Conventional Commits

Use scoped Conventional Commits:

```text
type(scope): summary
```

Typical types are `feat`, `fix`, `refactor`, `test`, `docs`, `ci`, and `build`. Choose a narrow scope
such as `runtime`, `config`, `workspace`, `mcp`, `release`, or `tunnel`. The summary states the behavior
or invariant changed, not a list of files.

Examples:

```text
fix(tunnel): wait for bounded log drainage before reporting exit
ci(release): enforce frozen MCP and configuration contracts
```

## Required verification

From a clean checkout run:

```bash
scripts/verify-production.sh
```

During development, `scripts/verify-production.sh --allow-dirty` runs the same source, contract,
coverage, build, and clean-wheel smoke gates without requiring a committed tree. Never update
`docs/contracts/release-contract-v2.json` or `docs/contracts/tool-schemas-v2.json` merely to make CI
pass; review identity, exact tool roster, descriptions, annotations, input/output schemas, hashes,
and migration impact first. The v1 file is historical rollback evidence, not the current contract.

## Safety invariants

Do not introduce arbitrary shell execution, force push, non-draft PR creation, protected-path writes,
secret persistence, in-place active configuration mutation, or rollback that silently restores an
explicitly revoked repository. Discovered repository commands remain data until explicit approval.
