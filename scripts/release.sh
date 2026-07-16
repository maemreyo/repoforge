#!/bin/sh
# Reviewed RepoForge release workflow.
# Usage: scripts/release.sh patch|minor|major
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

die() { printf 'release error: %s\n' "$*" >&2; exit 1; }
info() { printf '• %s\n' "$*"; }
step() { printf '\n═══ %s ═══\n' "$*"; }

BUMP="${1:-}"
case "$BUMP" in
  patch|minor|major) ;;
  '') die "explicit bump required: patch, minor, or major" ;;
  *) die "unknown bump type: $BUMP (use patch|minor|major)" ;;
esac

command -v git >/dev/null 2>&1 || die "git is required"
command -v uv >/dev/null 2>&1 || die "uv is required"
command -v gh >/dev/null 2>&1 || die "gh is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

step "Pre-flight"
[ "$(git branch --show-current)" = "main" ] || die "release must run from main"
[ -z "$(git status --porcelain)" ] || die "working tree contains tracked or untracked changes"
git fetch origin main --tags
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" || die "local main must exactly match origin/main"

CURRENT=$(python3 - <<'PY'
from pathlib import Path
import re
text = Path("pyproject.toml").read_text(encoding="utf-8")
match = re.search(r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$', text, re.MULTILINE)
if match is None:
    raise SystemExit("pyproject.toml project version is missing or unsupported")
print(match.group(1))
PY
)

VERSION=$(python3 - "$CURRENT" "$BUMP" <<'PY'
import sys
major, minor, patch = (int(part) for part in sys.argv[1].split("."))
kind = sys.argv[2]
if kind == "major":
    major, minor, patch = major + 1, 0, 0
elif kind == "minor":
    minor, patch = minor + 1, 0
else:
    patch += 1
print(f"{major}.{minor}.{patch}")
PY
)
TAG="v$VERSION"

git rev-parse -q --verify "refs/tags/$TAG" >/dev/null 2>&1 && die "local tag already exists: $TAG"
git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1 && die "remote tag already exists: $TAG"
info "Current: v$CURRENT → $TAG"

step "Update version and changelog"
python3 - "$VERSION" <<'PY'
from pathlib import Path
import re
import sys
from datetime import date

version = sys.argv[1]
updates = {
    Path("src/repoforge/__init__.py"): (
        r'^__version__ = ".*"$',
        f'__version__ = "{version}"',
    ),
    Path("pyproject.toml"): (
        r'^version = ".*"$',
        f'version = "{version}"',
    ),
    Path("scripts/verify-wheel-install.sh"): (
        r'assert repoforge\.__version__ == ".*"',
        f'assert repoforge.__version__ == "{version}"',
    ),
}
for path, (pattern, replacement) in updates.items():
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"could not update version in {path}")
    path.write_text(updated, encoding="utf-8")

changelog = Path("CHANGELOG.md")
text = changelog.read_text(encoding="utf-8")
heading = f"## {version} — {date.today().isoformat()}"
if heading in text:
    raise SystemExit(f"changelog already contains {heading}")
marker = "## Unreleased\n"
if marker not in text:
    raise SystemExit("CHANGELOG.md is missing ## Unreleased")
text = text.replace(marker, marker + "\n" + heading + "\n", 1)
changelog.write_text(text, encoding="utf-8")
PY
uv lock --offline

step "Production verification"
scripts/verify-production.sh --allow-dirty

step "Build release artifacts"
rm -rf dist
uv build --out-dir dist
set -- $(find dist -maxdepth 1 -type f -name '*.whl' -print)
[ "$#" -eq 1 ] || die "expected exactly one wheel, found $#"
WHEEL=$1
set -- $(find dist -maxdepth 1 -type f -name '*.tar.gz' -print)
[ "$#" -eq 1 ] || die "expected exactly one source distribution, found $#"
SDIST=$1
python3 - "$WHEEL" "$SDIST" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
lines = []
for raw in sys.argv[1:]:
    path = Path(raw)
    lines.append(f"{sha256(path.read_bytes()).hexdigest()}  {path.name}")
Path("dist/SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

step "Commit and tag exact verified tree"
git add CHANGELOG.md pyproject.toml uv.lock src/repoforge/__init__.py scripts/verify-wheel-install.sh
git commit -m "chore(release): bump version to $VERSION" -m "Release $TAG."
git tag -a "$TAG" -m "$TAG"

step "Push reviewed commit and tag"
git push origin main
git push origin "$TAG"

step "Create GitHub release"
NOTES=$(awk "/^## $VERSION — /{found=1; next} /^## [0-9]+\.[0-9]+\.[0-9]+ — /{if(found) exit} found{print}" CHANGELOG.md)
if gh release view "$TAG" >/dev/null 2>&1; then
  info "GitHub Release already exists: $TAG"
else
  gh release create "$TAG" \
    --verify-tag \
    --title "$TAG" \
    --notes "$(printf "## What's New\n%s\n\nSee CHANGELOG.md for full details.\n" "$NOTES")" \
    "$WHEEL" \
    "$SDIST" \
    dist/SHA256SUMS
fi

step "Done"
info "Released $TAG"
