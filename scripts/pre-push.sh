#!/usr/bin/env bash
# Pre-push hook — prevent pushes that would break the release contract or repo formatting.
# Install via:  make install-hooks
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

remote="$1"
url="$2"

run_check() {
    local label="$1"
    shift
    printf '🔎 %s...\n' "$label"
    if "$@"; then
        printf '%b\n' "${GREEN}✅ ${label} passed${NC}"
        return 0
    fi
    printf '%b\n' "${RED}❌ ${label} failed${NC}"
    return 1
}

format_failed=0
run_check "ruff format --check" uv run ruff format --check src tests || format_failed=1
run_check "ruff check" uv run ruff check src tests || format_failed=1
run_check "mypy --strict" uv run mypy --strict src/repoforge || format_failed=1

if [ "$format_failed" -ne 0 ]; then
    echo ""
    echo -e "${RED}⛔ REJECTED: format/lint/typecheck failed (see above).${NC}"
    echo "Fix locally before pushing:"
    echo "  uv run ruff format src tests"
    echo "  uv run ruff check --fix src tests"
    echo "  uv run mypy --strict src/repoforge"
    echo ""
    exit 1
fi

# Collect changed files in the push range.
# During a normal push, git feeds "local_ref local_sha remote_ref remote_sha" lines on stdin.
while read local_ref local_sha remote_ref remote_sha; do
    # Determine the range of commits to check
    if [ "$local_sha" = "0000000000000000000000000000000000000000" ]; then
        # Deleting a branch — skip
        continue
    fi
    if [ "$remote_sha" = "0000000000000000000000000000000000000000" ]; then
        # New branch — check against origin/main if available
        if git rev-parse origin/main >/dev/null 2>&1; then
            range="origin/main..$local_sha"
        else
            # No origin/main — cannot diff, skip contract check
            continue
        fi
    else
        range="$remote_sha..$local_sha"
    fi

    changed=$(git diff --name-only "$range" -- src/repoforge/interfaces/mcp/ docs/contracts/ 2>/dev/null || true)
    if [ -z "$changed" ]; then
        continue
    fi

    echo "🔍 MCP interface or contract files changed — validating release contract..."

    if ! uv run python scripts/check_release_contracts.py 2>/dev/null; then
        echo ""
        echo -e "${RED}⛔ REJECTED: Release contract is stale.${NC}"
        echo "Changed files:"
        echo "$changed"
        echo ""
        echo "Regenerate the contract and commit it:"
        echo "  uv run python scripts/check_release_contracts.py --write"
        echo "  git add docs/contracts/release-contract-v1.json"
        echo "  git commit -m \"fix: update release contract\""
        echo ""
        exit 1
    fi
done

exit 0
