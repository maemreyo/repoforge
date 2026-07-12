# Development guide

## Setup

```bash
uv sync --extra dev
```

`uv.lock` được commit để cài đúng dependency graph. Dùng `uv run --locked ...` trong môi trường
release/CI khi muốn fail thay vì tự cập nhật lockfile.

## Quality commands

```bash
make lint
make typecheck
make test
make build
make check
```

Tương đương:

```bash
uv run ruff check .
uv run mypy src/repoforge
uv run pytest --cov=repoforge --cov-report=term-missing
uv build
```

## Local MCP debugging

```bash
./scripts/inspect-mcp.sh
```

Hoặc chạy server stdio:

```bash
REPOFORGE_CONFIG=~/.config/repoforge/config.toml uv run rf serve
```

Không log ra stdout trong stdio mode.

## Source layout

```text
src/repoforge/config.py     TOML model + validation
src/repoforge/discovery.py  repo auto-detection and config rendering
src/repoforge/security.py   branch/path/patch policies
src/repoforge/runner.py     shell-free subprocess runner
src/repoforge/state.py      workspace registry, verification receipts, locks
src/repoforge/service.py    Git/gh/worktree operations
src/repoforge/server.py     MCP metadata and tool registration
src/repoforge/cli.py        init/doctor/smoke/tunnel DX
```

## Adding a tool

1. Keep it to one read or one write responsibility.
2. Add a typed service method; never accept an arbitrary command string.
3. Register with accurate read-only, destructive and open-world annotations.
4. Return stable structured fields and reusable IDs.
5. Add positive, negative and protocol-level tests.
6. Update `docs/TOOL_REFERENCE.md` and the golden prompt set.
