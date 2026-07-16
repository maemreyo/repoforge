#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
HEAD_SHA=$(git rev-parse HEAD)
echo "RepoForge verification HEAD: $HEAD_SHA"
ALLOW_DIRTY=false
if [ "${1:-}" = "--allow-dirty" ]; then
  ALLOW_DIRTY=true
fi

if [ "$ALLOW_DIRTY" = false ]; then
  STATUS=$(git status --porcelain --untracked-files=normal)
  if [ -n "$STATUS" ]; then
    echo "working tree is not clean; commit/stash/remove artifacts or pass --allow-dirty" >&2
    printf '%s\n' "$STATUS" >&2
    exit 1
  fi
fi

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/repoforge-production-gate.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT INT TERM
export PYTHONDONTWRITEBYTECODE=1
export RUFF_CACHE_DIR="$TMP_ROOT/ruff-cache"

echo "[integrity] synchronize frozen dependencies"
uv sync --extra dev --frozen
echo "[integrity] validate release contract"
uv run python scripts/check_release_contracts.py
echo "[integrity] check formatting, lint, and types"
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy --strict --cache-dir "$TMP_ROOT/mypy-cache" src/repoforge
echo "[integrity] run deterministic pytest shards and combine branch coverage"
uv run python scripts/run_test_shards.py --coverage-dir "$TMP_ROOT/coverage-data"

echo "[integrity] build source and wheel distributions"
uv build --out-dir "$TMP_ROOT/dist"
echo "[integrity] verify isolated installed-wheel behavior"
scripts/verify-wheel-install.sh "$TMP_ROOT/dist"

echo "[integrity] validate diff and repository cleanliness"
git diff --check
if [ "$ALLOW_DIRTY" = false ]; then
  STATUS=$(git status --porcelain --untracked-files=normal)
  if [ -n "$STATUS" ]; then
    echo "production verification left repository artifacts behind:" >&2
    printf '%s\n' "$STATUS" >&2
    exit 1
  fi
fi
echo "production verification passed for $HEAD_SHA"
