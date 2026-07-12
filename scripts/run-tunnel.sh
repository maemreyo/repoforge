#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -n "${REPOFORGE_BIN:-}" ]]; then
  RF="$REPOFORGE_BIN"
elif [[ -x "$ROOT/.venv/bin/repoforge" ]]; then
  RF="$ROOT/.venv/bin/repoforge"
elif command -v repoforge >/dev/null 2>&1; then
  RF="$(command -v repoforge)"
elif command -v rf >/dev/null 2>&1; then
  RF="$(command -v rf)"
else
  echo "RepoForge is not installed. Set REPOFORGE_BIN or install it with uv." >&2
  exit 1
fi

exec "$RF" start "$@"
