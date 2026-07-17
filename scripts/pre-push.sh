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

workspace_fingerprint() {
    {
        git diff --binary --no-ext-diff -- src tests
        git diff --cached --binary --no-ext-diff -- src tests
        while IFS= read -r -d '' path; do
            printf 'untracked:%s\0' "$path"
            git hash-object -- "$path"
        done < <(git ls-files --others --exclude-standard -z -- src tests)
    } | git hash-object --stdin
}

before_format=$(workspace_fingerprint)
format_failed=0
run_check "ruff format" uv run ruff format src tests || format_failed=1
run_check "ruff check --fix" uv run ruff check --fix src tests || format_failed=1
after_format=$(workspace_fingerprint)

if [ "$before_format" != "$after_format" ]; then
    echo ""
    echo -e "${RED}⛔ REJECTED: Auto-format changed the working tree.${NC}"
    echo "Review and commit those changes before pushing again:"
    echo "  git diff -- src tests"
    echo "  git add <reviewed-files>"
    echo "  git commit"
    echo ""
    exit 1
fi

run_check "mypy --strict" uv run mypy --strict src/repoforge || format_failed=1

if [ "$format_failed" -ne 0 ]; then
    echo ""
    echo -e "${RED}⛔ REJECTED: format/lint/typecheck failed (see above).${NC}"
    echo "Fix the reported issues before pushing again."
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
