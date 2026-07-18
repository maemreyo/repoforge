# RepoForge release contracts

`release-contract-v2.json` is the reviewed machine-readable boundary for the active `forge_v2`
connector. It freezes:

- package, source-config, resolved-config, runtime-control, and diagnostics schema versions;
- connector identity, the exact ordered roster of 28 public tools, and the retired `forge_v1` grace
  identity;
- per-tool schema/metadata hashes, server-instruction hash, tool-surface hash, and CLI contract;
- the one-tool `migration_required` grace surface used by stale Forge v1 consumers.

`tool-schemas-v2.json` is generated directly from the authoritative Pydantic registry and contains the
full closed input/output schemas, published bounds, enums, patterns, annotations, and typed error
contracts for all 28 tools. The compact release contract references its digest instead of duplicating
the entire schema bundle.

The historical Forge v1 file is retained only as rollback evidence. It is not the current supported
contract, is not selected through client negotiation, and must not be edited to make current CI pass.

Run the non-mutating drift checks with:

```sh
uv run --extra dev python scripts/generate_tool_schemas.py
uv run --extra dev python scripts/check_release_contracts.py
```

When a public change is intentional, first review connector migration, policy, annotations, payload
compatibility, and rollback impact. Then regenerate through the enrolled command:

```sh
make schemas
```

A changed golden without corresponding tests, documentation, migration reasoning, and a green
production gate is not an approved contract change.
