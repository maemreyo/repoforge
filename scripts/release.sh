#!/bin/sh
# Quick release script for RepoForge.
# Usage:  scripts/release.sh [patch|minor|major]
# Depends: git, uv, gh (for GitHub release)
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

# --- helpers ---------------------------------------------------------------
die() { echo "❌ $*" >&2; exit 1; }
info() { echo "• $*"; }
step() { echo ""; echo "═══ $* ═══"; }

# --- parse bump type ---------------------------------------------------------
BUMP="${1:-minor}"
CURRENT=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
CURRENT="${CURRENT#v}"  # strip v prefix

# Compute next version
major=$(echo "$CURRENT" | cut -d. -f1)
minor=$(echo "$CURRENT" | cut -d. -f2)
patch=$(echo "$CURRENT" | cut -d. -f3)

case "$BUMP" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
  *) die "unknown bump type: $BUMP (use patch|minor|major)" ;;
esac

VERSION="${major}.${minor}.${patch}"
TAG="v${VERSION}"

# --- sanity checks -----------------------------------------------------------
step "Pre-flight"
git diff --exit-code --stat || die "Working tree is dirty; commit or stash first"
git diff --cached --exit-code --stat || die "Staged changes; commit or unstage first"

info "Current: v${CURRENT} → ${TAG}"

# --- bump version ------------------------------------------------------------
step "Bump version to ${VERSION}"
echo "$VERSION"

# Update __init__.py
sed -i '' "s/^__version__ = \".*\"/__version__ = \"${VERSION}\"/" src/repoforge/__init__.py

# Update pyproject.toml
sed -i '' "s/^version = \".*\"/version = \"${VERSION}\"/" pyproject.toml

# Update verify-wheel-install.sh assertion
sed -i '' "s/assert repoforge.__version__ == \".*\"/assert repoforge.__version__ == \"${VERSION}\"/" scripts/verify-wheel-install.sh

# --- update changelog --------------------------------------------------------
step "Update CHANGELOG.md"
TODAY=$(date +%Y-%m-%d)
# Insert release header right after "## Unreleased"
if grep -q "^## Unreleased" CHANGELOG.md; then
  # Use a temp file for portability
  awk -v version="$VERSION" -v date="$TODAY" '
    /^## Unreleased/ {
      print
      print ""
      print "## " version " — " date
      next
    }
    { print }
  ' CHANGELOG.md > CHANGELOG.md.tmp && mv CHANGELOG.md.tmp CHANGELOG.md
fi

# --- run full production gate ------------------------------------------------
step "Production verification"
scripts/verify-production.sh || die "Production verification failed"

# --- commit & tag ------------------------------------------------------------
step "Commit & tag"
git add -A
git commit -m "chore: bump version to ${VERSION}" -m "" -m "Release ${TAG}."
git tag -a "$TAG" -m "v${VERSION}"

# --- push --------------------------------------------------------------------
step "Push"
git push origin main --tags
info "Pushed ${TAG} to origin"

step "Done! Released ${TAG}"
echo ""
echo "Next steps:"
echo "  gh release view ${TAG}"
