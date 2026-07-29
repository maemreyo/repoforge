#!/bin/sh
# Developer- and model-facing verification for a change in progress.
#
# This is deliberately NOT the release gate. `scripts/verify-production.sh` remains the
# authority for a release, and production-gate.yml already runs coverage, the wheel build
# and the installed-wheel smoke on every push to a protected branch. Repeating those on a
# contended laptop for every change bought nothing and cost about three times the wall
# clock: the same suite is 8:08 through the xdist lane and over thirty minutes with
# coverage under the old partitioned runner.
#
# What this keeps is everything that can fail because of the change itself: contracts,
# corpora and control-plane fault gates, formatting, lint, strict types, and the whole
# test suite. What it leaves to the release gate is the branch-coverage floor, the source
# and wheel builds, and the isolated installed-wheel behaviour.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
HEAD_SHA=$(git rev-parse HEAD)
echo "RepoForge change verification HEAD: $HEAD_SHA"

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/repoforge-change-gate.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT INT TERM
export PYTHONDONTWRITEBYTECODE=1
export RUFF_CACHE_DIR="$TMP_ROOT/ruff-cache"

echo "[change] synchronize frozen dependencies"
uv sync --extra dev --frozen
echo "[change] validate release contract"
uv run python scripts/check_release_contracts.py
echo "[change] run Forge v2 release corpora"
make v2-gates
echo "[change] check formatting, lint, and types"
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy --strict --cache-dir "$TMP_ROOT/mypy-cache" src/repoforge
echo "[change] run the full pytest suite in lanes"
uv run --extra dev python scripts/select_affected_tests.py --full --run

echo "[change] validate diff cleanliness"
git diff --check
echo "change verification passed for $HEAD_SHA"
