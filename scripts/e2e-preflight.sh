#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG="${REPOFORGE_CONFIG:-$HOME/.config/repoforge/config.toml}"
REPO_ID="${REPOFORGE_E2E_REPO_ID:-${REPO_ID:-}}"
SKIP_SOURCE_GATE=0

usage() {
  cat <<'EOF'
Usage: scripts/e2e-preflight.sh [options]

Safe preflight for the RepoForge full-flow runbook. It never edits target
files, pushes a branch, or creates a pull request.

Options:
  --config PATH       RepoForge config path
  --repo-id ID        Configured repository id (required unless REPOFORGE_E2E_REPO_ID is set)
  --skip-source-gate  Skip ./scripts/test-all.sh
  -h, --help          Show this help

Environment alternatives:
  REPOFORGE_CONFIG
  REPOFORGE_E2E_REPO_ID
EOF
}

while (($#)); do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { echo "missing value for --config" >&2; exit 2; }
      CONFIG="$2"
      shift 2
      ;;
    --repo-id)
      [[ $# -ge 2 ]] || { echo "missing value for --repo-id" >&2; exit 2; }
      REPO_ID="$2"
      shift 2
      ;;
    --skip-source-gate)
      SKIP_SOURCE_GATE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$REPO_ID" ]]; then
  echo "Set --repo-id or REPOFORGE_E2E_REPO_ID to a configured repository id." >&2
  exit 2
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "RepoForge config not found: $CONFIG" >&2
  echo "Create it with rf setup --local PATH or rf setup --tunnel-id ID PATH." >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  RF=(uv run rf --config "$CONFIG")
  PYTEST=(uv run pytest)
else
  [[ -x .venv/bin/rf ]] || {
    echo "Neither uv nor .venv/bin/rf is available. Run bootstrap first." >&2
    exit 1
  }
  RF=(.venv/bin/rf --config "$CONFIG")
  PYTEST=(.venv/bin/pytest)
fi

run_step() {
  local title="$1"
  shift
  printf '\n==> %s\n' "$title"
  "$@"
}

printf 'RepoForge E2E preflight\n'
printf '  root:    %s\n' "$ROOT"
printf '  config:  %s\n' "$CONFIG"
printf '  repo id: %s\n' "$REPO_ID"
printf '\nThis script performs no target-file edit, push, or PR creation.\n'

if [[ "$SKIP_SOURCE_GATE" -eq 0 ]]; then
  run_step "L0 source quality gate" ./scripts/test-all.sh
else
  printf '\n==> L0 source quality gate skipped by operator\n'
fi

run_step \
  "L1 MCP contract regression" \
  "${PYTEST[@]}" -q tests/test_mcp_contract.py

run_step \
  "L2 doctor" \
  "${RF[@]}" doctor

run_step \
  "L2 configured repository inventory" \
  "${RF[@]}" repo list

run_step \
  "Show RepoForge paths" \
  "${RF[@]}" config path

run_step \
  "Show resolved config" \
  "${RF[@]}" show-config

cat <<'EOF'

PASS: automated safe preflight completed.

Manual levels still required:
  L3  ./scripts/inspect-mcp.sh
  L4  start Secure MCP Tunnel and run direct/indirect/negative prompts
  L5  controlled canary edit, exact verification, approved commit/push/draft PR
  Cleanup close the canary PR and delete its branch

Follow docs/testing/FULL_FLOW_TESTING.md and record results in docs/testing/TEST_RUN_RECORD.md.
EOF
