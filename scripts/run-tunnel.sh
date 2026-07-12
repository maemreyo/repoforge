#!/usr/bin/env bash
set -euo pipefail

: "${TUNNEL_ID:?Set TUNNEL_ID, for example tunnel_...}"
: "${CONTROL_PLANE_API_KEY:?Set CONTROL_PLANE_API_KEY to a runtime tunnel key}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${TUNNEL_PROFILE:-repoforge}"
CONFIG="${REPOFORGE_CONFIG:-$HOME/.config/repoforge/config.toml}"
RF="${REPOFORGE_BIN:-$ROOT/.venv/bin/repoforge}"
MCP_COMMAND="$RF --config $CONFIG serve"

if ! command -v tunnel-client >/dev/null 2>&1; then
  echo "tunnel-client is not in PATH. Download it from OpenAI Platform tunnel settings." >&2
  exit 1
fi

"$RF" --config "$CONFIG" doctor

tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile "$PROFILE" \
  --tunnel-id "$TUNNEL_ID" \
  --mcp-command "$MCP_COMMAND"

tunnel-client doctor --profile "$PROFILE" --explain
tunnel-client run --profile "$PROFILE"
