#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST_DIR=${1:-"$ROOT/dist"}
WHEEL=$(find "$DIST_DIR" -maxdepth 1 -type f -name 'repoforge_mcp-*.whl' | sort | tail -n 1)
if [ -z "$WHEEL" ]; then
  echo "no RepoForge wheel found in $DIST_DIR" >&2
  exit 1
fi

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/repoforge-wheel-smoke.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT INT TERM
uv venv --python "${REPOFORGE_SMOKE_PYTHON:-python}" "$TMP_ROOT/venv" >/dev/null
uv pip install --python "$TMP_ROOT/venv/bin/python" "$WHEEL" >/dev/null
"$TMP_ROOT/venv/bin/python" - <<'PY'
import asyncio
import json

import repoforge
from repoforge.interfaces.cli.main import build_parser
from repoforge.interfaces.mcp.contract import build_release_contract

assert repoforge.__version__ == "2.0.0"
parser = build_parser()
commands = parser._subparsers._group_actions[0].choices
for required in ("repo", "runtime", "config", "diagnostics", "start", "serve"):
    assert required in commands, required
contract = asyncio.run(build_release_contract())
assert contract["package_version"] == repoforge.__version__
assert len(contract["mcp"]["tools"]) >= 25
print(json.dumps({
    "status": "ok",
    "package_version": repoforge.__version__,
    "tool_count": len(contract["mcp"]["tools"]),
    "tool_surface_hash": contract["mcp"]["tool_surface_hash"],
}, sort_keys=True))
PY
"$TMP_ROOT/venv/bin/rf" --version
"$TMP_ROOT/venv/bin/python" "$ROOT/scripts/verify-wheel-e2e.py"
