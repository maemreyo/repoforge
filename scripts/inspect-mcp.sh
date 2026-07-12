#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${REPOFORGE_CONFIG:-$HOME/.config/repoforge/config.toml}"
RF="${REPOFORGE_BIN:-$ROOT/.venv/bin/repoforge}"

if ! command -v npx >/dev/null 2>&1; then
  echo "npx is required for MCP Inspector." >&2
  exit 1
fi

exec npx -y @modelcontextprotocol/inspector@latest -- \
  "$RF" --config "$CONFIG" serve