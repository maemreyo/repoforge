#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${REPOFORGE_CONFIG:-$HOME/.config/repoforge/config.toml}"
REPO_PATH="${REPO_PATH:-${REPOFORGE_REPO_PATH:-}}"
REPO_ID="${REPO_ID:-${REPOFORGE_REPO_ID:-}}"

if [[ -z "$REPO_PATH" || -z "$REPO_ID" ]]; then
  echo "Set REPO_PATH and REPO_ID (or REPOFORGE_REPO_PATH and REPOFORGE_REPO_ID)." >&2
  exit 2
fi

for command in git; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done

if command -v uv >/dev/null 2>&1; then
  cd "$ROOT"
  uv sync --extra dev
  RF="$ROOT/.venv/bin/rf"
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
  RF="$ROOT/.venv/bin/rf"
fi

if [[ ! -f "$CONFIG" ]]; then
  cat >&2 <<OUT
RepoForge is installed, but no configuration exists at:
  $CONFIG

Create a local-first configuration with:
  "$RF" --config "$CONFIG" setup --local "$REPO_PATH"

Review the proposal output, then rerun with the exact approval token it prints.
OUT
  exit 3
fi

"$RF" --config "$CONFIG" doctor
"$RF" --config "$CONFIG" repo list

cat <<OUT

RepoForge installed and configuration checked.

Config: $CONFIG
Repository: $REPO_ID ($REPO_PATH)
Command: $RF

Inspect paths:
  "$RF" --config "$CONFIG" config path

Local stdio command:
  "$RF" --config "$CONFIG" serve

Managed tunnel startup additionally requires a tunnel ID, tunnel-client, and CONTROL_PLANE_API_KEY.
OUT
