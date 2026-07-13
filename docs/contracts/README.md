# RepoForge release contracts

`release-contract-v1.json` is the reviewed, machine-readable public compatibility boundary. It freezes:

- package, source-config, resolved-config, runtime-control, and diagnostics schema versions;
- MCP tool names, titles, descriptions, annotations, input schemas, and output schemas;
- the MCP tool-surface and server-instruction hashes.

Run `uv run python scripts/check_release_contracts.py` to detect drift. When a change is intentional,
review Plugin compatibility and migration impact first, update the architecture plan, then run:

```bash
uv run python scripts/check_release_contracts.py --write
```

A changed golden file without corresponding documentation and migration reasoning is not an approved
contract change.
