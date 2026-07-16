#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/repoforge-agent-gate.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT INT TERM
export PYTHONDONTWRITEBYTECODE=1
export RUFF_CACHE_DIR="$TMP_ROOT/ruff-cache"

uv sync --extra dev --frozen
uv run python scripts/check_release_contracts.py

(
  uv run ruff format --check src tests scripts
  uv run ruff check src tests scripts
  uv run mypy --strict --cache-dir "$TMP_ROOT/mypy-cache" src/repoforge
) &
STATIC_PID=$!
(
  BASE_REF=${REPOFORGE_BASE_REF:-origin/main}
  TEST_FILES=$(
    {
      git diff --name-only --diff-filter=AM "$BASE_REF"...HEAD -- 'tests/test_*.py'
      git diff --name-only --diff-filter=AM HEAD -- 'tests/test_*.py'
    } | sort -u
  )
  if [ -n "$TEST_FILES" ]; then
    # Test paths are repository-controlled and cannot contain whitespace.
    uv run pytest -q $TEST_FILES
  else
    uv run pytest -q tests/test_mcp_contract.py
  fi
) &
TEST_PID=$!

VERIFY_STATUS=0
wait "$STATIC_PID" || VERIFY_STATUS=$?
wait "$TEST_PID" || VERIFY_STATUS=$?
if [ "$VERIFY_STATUS" -ne 0 ]; then
  exit "$VERIFY_STATUS"
fi

git diff --check
echo "agent verification passed for $(git rev-parse HEAD)"
