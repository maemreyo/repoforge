#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${REPOFORGE_CONFIG:-$HOME/.config/repoforge/config.toml}"
REPO_PATH="${REPOFORGE_REPO_PATH:-/Users/trung.ngo/Documents/zaob-dev/work-frontier}"

for command in git; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done

if command -v uv >/dev/null 2>&1; then
  cd "$ROOT"
  uv sync --extra dev
  RF="$ROOT/.venv/bin/repoforge"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
  "$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY
  "$PYTHON_BIN" -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip
  "$ROOT/.venv/bin/pip" install -e "$ROOT[dev]"
  RF="$ROOT/.venv/bin/repoforge"
fi

if [[ ! -f "$CONFIG" ]]; then
  "$RF" --config "$CONFIG" init --repo "$REPO_PATH" --repo-id work-frontier
fi

cat <<OUT

RepoForge installed.

Config: $CONFIG
Command: $RF

Next:
  gh auth login
  gh auth setup-git
  "$RF" --config "$CONFIG" doctor
  "$RF" --config "$CONFIG" smoke-test --repo-id work-frontier

MCP stdio command:
  "$RF" --config "$CONFIG" serve
OUT
