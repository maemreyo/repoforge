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
# What this keeps is the fast change-facing signal: contracts, metadata, corpora and
# control-plane fault gates, formatting, lint, strict types, and affected tests. The full
# behavioral suite, branch-coverage floor, builds, and installed-wheel behaviour belong
# to the production gate.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
HEAD_SHA=$(git rev-parse HEAD)
echo "RepoForge change verification HEAD: $HEAD_SHA"

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/repoforge-change-gate.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT INT TERM
export PYTHONDONTWRITEBYTECODE=1
export RUFF_CACHE_DIR="$TMP_ROOT/ruff-cache"

# One stage per invocation, so the reviewed profile can declare typed steps
# (`--stage sync`, `--stage typecheck`, ...) and a caller polling the operation sees which
# stage is running rather than one opaque script and an elapsed counter. Every stage still
# lives here and only here: the profile names stages, it does not redefine them, so
# `make verify` and the profile cannot drift into meaning different things.
STAGES="sync release-contract metadata v2-gates format lint typecheck suite diff"

run_stage() {
  case "$1" in
    sync)             uv sync --extra dev --frozen ;;
    release-contract) uv run python scripts/check_release_contracts.py ;;
    metadata)
      uv run python scripts/select_affected_tests.py --check-completeness
      uv run python scripts/select_affected_tests.py --check-map-freshness \
        --base "${REPOFORGE_TEST_AFFECTED_BASE:-main}"
      ;;
    v2-gates)         make v2-gates ;;
    format)           uv run ruff format --check src tests scripts ;;
    lint)             uv run ruff check src tests scripts ;;
    typecheck)        uv run mypy --strict --cache-dir "$TMP_ROOT/mypy-cache" src/repoforge ;;
    suite)            uv run --extra dev python scripts/select_affected_tests.py --run \
                         --base "${REPOFORGE_TEST_AFFECTED_BASE:-main}" ;;
    diff)             git diff --check ;;
    *) echo "unknown stage: $1 (expected one of: $STAGES)" >&2; exit 2 ;;
  esac
}

if [ "${1:-}" = "--stage" ]; then
  [ -n "${2:-}" ] || { echo "--stage requires a stage name (one of: $STAGES)" >&2; exit 2; }
  echo "[change] $2"
  run_stage "$2"
  exit 0
fi

for stage in $STAGES; do
  echo "[change] $stage"
  run_stage "$stage"
done
echo "change verification passed for $HEAD_SHA"
